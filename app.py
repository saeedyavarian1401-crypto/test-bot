# ==================== config.py ====================
from flask import Flask, request, jsonify
import requests
from datetime import datetime, timedelta
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
import time
import secrets
import hashlib
import hmac
import base64
import random
from typing import Dict, Optional, Tuple, Any, List
from dataclasses import dataclass, asdict
from functools import lru_cache, wraps
from threading import Lock
from collections import defaultdict
import re

# ==================== تنظیمات اولیه ====================
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ')
JWT_SECRET = os.environ.get('JWT_SECRET', secrets.token_hex(32))
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
TALISMAN_PASSWORD = "13640624"
TALISMAN_SALT = b'occult_v5_talisman_salt_2024'
# ==================== security.py ====================
from config import *

# ==================== سیستم رمز ۱۳۶۴۰۶۲۴ برای طلسمات ====================
def _hash_password(password: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), TALISMAN_SALT, 100000).hex()

class TalismanProtector:
    _instance = None
    _unlocked = False
    _unlock_time = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._password_hash = _hash_password(TALISMAN_PASSWORD)
    
    def unlock(self, password: str) -> bool:
        if _hash_password(password) == self._password_hash:
            self._unlocked = True
            self._unlock_time = datetime.now()
            logger.info("🔓 قفل طلسمات ویژه باز شد")
            return True
        else:
            logger.warning("🔒 تلاش ناموفق برای باز کردن قفل طلسمات")
            return False
    
    def is_unlocked(self) -> bool:
        return self._unlocked
    
    def lock(self):
        self._unlocked = False
        logger.info("🔒 قفل طلسمات ویژه فعال شد")
    
    def require_unlock(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if not self._unlocked:
                return {"error": "برای دسترسی به طلسمات ویژه، رمز ۱۳۶۴۰۶۲۴ را وارد کنید"}
            return func(self, *args, **kwargs)
        return wrapper

talisman_protector = TalismanProtector()

# ==================== JWT احراز هویت ====================
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60

def create_jwt_token(user_id: str, username: str) -> str:
    import jwt
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "iat": datetime.utcnow(),
        "jti": secrets.token_hex(16)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_jwt_token(token: str) -> Optional[Dict]:
    import jwt
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return {"error": "توکن منقضی شده است"}
    except jwt.InvalidTokenError:
        return {"error": "توکن نامعتبر است"}

# ==================== Rate Limiter ====================
class RateLimiter:
    def __init__(self, capacity: int = 100, refill_rate: int = 10, refill_interval: int = 1):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.refill_interval = refill_interval
        self._buckets: Dict[str, Dict] = {}
        self._lock = Lock()
    
    def _get_bucket(self, key: str) -> Dict:
        now = time.time()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = {"tokens": self.capacity, "last_refill": now}
            else:
                bucket = self._buckets[key]
                elapsed = now - bucket["last_refill"]
                new_tokens = elapsed * (self.refill_rate / self.refill_interval)
                bucket["tokens"] = min(self.capacity, bucket["tokens"] + new_tokens)
                bucket["last_refill"] = now
            return self._buckets[key]
    
    def consume(self, key: str, tokens: int = 1) -> bool:
        bucket = self._get_bucket(key)
        with self._lock:
            if bucket["tokens"] >= tokens:
                bucket["tokens"] -= tokens
                return True
            return False
    
    def get_available_tokens(self, key: str) -> float:
        bucket = self._get_bucket(key)
        with self._lock:
            return bucket["tokens"]

rate_limiter = RateLimiter(capacity=100, refill_rate=10, refill_interval=1)

# ==================== Circuit Breaker ====================
class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"
        self.lock = Lock()
    
    def call(self, func, *args, **kwargs):
        with self.lock:
            if self.state == "OPEN":
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    logger.info(f"🔌 Circuit breaker {self.name} -> HALF_OPEN")
                else:
                    return {"error": f"Circuit breaker {self.name} is OPEN"}
        
        try:
            result = func(*args, **kwargs)
            with self.lock:
                if self.state == "HALF_OPEN":
                    self.state = "CLOSED"
                    self.failure_count = 0
                    logger.info(f"🔌 Circuit breaker {self.name} -> CLOSED")
            return result
        except Exception as e:
            with self.lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold and self.state != "OPEN":
                    self.state = "OPEN"
                    logger.error(f"🔌 Circuit breaker {self.name} -> OPEN")
            return {"error": str(e)}

circuit_breaker = CircuitBreaker("predictor", failure_threshold=5, recovery_timeout=60)

# ==================== Redis Cache ====================
class RedisCache:
    def __init__(self, ttl: int = 3600):
        self.ttl = ttl
        self._redis = None
        self._fallback = {}
        self._enabled = False
        self._init_redis()
    
    def _init_redis(self):
        try:
            import redis
            self._redis = redis.from_url(REDIS_URL, decode_responses=True)
            self._redis.ping()
            self._enabled = True
            logger.info("✅ Redis متصل شد")
        except ImportError:
            logger.info("ℹ️ redis-py نصب نیست - استفاده از کش ساده")
        except Exception as e:
            logger.warning(f"⚠️ اتصال به Redis ناموفق: {e} - استفاده از کش ساده")
    
    def get(self, key: str) -> Optional[Any]:
        if self._enabled:
            try:
                value = self._redis.get(key)
                if value:
                    return json.loads(value)
            except:
                pass
        if key in self._fallback:
            value, expire = self._fallback[key]
            if time.time() < expire:
                return value
            del self._fallback[key]
        return None
    
    def set(self, key: str, value: Any):
        if self._enabled:
            try:
                self._redis.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
            except:
                pass
        self._fallback[key] = (value, time.time() + self.ttl)
    
    def is_enabled(self) -> bool:
        return self._enabled

cache = RedisCache()
# ==================== database.py ====================
from config import *

class Database:
    def __init__(self, db_path='jafr_bot.db'):
        self.db_path = db_path
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    chat_id TEXT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_queries INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_sessions (
                    chat_id TEXT PRIMARY KEY,
                    step TEXT,
                    name TEXT,
                    mother TEXT,
                    day INTEGER,
                    month INTEGER,
                    year INTEGER,
                    question TEXT,
                    jafr_type TEXT,
                    fortune_type TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS query_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    question TEXT,
                    jafr_36_result TEXT,
                    jafr_360_result TEXT,
                    fortune_type TEXT,
                    fortune_result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    name TEXT NOT NULL,
                    mother TEXT NOT NULL,
                    birth_date TEXT NOT NULL,
                    question TEXT,
                    result_encrypted TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processing_time_ms REAL DEFAULT 0,
                    mode TEXT DEFAULT 'complete'
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS fortune_readings (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    type TEXT NOT NULL,
                    input_data TEXT,
                    result_encrypted TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    endpoint TEXT,
                    status_code INTEGER,
                    processing_time_ms REAL,
                    timestamp TEXT
                )
            ''')
            conn.commit()
            logger.info("✅ دیتابیس راه‌اندازی شد")
    
    def get_user(self, chat_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            result = conn.execute('SELECT * FROM users WHERE chat_id = ?', (chat_id,)).fetchone()
            return dict(result) if result else None
    
    def create_user(self, chat_id: str, username: str = '', first_name: str = '', last_name: str = ''):
        with self.get_connection() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO users (chat_id, username, first_name, last_name) VALUES (?, ?, ?, ?)',
                (chat_id, username, first_name, last_name)
            )
            conn.commit()
    
    def increment_queries(self, chat_id: str):
        with self.get_connection() as conn:
            conn.execute('UPDATE users SET total_queries = total_queries + 1 WHERE chat_id = ?', (chat_id,))
            conn.commit()
    
    def save_session(self, chat_id: str, step: str, **kwargs):
        with self.get_connection() as conn:
            conn.execute('DELETE FROM user_sessions WHERE chat_id = ?', (chat_id,))
            conn.execute(
                '''INSERT INTO user_sessions 
                   (chat_id, step, name, mother, day, month, year, question, jafr_type, fortune_type) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (chat_id, step, 
                 kwargs.get('name', ''),
                 kwargs.get('mother', ''),
                 kwargs.get('day', None),
                 kwargs.get('month', None),
                 kwargs.get('year', None),
                 kwargs.get('question', ''),
                 kwargs.get('jafr_type', 'both'),
                 kwargs.get('fortune_type', None))
            )
            conn.commit()
    
    def get_session(self, chat_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            result = conn.execute('SELECT * FROM user_sessions WHERE chat_id = ?', (chat_id,)).fetchone()
            return dict(result) if result else None
    
    def delete_session(self, chat_id: str):
        with self.get_connection() as conn:
            conn.execute('DELETE FROM user_sessions WHERE chat_id = ?', (chat_id,))
            conn.commit()
    
    def save_query_history(self, chat_id: str, question: str, j36: Dict = None, j360: Dict = None, 
                           fortune_type: str = None, fortune_result: Dict = None):
        with self.get_connection() as conn:
            conn.execute(
                '''INSERT INTO query_history 
                   (chat_id, question, jafr_36_result, jafr_360_result, fortune_type, fortune_result) 
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (chat_id, question, 
                 json.dumps(j36) if j36 else None, 
                 json.dumps(j360) if j360 else None, 
                 fortune_type, 
                 json.dumps(fortune_result) if fortune_result else None)
            )
            conn.commit()
    
    def save_prediction(self, prediction_id: str, user_id: str, name: str, mother: str, 
                        birth_date: str, question: str, result: Dict, processing_time_ms: float, mode: str = "complete"):
        encrypted_result = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        with self.get_connection() as conn:
            conn.execute('''
                INSERT INTO predictions (id, user_id, name, mother, birth_date, question, result_encrypted, created_at, processing_time_ms, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (prediction_id, user_id, name, mother, birth_date, question, encrypted_result, 
                  datetime.now().isoformat(), processing_time_ms, mode))
            conn.commit()
    
    def save_fortune_reading(self, reading_id: str, user_id: str, reading_type: str, input_data: Dict, result: Dict):
        encrypted_result = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        with self.get_connection() as conn:
            conn.execute('''
                INSERT INTO fortune_readings (id, user_id, type, input_data, result_encrypted, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (reading_id, user_id, reading_type, json.dumps(input_data), encrypted_result, datetime.now().isoformat()))
            conn.commit()
    
    def get_stats(self) -> Dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM predictions')
            total_predictions = cursor.fetchone()[0]
            cursor.execute('SELECT AVG(processing_time_ms) FROM predictions')
            avg_time = cursor.fetchone()[0] or 0
            cursor.execute('SELECT COUNT(*) FROM requests WHERE status_code = 200')
            success_requests = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM requests')
            total_requests = cursor.fetchone()[0] or 1
            return {
                "total_predictions": total_predictions,
                "average_processing_time_ms": round(avg_time, 2),
                "success_rate": round((success_requests / total_requests) * 100, 2),
                "total_requests": total_requests
            }

db = Database()
