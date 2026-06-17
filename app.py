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
