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
# ==================== jafr_core.py ====================
from database import db
from security import cache, circuit_breaker, rate_limiter, talisman_protector

# ==================== کلاس‌های داده ====================
@dataclass
class JafrResult:
    answer: str
    score: int
    advice: str
    remainder: int
    degree: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)

# ==================== فرهنگ ابجد ====================
ABJAD = {
    'ا':1, 'ب':2, 'ج':3, 'د':4, 'ه':5, 'و':6, 'ز':7, 'ح':8, 'ط':9, 'ی':10,
    'ک':20, 'ل':30, 'م':40, 'ن':50, 'س':60, 'ع':70, 'ف':80, 'ص':90, 'ق':100,
    'ر':200, 'ش':300, 'ت':400, 'ث':500, 'خ':600, 'ذ':700, 'ض':800, 'ظ':900, 'غ':1000,
    'پ':2, 'چ':3, 'ژ':7, 'گ':20, 'آ':1, 'أ':1, 'إ':1, 'ة':5, 'ى':10, 'ئ':10, 'ؤ':6,
    '‌':0, ' ':0
}

def abjad_sum(text: str) -> int:
    if not text:
        return 0
    total = 0
    for ch in text:
        if ch in ABJAD:
            total += ABJAD[ch]
    return total

def reduce_to_single(n: int) -> int:
    if n == 0:
        return 0
    while n > 9:
        n = sum(int(d) for d in str(n))
    return n

def life_path_number(day: int, month: int, year: int) -> int:
    total = sum(int(d) for d in f"{day}{month}{year}")
    return reduce_to_single(total)

# ==================== طالع و بروج ====================
ZODIAC = [
    {"name": "حمل", "element": "آتشی", "planet": "مریخ", "stone": "یاقوت سرخ", "start": (21, 3), "end": (19, 4)},
    {"name": "ثور", "element": "خاکی", "planet": "زهره", "stone": "زمرد", "start": (20, 4), "end": (20, 5)},
    {"name": "جوزا", "element": "بادی", "planet": "عطارد", "stone": "عقیق", "start": (21, 5), "end": (20, 6)},
    {"name": "سرطان", "element": "آبی", "planet": "ماه", "stone": "مروارید", "start": (21, 6), "end": (22, 7)},
    {"name": "اسد", "element": "آتشی", "planet": "خورشید", "stone": "یاقوت زرد", "start": (23, 7), "end": (22, 8)},
    {"name": "سنبله", "element": "خاکی", "planet": "عطارد", "stone": "زبرجد", "start": (23, 8), "end": (22, 9)},
    {"name": "میزان", "element": "بادی", "planet": "زهره", "stone": "عقیق", "start": (23, 9), "end": (22, 10)},
    {"name": "عقرب", "element": "آبی", "planet": "مریخ", "stone": "یاقوت کبود", "start": (23, 10), "end": (21, 11)},
    {"name": "قوس", "element": "آتشی", "planet": "مشتری", "stone": "فیروزه", "start": (22, 11), "end": (21, 12)},
    {"name": "جدی", "element": "خاکی", "planet": "زحل", "stone": "یشم", "start": (22, 12), "end": (19, 1)},
    {"name": "دلو", "element": "بادی", "planet": "زحل", "stone": "لاجورد", "start": (20, 1), "end": (18, 2)},
    {"name": "حوت", "element": "آبی", "planet": "مشتری", "stone": "سنگ ماه", "start": (19, 2), "end": (20, 3)}
]

LIFE_PATH_DATA = {
    1: {"name": "لیدر", "color": "قرمز", "stone": "یاقوت", "strength": "رهبری", "weakness": "خودخواهی"},
    2: {"name": "همکار", "color": "نارنجی", "stone": "عقیق", "strength": "همکاری", "weakness": "وابستگی"},
    3: {"name": "هنرمند", "color": "زرد", "stone": "زمرد", "strength": "خلاقیت", "weakness": "پراکندگی"},
    4: {"name": "سازنده", "color": "سبز", "stone": "یشم", "strength": "نظم", "weakness": "انعطاف‌ناپذیری"},
    5: {"name": "ماجراجو", "color": "آبی", "stone": "فیروزه", "strength": "آزادی", "weakness": "بی‌قراری"},
    6: {"name": "خدمتگزار", "color": "نیلی", "stone": "لاجورد", "strength": "مسئولیت", "weakness": "وسواس"},
    7: {"name": "جستجوگر", "color": "بنفش", "stone": "آمتیست", "strength": "تعمق", "weakness": "گوشه‌گیری"},
    8: {"name": "قدرت", "color": "سیاه", "stone": "الماس", "strength": "ثروت", "weakness": "استبداد"},
    9: {"name": "انسان دوست", "color": "سفید", "stone": "مروارید", "strength": "بخشش", "weakness": "ساده‌لوحی"}
}

def get_zodiac(day: int, month: int, year: int) -> Dict:
    try:
        import jdatetime
        greg = jdatetime.date(year, month, day).togregorian()
        g_month, g_day = greg.month, greg.day
    except:
        g_month, g_day = month, day
    
    for z in ZODIAC:
        s_day, s_month = z["start"]
        e_day, e_month = z["end"]
        if s_month > e_month:
            if (g_month == s_month and g_day >= s_day) or (g_month == e_month and g_day <= e_day):
                return z
        else:
            if (g_month == s_month and g_day >= s_day) or (g_month == e_month and g_day <= e_day):
                return z
    return ZODIAC[0]

# ==================== تحلیلگر سوال ====================
class QuestionAnalyzer:
    NEGATIVE_KEYWORDS = {
        'طلسم', 'سحر', 'جادو', 'نحس', 'بدشانسی', 'بیماری', 'مشکل', 
        'گرفتاری', 'بدهی', 'دشمن', 'خطر', 'آسیب', 'شکست', 'رد', 
        'ناسازگاری', 'اختلاف', 'جدایی', 'طلاق', 'مرگ', 'فقر'
    }
    
    POSITIVE_KEYWORDS = {
        'موفق', 'پول', 'ثروت', 'عشق', 'ازدواج', 'سفر', 'کار', 
        'شغل', 'ترفیع', 'تبریک', 'خوشبختی', 'سلامت', 'شفا',
        'برکت', 'روزی', 'عاقبت'
    }
    
    @staticmethod
    def analyze_question(question: str) -> Dict:
        question = question.lower()
        has_negative = any(keyword in question for keyword in QuestionAnalyzer.NEGATIVE_KEYWORDS)
        has_positive = any(keyword in question for keyword in QuestionAnalyzer.POSITIVE_KEYWORDS)
        
        return {
            'has_negative': has_negative,
            'has_positive': has_positive,
            'is_negative_question': has_negative and not has_positive,
            'is_positive_question': has_positive and not has_negative
        }

# ==================== جفر 36 و 360 ====================
@lru_cache(maxsize=128)
def jafar_36(question: str) -> Dict:
    total = abjad_sum(question)
    remainder = total % 36
    
    if remainder == 0:
        return {"answer": "✅ بله - قطعاً انجام می‌شود", "score": 95, "advice": "با اطمینان کامل اقدام کنید", "accuracy": "قطعی"}
    elif remainder <= 9:
        return {"answer": "✅ بله - با احتمال زیاد", "score": 85, "advice": "مانعی نیست، اقدام کن", "accuracy": "قوی"}
    elif remainder <= 18:
        return {"answer": "⚠️ بله - با احتیاط", "score": 65, "advice": "صدقه بدهید و توکل کنید", "accuracy": "متوسط"}
    elif remainder <= 27:
        return {"answer": "❓ شاید - مصلحت نیست", "score": 50, "advice": "چند روز صبر کنید", "accuracy": "متوسط"}
    else:
        return {"answer": "❌ خیر - مشکل دارد", "score": 30, "advice": "بهتر است منصرف شوید", "accuracy": "ضعیف"}

def jafar_360(question: str, name: str, mother: str) -> Dict:
    total = abjad_sum(question) + abjad_sum(name) + abjad_sum(mother)
    remainder = total % 360
    
    if remainder < 30:
        return {"answer": "✅ بله قطعی - گشایش بزرگ", "score": 98, "advice": "بدون تردید اقدام کن", "degree": "عالی"}
    elif remainder < 90:
        return {"answer": "✅ بله - موفقیت", "score": 85, "advice": "زمان مناسبه، اقدام کن", "degree": "خوب"}
    elif remainder < 150:
        return {"answer": "⚠️ احتمالاً - با تلاش", "score": 65, "advice": "تلاش بیشتری کن", "degree": "متوسط"}
    elif remainder < 210:
        return {"answer": "❓ شاید - صبر کن", "score": 50, "advice": "فعلاً صبر کن و دوباره امتحان کن", "degree": "متوسط"}
    elif remainder < 270:
        return {"answer": "❌ خیر - مانع داره", "score": 35, "advice": "بهتره منصرف شی", "degree": "ضعیف"}
    else:
        return {"answer": "❌ خیر قطعی - مشکل داره", "score": 20, "advice": "اصلاً مناسب نیست", "degree": "خیلی ضعیف"}

# ==================== جفر هوشمند ====================
class SmartJafrCalculator:
    @staticmethod
    def calculate_36(question: str, name: str, mother: str, day: int, month: int, year: int) -> JafrResult:
        analysis = QuestionAnalyzer.analyze_question(question)
        total = abjad_sum(question) + abjad_sum(name) + abjad_sum(mother)
        remainder = total % 36
        
        if analysis['is_negative_question']:
            if remainder <= 9:
                return JafrResult(
                    answer="✅ نگران نباشید - وضعیت خوب است",
                    score=80,
                    advice="احتمال مشکل جدی وجود ندارد. به زندگی عادی ادامه دهید.",
                    remainder=remainder
                )
            elif remainder <= 18:
                return JafrResult(
                    answer="🔍 نیاز به بررسی دارد",
                    score=55,
                    advice="احتمال ضعیفی وجود دارد. با یک فرد متخصص مشورت کنید.",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="❌ شاید مشکل وجود داشته باشد",
                    score=40,
                    advice="توصیه می‌کنم به پزشک یا مشاور مراجعه کنید.",
                    remainder=remainder
                )
        elif analysis['is_positive_question']:
            if remainder <= 9:
                return JafrResult(
                    answer="✅ بله - موفقیت قطعی است",
                    score=95,
                    advice="زمان عالی برای اقدام است. با انرژی جلو بروید.",
                    remainder=remainder
                )
            elif remainder <= 18:
                return JafrResult(
                    answer="✅ بله - با کمی تلاش",
                    score=75,
                    advice="موفق خواهید شد، اما نیاز به پشتکار دارید.",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="⚠️ ممکن است - صبر کنید",
                    score=55,
                    advice="فعلاً صبر کنید و برنامه‌ریزی دقیق‌تری انجام دهید.",
                    remainder=remainder
                )
        else:
            if remainder == 0:
                return JafrResult(
                    answer="✅ بله - قطعاً انجام می‌شود",
                    score=95,
                    advice="با اطمینان کامل اقدام کنید",
                    remainder=remainder
                )
            elif remainder <= 9:
                return JafrResult(
                    answer="✅ بله - با احتمال زیاد",
                    score=85,
                    advice="مانعی در پیش نیست، اقدام کنید",
                    remainder=remainder
                )
            elif remainder <= 18:
                return JafrResult(
                    answer="⚠️ بله - با احتیاط",
                    score=65,
                    advice="صدقه بدهید و توکل کنید",
                    remainder=remainder
                )
            elif remainder <= 27:
                return JafrResult(
                    answer="❓ شاید - زمان مناسب نیست",
                    score=50,
                    advice="چند روز صبر کنید",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="❌ خیر - مشکلاتی وجود دارد",
                    score=30,
                    advice="بهتر است منصرف شوید",
                    remainder=remainder
                )
    
    @staticmethod
    def calculate_360(question: str, name: str, mother: str, day: int, month: int, year: int) -> JafrResult:
        analysis = QuestionAnalyzer.analyze_question(question)
        total = abjad_sum(question) + abjad_sum(name) + abjad_sum(mother)
        remainder = total % 360
        
        if analysis['is_negative_question']:
            if remainder < 90:
                return JafrResult(
                    answer="✅ خیالت راحت - مشکلی نیست",
                    score=85,
                    advice="هیچ نشانه‌ای از مشکل جدی وجود ندارد.",
                    degree="خوب",
                    remainder=remainder
                )
            elif remainder < 180:
                return JafrResult(
                    answer="🔍 نیاز به بررسی بیشتر",
                    score=50,
                    advice="با یک فرد متخصص مشورت کنید.",
                    degree="متوسط",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="⚠️ احتمال وجود مشکل",
                    score=35,
                    advice="توصیه می‌کنم به پزشک مراجعه کنید.",
                    degree="ضعیف",
                    remainder=remainder
                )
        elif analysis['is_positive_question']:
            if remainder < 60:
                return JafrResult(
                    answer="✅ موفقیت بزرگ در انتظار شماست",
                    score=98,
                    advice="بهترین زمان برای اقدام است.",
                    degree="عالی",
                    remainder=remainder
                )
            elif remainder < 150:
                return JafrResult(
                    answer="✅ موفقیت با کمی تلاش",
                    score=80,
                    advice="به مسیر خود ادامه دهید.",
                    degree="خوب",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="⚠️ با برنامه‌ریزی بیشتر",
                    score=60,
                    advice="برنامه خود را دقیق‌تر کنید.",
                    degree="متوسط",
                    remainder=remainder
                )
        else:
            if remainder < 30:
                return JafrResult(
                    answer="✅ بله قطعی - گشایش بزرگ",
                    score=98,
                    advice="بدون تردید اقدام کنید",
                    degree="عالی",
                    remainder=remainder
                )
            elif remainder < 90:
                return JafrResult(
                    answer="✅ بله - موفقیت چشمگیر",
                    score=85,
                    advice="زمان مناسبی است",
                    degree="خوب",
                    remainder=remainder
                )
            elif remainder < 150:
                return JafrResult(
                    answer="⚠️ احتمالاً - نیاز به تلاش دارد",
                    score=65,
                    advice="تلاش بیشتری کنید",
                    degree="متوسط",
                    remainder=remainder
                )
            elif remainder < 210:
                return JafrResult(
                    answer="❓ شاید - صبر کنید",
                    score=50,
                    advice="فعلاً صبر کنید",
                    degree="متوسط",
                    remainder=remainder
                )
            elif remainder < 270:
                return JafrResult(
                    answer="❌ خیر - موانع جدی",
                    score=35,
                    advice="بهتر است منصرف شوید",
                    degree="ضعیف",
                    remainder=remainder
                )
            else:
                return JafrResult(
                    answer="❌ خیر قطعی - کاملاً نامناسب",
                    score=20,
                    advice="به هیچ وجه اقدام نکنید",
                    degree="خیلی ضعیف",
                    remainder=remainder
                )

# ==================== رمل 8 و 16 شکل ====================
RAML_8 = {
    "اطلال": {"sign": "⚪⚪⚪⚪", "element": "آتش", "meaning": "رفتن و از دست دادن", "good": False},
    "نقا": {"sign": "⚪⚪⚪⚫", "element": "خاک", "meaning": "نقص و کمبود", "good": False},
    "عقله": {"sign": "⚪⚪⚫⚪", "element": "هوا", "meaning": "عقل و تدبیر", "good": True},
    "بید": {"sign": "⚪⚪⚫⚫", "element": "آب", "meaning": "محبت", "good": True},
    "سعاده": {"sign": "⚪⚫⚪⚪", "element": "آتش", "meaning": "خوشبختی", "good": True},
    "رجل": {"sign": "⚪⚫⚪⚫", "element": "خاک", "meaning": "مردانگی", "good": True},
    "نصر": {"sign": "⚫⚪⚫⚪", "element": "هوا", "meaning": "پیروزی", "good": True},
    "ثابت": {"sign": "⚫⚫⚫⚫", "element": "آب", "meaning": "ثبات", "good": True}
}

RAML_16 = {
    "اطلال": {"sign": "⚪⚪⚪⚪", "element": "آتش", "meaning": "رفتن و از دست دادن", "good": False},
    "منقاد": {"sign": "⚪⚪⚪⚫", "element": "خاک", "meaning": "فرمانبردار", "good": True},
    "انفراد": {"sign": "⚪⚪⚫⚪", "element": "هوا", "meaning": "تنهایی", "good": False},
    "اتصال": {"sign": "⚪⚪⚫⚫", "element": "آب", "meaning": "ارتباط", "good": True},
    "فتح": {"sign": "⚪⚫⚪⚪", "element": "آتش", "meaning": "گشایش", "good": True},
    "نصر": {"sign": "⚪⚫⚪⚫", "element": "خاک", "meaning": "پیروزی", "good": True},
    "سعادت": {"sign": "⚪⚫⚫⚪", "element": "هوا", "meaning": "خوشبختی", "good": True},
    "عاقبت": {"sign": "⚪⚫⚫⚫", "element": "آب", "meaning": "پایان", "good": False},
    "زیاده": {"sign": "⚫⚪⚪⚪", "element": "آتش", "meaning": "افزایش", "good": True},
    "نقصان": {"sign": "⚫⚪⚪⚫", "element": "خاک", "meaning": "کمبود", "good": False},
    "اجتماع": {"sign": "⚫⚪⚫⚪", "element": "هوا", "meaning": "جمع شدن", "good": True},
    "افتراق": {"sign": "⚫⚪⚫⚫", "element": "آب", "meaning": "جدایی", "good": False},
    "جذب": {"sign": "⚫⚫⚪⚪", "element": "آتش", "meaning": "جذب", "good": True},
    "دفع": {"sign": "⚫⚫⚪⚫", "element": "خاک", "meaning": "دفع", "good": False},
    "نور": {"sign": "⚫⚫⚫⚪", "element": "هوا", "meaning": "روشنایی", "good": True},
    "ظلمت": {"sign": "⚫⚫⚫⚫", "element": "آب", "meaning": "تاریکی", "good": False}
}

def raml_extract(name: str, use_16: bool = False) -> Dict:
    total = abjad_sum(name)
    raml_dict = RAML_16 if use_16 else RAML_8
    keys = list(raml_dict.keys())
    shape_key = keys[total % len(keys)]
    shape = raml_dict[shape_key]
    return {
        "shape": shape_key,
        "sign": shape["sign"],
        "meaning": shape["meaning"],
        "good": shape["good"],
        "element": shape["element"]
    }

# ==================== همزاد ====================
HAMZAD_SYMPTOMS = [
    "تنگی رزق", "عصبانیت بی‌دلیل", "کم شدن آرامش", "افکار منفی",
    "ترس از تاریکی", "گره در امورات", "افسردگی", "باردار نشدن",
    "بیماری‌های جسمی", "ریزش مو", "بستگی بخت", "بدبیاری متوالی",
    "کابوس دیدن", "احساس حضور", "سنگینی در خانه"
]

def check_hamzad(symptoms: List[str]) -> Dict:
    matched = [s for s in symptoms if s in HAMZAD_SYMPTOMS]
    severity = "شدید" if len(matched) >= 8 else "متوسط" if len(matched) >= 4 else "ضعیف" if len(matched) >= 1 else "هیچ"
    return {
        "has_hamzad": len(matched) >= 4,
        "matched_symptoms": matched,
        "severity": severity,
        "advice": "آیت الکرسی و سوره فلق و ناس را بخوانید، هر روز صبح و شب ۳ مرتبه قل هوالله احد بگویید"
    }

def hamzad_name(name: str) -> Dict:
    total = abjad_sum(name)
    parts = []
    r = total
    for m in [1000, 900, 800, 700, 600, 500, 400, 300, 200, 100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]:
        if r >= m:
            cnt = r // m
            for _ in range(cnt):
                parts.append(m)
            r = r % m
    
    rev_map = {1:'ا', 2:'ب', 3:'ج', 4:'د', 5:'ه', 6:'و', 7:'ز', 8:'ح', 9:'ط', 10:'ی',
               20:'ک', 30:'ل', 40:'م', 50:'ن', 60:'س', 70:'ع', 80:'ف', 90:'ص', 100:'ق',
               200:'ر', 300:'ش', 400:'ت', 500:'ث', 600:'خ', 700:'ذ', 800:'ض', 900:'ظ', 1000:'غ'}
    
    letters = [rev_map[p] for p in parts if p in rev_map]
    return {
        "malaki": ''.join(letters) + "اییل",
        "jinni": ''.join(reversed(letters)) + "ؤش",
        "total": total
    }

# ==================== اوقات سعد و نحس ====================
SAAD_NAHS = {
    1: {"saad": [1,3,5,7,9,11,13], "nahs": [2,4,6,8,10,12,14]},
    2: {"saad": [2,4,6,8,10,12,14], "nahs": [1,3,5,7,9,11,13]},
    3: {"saad": [1,4,7,10,13,16,19], "nahs": [2,5,8,11,14,17,20]},
    4: {"saad": [3,6,9,12,15,18,21], "nahs": [1,4,7,10,13,16,19]},
    5: {"saad": [5,10,15,20,25,30], "nahs": [1,6,11,16,21,26]},
    6: {"saad": [2,7,12,17,22,27], "nahs": [4,9,14,19,24,29]},
    7: {"saad": [1,8,15,22,29], "nahs": [3,10,17,24]},
    8: {"saad": [2,9,16,23,30], "nahs": [4,11,18,25]},
    9: {"saad": [1,4,7,10,13,16,19,22], "nahs": [2,5,8,11,14,17,20,23]},
    10: {"saad": [3,6,9,12,15,18,21,24], "nahs": [1,4,7,10,13,16,19,22]},
    11: {"saad": [5,10,15,20,25,30], "nahs": [1,6,11,16,21,26]},
    12: {"saad": [2,7,12,17,22,27], "nahs": [4,9,14,19,24,29]}
}

PLANETARY_HOURS = {
    0: "زحل (نحس)", 1: "مشتری (سعد)", 2: "مریخ (نحس)", 3: "خورشید (سعد)",
    4: "زهره (سعد)", 5: "عطارد (متوسط)", 6: "ماه (سعد)", 7: "زحل (نحس)",
    8: "مشتری (سعد)", 9: "مریخ (نحس)", 10: "خورشید (سعد)", 11: "زهره (سعد)",
    12: "عطارد (متوسط)", 13: "ماه (سعد)", 14: "زحل (نحس)", 15: "مشتری (سعد)",
    16: "مریخ (نحس)", 17: "خورشید (سعد)", 18: "زهره (سعد)", 19: "عطارد (متوسط)",
    20: "ماه (سعد)", 21: "زحل (نحس)", 22: "مشتری (سعد)", 23: "مریخ (نحس)"
}

def get_saad_nahs(day: int, month: int, year: int) -> Dict:
    lunar_day = (day + month) % 30 + 1
    lunar_month = (month + year) % 12 + 1
    saad_info = SAAD_NAHS.get(lunar_month, {"saad": [], "nahs": []})
    return {
        "lunar_day": lunar_day,
        "lunar_month": lunar_month,
        "is_saad": lunar_day in saad_info["saad"],
        "is_nahs": lunar_day in saad_info["nahs"],
        "status": "سعد" if lunar_day in saad_info["saad"] else "نحس" if lunar_day in saad_info["nahs"] else "معمولی"
    }

def get_planetary_hour(hour: int) -> Dict:
    hour_key = hour % 24
    planet_info = PLANETARY_HOURS.get(hour_key, "نامشخص")
    return {
        "hour": hour,
        "planet": planet_info.split(" ")[0],
        "type": planet_info.split(" ")[1] if " " in planet_info else "متوسط",
        "is_saad": "سعد" in planet_info
    }

# ==================== معادن و کواکب ====================
MINERALS = {
    "شمس": {"metal": "طلا", "plant": "صندل", "animal": "شیر", "incense": "عود", "color": "زرد"},
    "قمر": {"metal": "نقره", "plant": "کافور", "animal": "فیل", "incense": "قسط أبيض", "color": "سفید"},
    "مریخ": {"metal": "مس", "plant": "فلفل", "animal": "پلنگ", "incense": "فلفل", "color": "قرمز"},
    "عطارد": {"metal": "زئبق", "plant": "ریحان", "animal": "کبوتر", "incense": "شمع أبيض", "color": "آبی"},
    "مشتری": {"metal": "قلع", "plant": "لبان", "animal": "گاو", "incense": "لبان أبيض", "color": "سبز"},
    "زهره": {"metal": "مس قرمز", "plant": "گلاب", "animal": "کبوتر", "incense": "مسک", "color": "صورتی"},
    "زحل": {"metal": "سرب", "plant": "بنفشه", "animal": "خفاش", "incense": "صمغ أسود", "color": "سیاه"}
}

def get_mineral(planet: str) -> Dict:
    return MINERALS.get(planet, {"metal": "نامشخص", "plant": "نامشخص", "animal": "نامشخص", "incense": "نامشخص", "color": "نامشخص"})

def get_purification_method(metal: str) -> str:
    PURIFICATION_METHODS = {
        "طلا": "با آب زمزم و خاک کربلا ۷ مرتبه شسته شود سپس با عود عنبر بخور داده شود",
        "نقره": "با آب باران ۳ مرتبه شسته شود سپس با عود صندل بخور داده شود",
        "مس": "با آب و سرکه شسته شود سپس در آفتاب گذاشته شود",
        "سرب": "با آب و نمک شسته شود سپس با عود بخور داده شود",
        "زئبق": "با آب و گلاب شسته شود (با احتیاط کامل)",
        "آهن": "با آب و زاج سفید شسته شود سپس با دمشقی بخور داده شود"
    }
    return PURIFICATION_METHODS.get(metal, "با آب پاک شسته شود و با عود بخور داده شود")

# ==================== تکسیر و بسط ====================
def taksir_correct(word: str, iterations: int = 4) -> Dict:
    if not word:
        return {"lines": [], "zamam": None, "details": "ورودی خالی است"}
    
    chars = list(word)
    all_lines = [word]
    current = chars.copy()
    
    for step in range(iterations):
        new_chars = []
        left, right = 0, len(current) - 1
        while left <= right:
            if left == right:
                new_chars.append(current[left])
                break
            new_chars.append(current[left])
            new_chars.append(current[right])
            left += 1
            right -= 1
        new_word = ''.join(new_chars)
        all_lines.append(new_word)
        current = new_chars
        if new_word == word:
            break
    
    return {
        "lines": all_lines,
        "zamam": all_lines[-1] if len(all_lines) > 1 else None,
        "details": f"تکسیر در {len(all_lines)} سطر"
    }

def basts_azizi(name: str, mother: str) -> Dict:
    combined = name + mother
    total_abjad = abjad_sum(combined)
    letters = list(combined)
    
    malak = ''.join(letters[:5]) + "ائیل" if len(letters) >= 5 else ''.join(letters) + "ائیل"
    awn = ''.join(letters[-5:]) + "وش" if len(letters) >= 5 else ''.join(letters) + "وش"
    
    return {
        "malak": malak,
        "awn": awn,
        "total_abjad": total_abjad
    }

def get_dominant_tab(word: str) -> Dict:
    tab_harf = {
        'ا': 'نار', 'ه': 'نار', 'ط': 'نار', 'م': 'نار', 'ف': 'نار', 'ش': 'نار', 'ذ': 'نار',
        'ب': 'هواء', 'و': 'هواء', 'ی': 'هواء', 'ن': 'هواء', 'ص': 'هواء', 'ت': 'هواء', 'ض': 'هواء',
        'ج': 'ماء', 'ز': 'ماء', 'ک': 'ماء', 'س': 'ماء', 'ق': 'ماء', 'ث': 'ماء', 'ظ': 'ماء',
        'د': 'تراب', 'ح': 'تراب', 'ل': 'تراب', 'ع': 'تراب', 'ر': 'تراب', 'خ': 'تراب', 'غ': 'تراب'
    }
    
    if not word:
        return {"tab": "تراب", "counts": {"نار": 0, "هواء": 0, "ماء": 0, "تراب": 0}}
    
    counts = {"نار": 0, "هواء": 0, "ماء": 0, "تراب": 0}
    for ch in word:
        if ch in tab_harf:
            counts[tab_harf[ch]] += 1
    
    dominant = max(counts, key=counts.get)
    return {"tab": dominant, "counts": counts}

# ==================== زایجه عدل ====================
def zayejah_adl(question: str, qamari_day: int = 15, hour: Optional[int] = None) -> Dict:
    if hour is None:
        hour = datetime.now().hour
    
    total = abjad_sum(question)
    hour_type = 3 if hour % 2 == 1 else 4
    main_num = total + qamari_day + hour_type
    remainder = main_num % 4
    
    tabaye = {1: "آتش", 2: "خاک", 3: "هوا", 0: "آب"}
    interpretations = {
        "آتش": "خیر و برکت با سرعت",
        "خاک": "پایداری با صبر", 
        "هوا": "گشایش با کمک",
        "آب": "آرامش با احساسات"
    }
    hours = {1: "مریخ", 2: "زحل", 3: "مشتری", 0: "زهره"}
    days = {1: "سه‌شنبه", 2: "شنبه", 3: "پنجشنبه", 0: "جمعه"}
    
    return {
        "tab": tabaye[remainder],
        "text": interpretations[tabaye[remainder]],
        "main_number": main_num,
        "remainder": remainder,
        "hour": hours[remainder],
        "day": days[remainder]
    }

# ==================== تعبیر خواب ====================
DREAM_SYMBOLS = {
    "شیر": "قدرت و سلطنت", "مار": "دشمن پنهان", "کلید": "گشایش کار",
    "ماه": "روشنایی دل", "خورشید": "نور و پادشاهی", "آتش": "دشمنی",
    "آب": "پاکی و رزق", "کبوتر": "صلح و آرامش", "عقاب": "قدرت و غلبه",
    "گل": "شادی و خوشی", "درخت": "برکت و رشد", "کوه": "مقام و عظمت",
    "دریا": "علم و دانش", "اسب": "عزت و افتخار", "انگشتر": "ولایت و قدرت",
    "طلا": "ثروت و برکت", "نقره": "عزت و جاه", "الماس": "قدرت و غلبه",
    "پادشاه": "مقام و منصب", "فرشته": "خیر و برکت", "کعبه": "حج و زیارت",
    "مسجد": "عبادت و طاعت", "قرآن": "علم و دانش", "نان": "رزق و روزی",
    "انگور": "برکت و نعمت", "انار": "فرزند و نسل", "عسل": "شفا و درمان",
    "باران": "رحمت و برکت", "سفر": "تغییر و تحول"
}

def get_dream_interpretation(dream: str) -> Tuple[str, str]:
    for key, value in DREAM_SYMBOLS.items():
        if key in dream:
            return (value, "قوی")
    return (f"{dream}: نشانه خیر و برکت است", "متوسط")
