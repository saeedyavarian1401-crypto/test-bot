# ==================== imports ====================
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

# ==================== سیستم رمز 13640624 برای طلسمات ====================
import hashlib
TALISMAN_HASH = hashlib.sha256("13640624".encode()).hexdigest()

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

# ==================== دیتابیس ====================
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

# ==================== طلسمات ====================
PUBLIC_TALISMANS = [
    {"کاربرد": "رزق و روزی", "طلسم": "مربع جادویی 4×4", "روز": "پنجشنبه", "ساعت": "مشتری"},
    {"کاربرد": "محبت و دوستی", "طلسم": "ستاره داوود", "روز": "جمعه", "ساعت": "زهره"},
    {"کاربرد": "دفع بلا", "طلسم": "خاتم سلیمان", "روز": "سه‌شنبه", "ساعت": "مریخ"},
    {"کاربرد": "گشایش کار", "طلسم": "وفق ابواب الفرج", "روز": "پنجشنبه", "ساعت": "مشتری"},
    {"کاربرد": "شفای بیماری", "طلسم": "حرز شفا", "روز": "دوشنبه", "ساعت": "ماه"},
]

PROTECTED_TALISMANS = [
    {"کاربرد": "قدرت و غلبه", "طلسم": "مربع جادویی 4×4 قدرت", "روز": "یکشنبه", "ساعت": "شمس", "درجه": "ویژه"},
    {"کاربرد": "الفت بین زوجین", "طلسم": "طلسم الفت", "روز": "جمعه", "ساعت": "زهره", "درجه": "بسیار ویژه"},
    {"کاربرد": "بخت گشایی", "طلسم": "طلسم الفتح العظیم", "روز": "پنجشنبه", "ساعت": "مشتری", "درجه": "نادر"},
    {"کاربرد": "تسخیر قلوب", "طلسم": "خاتم سلیمانی", "روز": "دوشنبه", "ساعت": "عطارد", "درجه": "فوق‌العاده"},
    {"کاربرد": "دفع سحر", "طلسم": "حرز امان", "روز": "سه‌شنبه", "ساعت": "زحل", "درجه": "ویژه"},
]

NEW_MAGIC_SQUARES = [
    {"نام": "مثلث خالی الوسط", "ابعاد": "3×3"},
    {"نام": "مخمسه خالی الوسط", "ابعاد": "5×5"},
]

NEW_SHAPES = [
    {"نام": "دایره جادویی", "کاربرد": "طلسمات محافظتی"},
    {"نام": "مثلث جادویی", "کاربرد": "طلسمات تسخیر"},
    {"نام": "ستاره داوود", "کاربرد": "طلسمات محبت"},
]

NEW_ADKAR = [
    {"نام": "ذکر 'یا حی یا قیوم'", "تعداد": 1000, "کاربرد": "طی الارض"},
    {"نام": "اسم 'الوهاب'", "تعداد": 4, "کاربرد": "رزق"},
    {"نام": "دعوة الجلجلوتية الصغرى", "کاربرد": "تسخیر"},
]

def get_talismans(with_protected: bool = False, password: str = None) -> List[Dict]:
    talismans = PUBLIC_TALISMANS.copy()
    
    if with_protected:
        if password and talisman_protector.unlock(password):
            talismans.extend(PROTECTED_TALISMANS)
            logger.info("🔓 طلسمات ویژه به لیست اضافه شد")
        elif not talisman_protector.is_unlocked():
            logger.warning("🔒 برای دیدن طلسمات ویژه، رمز ۱۳۶۴۰۶۲۴ را وارد کنید")
    
    return talismans

# ==================== فال قهوه ====================
COFFEE_SYMBOLS = {
    "انگشتر": {"meaning": "ازدواج یا نامزدی در راه است", "type": "خوب", "score": 85},
    "قلب": {"meaning": "عشق و محبت حقیقی به زودی وارد زندگی می‌شود", "type": "خوب", "score": 90},
    "تاج": {"meaning": "موفقیت و افتخار بزرگ در انتظار شماست", "type": "خوب", "score": 95},
    "خورشید": {"meaning": "موفقیت، خوشبختی و روشنایی کامل", "type": "خوب", "score": 98},
    "ستاره": {"meaning": "آرزوها برآورده می‌شود، شانس و اقبال", "type": "خوب", "score": 92},
    "گل": {"meaning": "خوشبختی بزرگ، دوستان خوب و عشق پایدار", "type": "خوب", "score": 88},
    "ماهی": {"meaning": "خبرهای خوب از کشور دیگر، رزق و روزی", "type": "خوب", "score": 85},
    "پرنده": {"meaning": "شانس خوب، احتمالاً سفر خوب", "type": "خوب", "score": 80},
    "مار": {"meaning": "دشمن پنهان، احتیاط کنید", "type": "بد", "score": 25},
    "عقرب": {"meaning": "خطر نزدیک است", "type": "بد", "score": 20},
    "کلاغ": {"meaning": "خبر بد در راه است", "type": "بد", "score": 15},
    "تابوت": {"meaning": "بیماری طولانی یا خبر تلخ", "type": "بد", "score": 10},
    "شتر": {"meaning": "صبر و استقامت، موفقیت دیر اما پایدار", "type": "متوسط", "score": 50},
    "کوه": {"meaning": "موانع بزرگ، اما با تلاش قابل عبور", "type": "متوسط", "score": 45},
    "درخت": {"meaning": "رشد، برکت، نعمت پایدار", "type": "خوب", "score": 82},
    "چشم": {"meaning": "حسادت، باید محافظت کنید", "type": "بد", "score": 30},
    "کلید": {"meaning": "گشایش کارها، راه حل مشکلات", "type": "خوب", "score": 87},
    "قفل": {"meaning": "موانع، مشکلات، نیاز به صبر", "type": "بد", "score": 35},
    "کتاب": {"meaning": "دانش، علم، موفقیت تحصیلی", "type": "خوب", "score": 86},
    "قلم": {"meaning": "نوشتن، امضا، قرارداد مهم", "type": "خوب", "score": 84},
    "خانه": {"meaning": "آرامش، ثبات، زندگی خوب", "type": "خوب", "score": 83},
    "ماشین": {"meaning": "سفر، تغییر مکان", "type": "متوسط", "score": 55},
    "تپانچه": {"meaning": "دعوا، درگیری، خطر", "type": "بد", "score": 18},
    "خنجر": {"meaning": "خیانت، دشمنی نزدیک", "type": "بد", "score": 12},
    "زن": {"meaning": "خبر از طرف یک زن", "type": "متوسط", "score": 60},
    "مرد": {"meaning": "خبر از طرف یک مرد", "type": "متوسط", "score": 60},
    "فرشته": {"meaning": "خبر خوب، کمک الهی", "type": "خوب", "score": 96},
    "صلیب": {"meaning": "رنج، فداکاری، اما پیروزی نهایی", "type": "متوسط", "score": 48},
    "دایره": {"meaning": "چرخه کامل، پایان خوب", "type": "خوب", "score": 78},
    "مربع": {"meaning": "ثبات، امنیت، زندان", "type": "متوسط", "score": 52},
    "مثلث": {"meaning": "تغییر، تحول، موفقیت", "type": "خوب", "score": 75}
}

def coffee_reading(symbols: List[str]) -> Dict:
    results = []
    total_score = 0
    good_count = 0
    bad_count = 0
    
    for symbol in symbols:
        if symbol in COFFEE_SYMBOLS:
            info = COFFEE_SYMBOLS[symbol]
            results.append({
                "symbol": symbol,
                "meaning": info["meaning"],
                "type": info["type"],
                "score": info["score"]
            })
            total_score += info["score"]
            if info["type"] == "خوب":
                good_count += 1
            elif info["type"] == "بد":
                bad_count += 1
    
    avg_score = total_score // len(symbols) if symbols else 50
    
    if good_count > bad_count + 1:
        overall = "بسیار خوب"
        advice = "فال شما بسیار خوب است. به زودی خبرهای خوشی خواهید شنید."
    elif good_count > bad_count:
        overall = "خوب"
        advice = "فال شما خوب است. با کمی تلاش به خواسته خود می‌رسید."
    elif bad_count > good_count:
        overall = "نسبتاً بد"
        advice = "فال شما چندان خوب نیست. احتیاط کنید و صبر پیشه کنید."
    else:
        overall = "متوسط"
        advice = "فال شما متوسط است. نتیجه به تلاش شما بستگی دارد."
    
    if "مار" in symbols or "عقرب" in symbols:
        advice += " مراقب اطرافیان خود باشید."
    if "تاج" in symbols or "خورشید" in symbols:
        advice += " به زودی خبر بسیار خوبی می‌شنوید."
    if "تابوت" in symbols:
        advice += " به سلامت خود بیشتر توجه کنید."
    
    return {
        "type": "coffee_reading",
        "symbols_found": symbols,
        "detailed_results": results,
        "average_score": avg_score,
        "overall": overall,
        "advice": advice
    }

# ==================== کف‌بینی ====================
PALM_LINES = {
    "heart_line": {
        "name": "خط قلب",
        "positions": ["مستقیم", "کج", "بلند", "کوتاه", "شاخه‌دار", "زنجیره‌ای", "منقطع"],
        "meanings": {
            "مستقیم": "عشق منطقی، احساسات کنترل شده، روابط پایدار",
            "کج": "عشق رمانتیک، احساساتی، دلباخته",
            "بلند": "عشق عمیق، وفاداری بالا",
            "کوتاه": "عشق سطحی، تمرکز روی خود",
            "شاخه‌دار": "چندین رابطه عشقی، موفقیت در عشق",
            "زنجیره‌ای": "مشکلات عاطفی، دلبستگی‌های متعدد",
            "منقطع": "شکست عشقی، جدایی"
        }
    },
    "head_line": {
        "name": "خط سر",
        "positions": ["مستقیم", "کج", "بلند", "کوتاه", "منقطع", "مواج", "شاخه‌دار"],
        "meanings": {
            "مستقیم": "تفکر منطقی، عملگرا، واقع‌بین",
            "کج": "خلاق، هنرمند، خیال‌پرداز",
            "بلند": "هوش بالا، تمرکز خوب، موفقیت تحصیلی",
            "کوتاه": "تصمیمات سریع، عمل‌گرایی، کم‌حوصله",
            "منقطع": "حواس‌پرتی، عدم تمرکز، فراموشکاری",
            "مواج": "تفکر نامنظم، ذهن خلاق اما آشفته",
            "شاخه‌دار": "استعدادهای چندگانه، موفقیت در چند زمینه"
        }
    },
    "life_line": {
        "name": "خط زندگی",
        "positions": ["طولانی", "متوسط", "کوتاه", "منقطع", "دوگانه", "زنجیره‌ای", "عمیق"],
        "meanings": {
            "طولانی": "عمر طولانی، سلامت خوب، انرژی بالا",
            "متوسط": "عمر معمولی، سلامت متوسط",
            "کوتاه": "نیاز به مراقبت از سلامت، پرانرژی اما زودرس",
            "منقطع": "تغییرات ناگهانی در زندگی، بیماری‌های متناوب",
            "دوگانه": "حمایت قوی، زندگی امن، محافظت شده",
            "زنجیره‌ای": "سلامت ضعیف، استرس بالا، دوران سخت",
            "عمیق": "سلامت قوی، انرژی حیاتی بالا"
        }
    },
    "fate_line": {
        "name": "خط سرنوشت",
        "positions": ["مستقیم", "منقطع", "شاخه‌دار", "کوتاه", "عمیق", "ضعیف", "مبتدی"],
        "meanings": {
            "مستقیم": "مسیر شغلی روشن، موفقیت پایدار",
            "منقطع": "تغییر شغل، مشکلات موقتی",
            "شاخه‌دار": "چندین منبع درآمد، موفقیت در چند زمینه",
            "کوتاه": "موفقیت دیرهنگام، تلاش بیشتر نیاز دارد",
            "عمیق": "موفقیت بزرگ، سرنوشت مشخص",
            "ضعیف": "نیاز به تلاش بیشتر، عدم تمرکز",
            "مبتدی": "شروع دیرهنگام حرفه، تغییر مسیر"
        }
    }
}

PALM_MOUNTS = {
    "mount_jupiter": {"name": "کوه مشتری", "meaning": "رهبری، جاه‌طلبی، اعتماد به نفس"},
    "mount_saturn": {"name": "کوه زحل", "meaning": "مسئولیت، جدیت، خرد"},
    "mount_apollo": {"name": "کوه آپولو", "meaning": "خلاقیت، هنر، خوشبختی"},
    "mount_mercury": {"name": "کوه عطارد", "meaning": "تجارت، هوش، ارتباطات"},
    "mount_venus": {"name": "کوه زهره", "meaning": "عشق، هنر، لذت‌های زندگی"},
    "mount_mars": {"name": "کوه مریخ", "meaning": "شجاعت، قدرت، تهاجم مثبت"},
    "mount_luna": {"name": "کوه ماه", "meaning": "خیال، سفر، احساسات"}
}

def palm_reading(lines: Dict[str, str], mounts: List[str]) -> Dict:
    results = {
        "lines_interpretation": {},
        "mounts_interpretation": [],
        "overall_personality": "",
        "strengths": [],
        "weaknesses": [],
        "advice": ""
    }
    
    for line_key, position in lines.items():
        if line_key in PALM_LINES and position in PALM_LINES[line_key]["meanings"]:
            results["lines_interpretation"][line_key] = {
                "name": PALM_LINES[line_key]["name"],
                "position": position,
                "meaning": PALM_LINES[line_key]["meanings"][position]
            }
    
    for mount in mounts:
        if mount in PALM_MOUNTS:
            results["mounts_interpretation"].append({
                "name": PALM_MOUNTS[mount]["name"],
                "meaning": PALM_MOUNTS[mount]["meaning"]
            })
    
    strengths = []
    weaknesses = []
    
    heart = results["lines_interpretation"].get("heart_line", {})
    head = results["lines_interpretation"].get("head_line", {})
    life = results["lines_interpretation"].get("life_line", {})
    
    if head.get("position") in ["بلند", "مستقیم"]:
        strengths.append("هوش بالا و تفکر منطقی")
    if heart.get("position") in ["بلند", "شاخه‌دار"]:
        strengths.append("وفاداری بالا در روابط عاطفی")
    if life.get("position") in ["طولانی", "عمیق", "دوگانه"]:
        strengths.append("سلامت خوب و انرژی حیاتی قوی")
    
    if head.get("position") == "منقطع":
        weaknesses.append("حواس‌پرتی و عدم تمرکز")
    if heart.get("position") == "منقطع":
        weaknesses.append("مشکلات عاطفی و احساسی")
    if life.get("position") in ["کوتاه", "زنجیره‌ای"]:
        weaknesses.append("نیاز به مراقبت بیشتر از سلامت")
    
    results["strengths"] = strengths
    results["weaknesses"] = weaknesses
    
    if "کوه مشتری" in [m["name"] for m in results["mounts_interpretation"]]:
        results["advice"] = "از توانایی رهبری خود استفاده کنید اما مغرور نشوید."
    elif "کوه زهره" in [m["name"] for m in results["mounts_interpretation"]]:
        results["advice"] = "از هنر و خلاقیت خود برای پیشرفت استفاده کنید."
    else:
        results["advice"] = "روی نقاط قوت خود تمرکز کنید و نقاط ضعف را بهبود بخشید."
    
    results["overall_personality"] = f"شخصیتی با {len(strengths)} نقطه قوت و {len(weaknesses)} نقطه ضعف"
    
    return results

# ==================== فال حافظ ====================
HAFEZ_GHAZALS = [
    {"ghazal_id": 1, "opening_beyt": "الا یا ایها الساقی ادر کاسا و ناولها", "interpretation": "زندگی را آسان بگیر، شادی و نشاط را به خود راه بده"},
    {"ghazal_id": 2, "opening_beyt": "صبا به لطف بگو آن غزال رعنا را", "interpretation": "عشق و محبت را جستجو کن، عاشقی صادق باش"},
    {"ghazal_id": 3, "opening_beyt": "اگر آن ترک شیرازی به دست آرد دل ما را", "interpretation": "عشق واقعی در راه است، آماده باش"},
    {"ghazal_id": 4, "opening_beyt": "دوش دیدم که ملائک در میخانه زدند", "interpretation": "عشق الهی، معنویت و پاکی"},
    {"ghazal_id": 5, "opening_beyt": "سخن عشق تو بی‌آفتاب، پیدا نیست", "interpretation": "عشق را با صداقت دنبال کن"},
    {"ghazal_id": 6, "opening_beyt": "به بوی نافهای کاخر صبا ز خط بیاورد", "interpretation": "خبر خوش در راه است"},
    {"ghazal_id": 7, "opening_beyt": "صوفی بیا که خرقهٔ زهدت به باد رفت", "interpretation": "از سخت‌گیری دست بردار"},
    {"ghazal_id": 8, "opening_beyt": "دلا برو که ز ره باز پس نمی‌آیی", "interpretation": "به راه خود ادامه بده"}
]

def hafez_fal(niyat: str = "") -> Dict:
    selected = random.choice(HAFEZ_GHAZALS)
    
    additional_interpretation = ""
    if "عشق" in niyat or "ازدواج" in niyat:
        additional_interpretation = "فال شما در امور عشقی بسیار نیک است. حافظ می‌گوید عشق حقیقی را جستجو کن."
    elif "کار" in niyat or "شغل" in niyat:
        additional_interpretation = "در امور شغلی، صبور باش و به تلاشت ادامه بده. موفقیت نزدیک است."
    elif "درس" in niyat or "تحصیل" in niyat:
        additional_interpretation = "طلب علم و دانش، راهگشای تو خواهد بود."
    else:
        additional_interpretation = "زندگی را با شادی و آرامش ادامه بده. حافظ تو را به خوشباشی دعوت می‌کند."
    
    return {
        "type": "hafez_fal",
        "ghazal_id": selected["ghazal_id"],
        "opening_beyt": selected["opening_beyt"],
        "interpretation": selected["interpretation"],
        "additional_interpretation": additional_interpretation if niyat else "",
        "niyat": niyat if niyat else "بدون نیت خاص",
        "advice": "به شعله‌ی شمع دل خود اعتماد کن و به راهت ادامه بده."
    }

# ==================== فال قرآن ====================
QURAN_VERSES = [
    {"surah": "فاتحه", "verse": 1, "text": "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ", "interpretation": "شروع به نام خدا، کار تو خیر و برکت دارد"},
    {"surah": "الرحمن", "verse": 1, "text": "الرَّحْمَٰنُ", "interpretation": "خداوند رحمان، مهربانی شامل حالت می‌شود"},
    {"surah": "یس", "verse": 1, "text": "يس", "interpretation": "سوره قلب قرآن، نشانه اجابت دعا"},
    {"surah": "الضحی", "verse": 1, "text": "وَالضُّحَىٰ", "interpretation": "سوگند به روشنایی روز، امید و روشنایی"},
    {"surah": "الشرح", "verse": 1, "text": "أَلَمْ نَشْرَحْ لَكَ صَدْرَكَ", "interpretation": "آیا سینهات را نگشادیم؟ گشایش در کارها"},
    {"surah": "التین", "verse": 1, "text": "وَالتِّينِ وَالزَّيْتُونِ", "interpretation": "قسم به انجیر و زیتون، برکت و سلامت"},
    {"surah": "العلق", "verse": 1, "text": "اقْرَأْ بِاسْمِ رَبِّكَ الَّذِي خَلَقَ", "interpretation": "بخوان به نام پروردگارت، علم و دانش"},
    {"surah": "القدر", "verse": 1, "text": "إِنَّا أَنزَلْنَاهُ فِي لَيْلَةِ الْقَدْرِ", "interpretation": "شب قدر بهتر از هزار ماه، اجابت دعا"},
    {"surah": "الکوثر", "verse": 1, "text": "إِنَّا أَعْطَيْنَاكَ الْكَوْثَرَ", "interpretation": "خیر کثیر به تو دادیم، برکت و نعمت"},
    {"surah": "الاخلاص", "verse": 1, "text": "قُلْ هُوَ اللَّهُ أَحَدٌ", "interpretation": "توحید خالص، ایمان و یقین"}
]

def quran_fal(question: str = "") -> Dict:
    selected = random.choice(QURAN_VERSES)
    
    specific_interpretation = ""
    if "ازدواج" in question:
        specific_interpretation = "ازدواج تو خیر و برکت دارد. با توکل بر خدا اقدام کن."
    elif "کار" in question or "شغل" in question:
        specific_interpretation = "کار تو رزق حلال دارد. به تلاشت ادامه بده."
    elif "سفر" in question:
        specific_interpretation = "سفر تو با سلامت و امانت خواهد بود."
    elif "درس" in question or "تحصیل" in question:
        specific_interpretation = "طلب علم عبادت است. موفق خواهی شد."
    else:
        specific_interpretation = "به راه خود ادامه بده. خداوند یاور توست."
    
    return {
        "type": "quran_fal",
        "surah": selected["surah"],
        "verse_number": selected["verse"],
        "verse_text": selected["text"],
        "interpretation": selected["interpretation"],
        "specific_interpretation": specific_interpretation if question else "",
        "question": question if question else "عمومی",
        "advice": "به قرآن ایمان داشته باش و به راهت ادامه بده."
    }

# ==================== فال تاروت ====================
TAROT_MAJOR_ARCANA = {
    0: {"name": "The Fool", "persian": "دیوانه", "meaning": "شروع جدید، ماجراجویی", "upright": "آزادی، پتانسیل"},
    1: {"name": "The Magician", "persian": "شعبده‌باز", "meaning": "اراده، تمرکز، قدرت عمل", "upright": "توانایی، خلاقیت"},
    2: {"name": "The High Priestess", "persian": "کاهنه اعظم", "meaning": "شهود، اسرار، دانش پنهان", "upright": "حکمت درونی"},
    3: {"name": "The Empress", "persian": "ملکه", "meaning": "عشق، خلاقیت، باروری", "upright": "شکوفایی، زیبایی"},
    4: {"name": "The Emperor", "persian": "پادشاه", "meaning": "اقتدار، ساختار، رهبری", "upright": "قدرت، ثبات"},
    5: {"name": "The Hierophant", "persian": "پاپ", "meaning": "سنت، آموزش، معنویت", "upright": "راهنمایی، باور"},
    6: {"name": "The Lovers", "persian": "عاشقان", "meaning": "عشق، انتخاب، هماهنگی", "upright": "اتحاد، تصمیم"},
    7: {"name": "The Chariot", "persian": "ارابه", "meaning": "پیروزی، اراده، کنترل", "upright": "غلبه، موفقیت"},
    8: {"name": "Strength", "persian": "قوت", "meaning": "شجاعت، شفقت، صبر", "upright": "قدرت درونی"},
    9: {"name": "The Hermit", "persian": "مرتاض", "meaning": "تنهایی، تفکر، راهنمایی", "upright": "درون‌نگری"},
    10: {"name": "Wheel of Fortune", "persian": "چرخ بخت", "meaning": "تغییر، شانس، سرنوشت", "upright": "چرخش好运"},
    11: {"name": "Justice", "persian": "عدالت", "meaning": "عدالت، حقیقت، قانون", "upright": "انصاف، مسئولیت"},
    12: {"name": "The Hanged Man", "persian": "معلق", "meaning": "تسلیم، دیدگاه جدید", "upright": "انتظار، رها کردن"},
    13: {"name": "Death", "persian": "مرگ", "meaning": "پایان، تحول، دگرگونی", "upright": "تغییر بزرگ"},
    14: {"name": "Temperance", "persian": "تعادل", "meaning": "تعادل، میانه‌روی", "upright": "هماهنگی"},
    15: {"name": "The Devil", "persian": "شیطان", "meaning": "اسارت، وسواس، مادیات", "upright": "وابستگی"},
    16: {"name": "The Tower", "persian": "برج", "meaning": "فاجعه، شکست ناگهانی", "upright": "تغییر ناگهانی"},
    17: {"name": "The Star", "persian": "ستاره", "meaning": "امید، الهام، آرامش", "upright": "بهبودی"},
    18: {"name": "The Moon", "persian": "ماه", "meaning": "توهم، شهود، ناخودآگاه", "upright": "رمز و راز"},
    19: {"name": "The Sun", "persian": "خورشید", "meaning": "شادی، موفقیت، مثبت‌اندیشی", "upright": "روشنایی"},
    20: {"name": "Judgement", "persian": "قیامت", "meaning": "باززایی، فراخوان", "upright": "بیداری"},
    21: {"name": "The World", "persian": "جهان", "meaning": "کمال، موفقیت، تحقق", "upright": "تکمیل"}
}

def tarot_reading(spread_type: str = "three_card", cards: List[int] = None) -> Dict:
    if cards is None:
        if spread_type == "one_card":
            cards = [random.randint(0, 21)]
        elif spread_type == "three_card":
            cards = random.sample(range(0, 22), 3)
        else:
            cards = random.sample(range(0, 22), 6)
    
    card_readings = []
    for i, card_num in enumerate(cards):
        if card_num in TAROT_MAJOR_ARCANA:
            card_info = TAROT_MAJOR_ARCANA[card_num]
            card_readings.append({
                "position": i + 1,
                "card_number": card_num,
                "card_name": card_info["name"],
                "card_persian": card_info["persian"],
                "meaning": card_info["meaning"],
                "upright_meaning": card_info["upright"]
            })
    
    main_card = card_readings[0] if card_readings else None
    
    if main_card:
        if main_card["card_number"] in [0, 1, 3, 6, 10, 17, 19, 21]:
            overall = "فال بسیار خوب - شادی و موفقیت در راه است"
            advice = "به راهت ادامه بده، نتایج مثبت خواهی دید."
        elif main_card["card_number"] in [13, 15, 16]:
            overall = "فال هشدار - نیاز به احتیاط و بازنگری"
            advice = "مراقب تصمیماتت باش، شاید نیاز به تغییر مسیر داشته باشی."
        else:
            overall = "فال متوسط - نتیجه به تلاش تو بستگی دارد"
            advice = "صبور باش و به تلاشت ادامه بده."
    else:
        overall = "فال انجام نشد"
        advice = "دوباره تلاش کن."
    
    return {
        "type": "tarot_reading",
        "spread_type": spread_type,
        "cards_drawn": card_readings,
        "overall": overall,
        "advice": advice
    }

# ==================== استخاره ====================
ISTIKHARA_PRAYER = """
اللَّهُمَّ إِنِّي أَسْتَخِيرُكَ بِعِلْمِكَ، وَأَسْتَقْدِرُكَ بِقُدْرَتِكَ، 
وَأَسْأَلُكَ مِنْ فَضْلِكَ الْعَظِيمِ، فَإِنَّكَ تَقْدِرُ وَلَا أَقْدِرُ، 
وَتَعْلَمُ وَلَا أَعْلَمُ، وَأَنْتَ عَلَّامُ الْغُيُوبِ.
اللَّهُمَّ إِنْ كُنْتَ تَعْلَمُ أَنَّ هَذَا الْأَمْرَ خَيْرٌ لِي فِي دِينِي وَمَعَاشِي وَعَاقِبَةِ أَمْرِي 
فَاقْدُرْهُ لِي وَيَسِّرْهُ لِي ثُمَّ بَارِكْ لِي فِيهِ.
وَإِنْ كُنْتَ تَعْلَمُ أَنَّ هَذَا الْأَمْرَ شَرٌّ لِي فِي دِينِي وَمَعَاشِي وَعَاقِبَةِ أَمْرِي 
فَاصْرِفْهُ عَنِّي وَاصْرِفْنِي عَنْهُ، وَاقْدُرْ لِيَ الْخَيْرَ حَيْثُ كَانَ ثُمَّ أَرْضِنِي بِهِ.
"""

def istikhara(issue: str) -> Dict:
    positive_keywords = ["ازدواج", "خیّر", "کمک", "خیر", "عبادت", "علم", "دانش", "کار", "شغل", "تجارت"]
    negative_keywords = ["حرام", "گناه", "شر", "دروغ", "خیانت", "ظلم", "طلاق"]
    
    score = 50
    for kw in positive_keywords:
        if kw in issue:
            score += 10
    for kw in negative_keywords:
        if kw in issue:
            score -= 15
    
    score = max(0, min(100, score))
    
    if score >= 70:
        result = "مثبت"
        sign = "✅"
        advice = "این کار برای تو خیر و برکت دارد. با توکل بر خدا اقدام کن."
        recommendation = "انجام بده"
    elif score >= 40:
        result = "متوسط"
        sign = "⚠️"
        advice = "نتیجه این کار به نیت و تلاش تو بستگی دارد. بهتر است بیشتر فکر کنی."
        recommendation = "مصمم شو و استخاره را تکرار کن"
    else:
        result = "منفی"
        sign = "❌"
        advice = "این کار برای تو خوب نیست. بهتر است منصرف شوی."
        recommendation = "انجام نده"
    
    return {
        "type": "istikhara",
        "issue": issue,
        "result": result,
        "sign": sign,
        "score": score,
        "prayer": ISTIKHARA_PRAYER,
        "advice": advice,
        "recommendation": recommendation,
        "instruction": "۲ رکعت نماز استخاره بخوان، سپس این دعا را بخوان و به علامت‌ها توجه کن."
    }

# ==================== کلاس پیشگویی نهایی ====================
class UltimatePredictor:
    def __init__(self):
        self.cache = cache
        self.db = db
        self.circuit_breaker = circuit_breaker
    
    def predict_complete(self, name: str, mother: str, day: int, month: int, year: int, 
                         question: str = None, dream: str = None, symptoms: List[str] = None,
                         hour: int = None, mode: str = "complete") -> Dict:
        start_time = time.time()
        
        cache_key = hashlib.md5(f"{name}_{mother}_{day}_{month}_{year}_{question}_{mode}".encode()).hexdigest()
        cached = self.cache.get(cache_key)
        if cached:
            cached["from_cache"] = True
            return cached
        
        zodiac = get_zodiac(day, month, year)
        lp = life_path_number(day, month, year)
        lp_info = LIFE_PATH_DATA.get(lp, LIFE_PATH_DATA[1])
        
        j36 = None
        j360 = None
        if question:
            j36 = jafar_36(question)
            if mode == "expert":
                j360 = jafar_360(question, name, mother)
        
        raml = raml_extract(name, use_16=(mode == "expert"))
        hamzad = check_hamzad(symptoms or [])
        hamzad_name_result = hamzad_name(name)
        saad_nahs = get_saad_nahs(day, month, year)
        planetary_hour = get_planetary_hour(hour or datetime.now().hour)
        
        planet = zodiac.get("planet", "شمس")
        mineral = get_mineral(planet)
        purification = get_purification_method(mineral["metal"])
        
        taksir = taksir_correct(name + mother)
        basts = basts_azizi(name, mother)
        dominant_tab = get_dominant_tab(name + mother)
        
        zayejah = None
        if question:
            zayejah = zayejah_adl(question, saad_nahs["lunar_day"], hour or datetime.now().hour)
        
        dream_interpretation = None
        if dream:
            dream_interpretation, _ = get_dream_interpretation(dream)
        
        talismans = PUBLIC_TALISMANS.copy()
        if talisman_protector.is_unlocked():
            talismans.extend(PROTECTED_TALISMANS)
        
        result = {
            "name": name,
            "mother": mother,
            "birth_date": f"{year}/{month}/{day}",
            "zodiac": f"{zodiac['name']} ({zodiac['element']})",
            "zodiac_planet": zodiac["planet"],
            "zodiac_stone": zodiac.get("stone", "نامشخص"),
            "life_path": lp,
            "life_path_name": lp_info["name"],
            "life_path_strength": lp_info["strength"],
            "life_path_weakness": lp_info["weakness"],
            "jafar_36": j36,
            "jafar_360": j360,
            "raml": raml,
            "hamzad": hamzad,
            "hamzad_name": hamzad_name_result,
            "saad_nahs": saad_nahs,
            "planetary_hour": planetary_hour,
            "mineral": mineral,
            "purification": purification,
            "taksir": taksir,
            "basts": basts,
            "dominant_tab": dominant_tab,
            "zayejah": zayejah,
            "dream_interpretation": dream_interpretation,
            "talismans": talismans,
            "new_magic_squares": NEW_MAGIC_SQUARES,
            "new_shapes": NEW_SHAPES,
            "new_adkar": NEW_ADKAR,
            "processing_time_ms": (time.time() - start_time) * 1000,
            "from_cache": False
        }
        
        self.cache.set(cache_key, result)
        return result

predictor = UltimatePredictor()

# ==================== کیبورد دائمی ====================
class BotKeyboard:
    @staticmethod
    def get_main_keyboard():
        keyboard = [
            ['🔮 جفرگیری', '📊 تاریخچه'],
            ['☕ فال قهوه', '✋ کف‌بینی'],
            ['📖 فال حافظ', '📖 فال قرآن'],
            ['🃏 فال تاروت', '🤲 استخاره'],
            ['📖 راهنما', '📈 آمار من'],
            ['🔐 طلسمات ویژه', 'ℹ️ درباره'],
            ['❌ لغو عملیات']
        ]
        return {
            'keyboard': keyboard,
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'persistent': True
        }
    
    @staticmethod
    def get_cancel_keyboard():
        keyboard = [
            ['❌ لغو عملیات']
        ]
        return {
            'keyboard': keyboard,
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'persistent': True
        }

# ==================== تلگرام ====================
class TelegramBot:
    BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
    
    @staticmethod
    def send_message(chat_id: str, text: str, parse_mode: str = 'Markdown', reply_markup: Optional[Dict] = None) -> bool:
        try:
            payload = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
            if reply_markup is None:
                reply_markup = BotKeyboard.get_main_keyboard()
            payload['reply_markup'] = json.dumps(reply_markup)
            
            response = requests.post(f"{TelegramBot.BASE_URL}/sendMessage", json=payload, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"خطا در ارسال پیام: {e}")
            return False

# ==================== مدیریت کاربر ====================
class UserManager:
    @staticmethod
    def register_user(update: Dict):
        message = update.get('message', {})
        chat = message.get('chat', {})
        chat_id = str(chat.get('id'))
        user = message.get('from', {})
        
        db.create_user(
            chat_id=chat_id,
            username=user.get('username', ''),
            first_name=user.get('first_name', ''),
            last_name=user.get('last_name', '')
        )
        return chat_id
    
    @staticmethod
    def get_stats(chat_id: str) -> str:
        user = db.get_user(chat_id)
        if not user:
            return "❌ کاربری یافت نشد."
        
        return f"""
📊 **آمار شما**

👤 کاربر: {user.get('first_name', 'ناشناس')}
📅 تاریخ ثبت: {user.get('registered_at', 'نامشخص')}
🔢 تعداد سوالات: {user.get('total_queries', 0)}
"""
    
    @staticmethod
    def get_history(chat_id: str) -> str:
        with db.get_connection() as conn:
            results = conn.execute(
                '''SELECT question, fortune_type, created_at FROM query_history 
                   WHERE chat_id = ? ORDER BY created_at DESC LIMIT 5''',
                (chat_id,)
            ).fetchall()
        
        if not results:
            return "📭 هنوز سوالی نپرسیده‌اید."
        
        history = "📜 **تاریخچه سوالات**\n\n"
        for i, row in enumerate(results, 1):
            history += f"{i}. {row['question']}\n"
            history += f"🕐 {row['created_at'][:16]}\n"
            if row['fortune_type']:
                history += f"📖 نوع: {row['fortune_type']}\n"
            history += "\n"
        return history
    
    @staticmethod
    def get_help_message() -> str:
        return """
📖 **راهنمای کامل ربات جفر + علوم غریبه**

🔮 **قابلیت‌های ربات:**
1. 🔮 جفر ۳۶ و ۳۶۰ هوشمند
2. ☕ فال قهوه
3. ✋ کف‌بینی
4. 📖 فال حافظ
5. 📖 فال قرآن
6. 🃏 فال تاروت
7. 🤲 استخاره
8. 🎲 رمل (۸ و ۱۶ شکل)
9. 👹 تشخیص همزاد
10. 🔐 طلسمات (عمومی و ویژه با رمز ۱۳۶۴۰۶۲۴)
11. ⚗️ زایجه عدل
12. 💭 تعبیر خواب
13. 📊 عدد شناسی و طالع

⚠️ **توجه:** صرفاً جنبه سرگرمی دارد.
"""
    
    @staticmethod
    def cancel_session(chat_id: str) -> str:
        session = db.get_session(chat_id)
        if session:
            db.delete_session(chat_id)
            return "❌ عملیات لغو شد."
        return "ℹ️ هیچ عملیات فعالی وجود ندارد."
    
    @staticmethod
    def process_step(chat_id: str, text: str):
        session = db.get_session(chat_id)
        if not session:
            return None, False
        
        step = session['step']
        fortune_type = session.get('fortune_type')
        
        try:
            if step == 'name':
                if len(text) < 2:
                    return "❌ نام باید حداقل ۲ حرف باشد.", False
                db.save_session(chat_id, 'mother', name=text, jafr_type=session.get('jafr_type', 'both'))
                return "👩 **نام مادر خود را وارد کنید:**", False
            
            elif step == 'mother':
                if len(text) < 2:
                    return "❌ نام مادر باید حداقل ۲ حرف باشد.", False
                db.save_session(chat_id, 'day', name=session['name'], mother=text, 
                               jafr_type=session.get('jafr_type', 'both'), fortune_type=fortune_type)
                return "📅 **روز تولد (۱ تا ۳۱):**", False
            
            elif step == 'day':
                try:
                    day = int(text)
                    if not 1 <= day <= 31:
                        return "❌ روز باید بین ۱ تا ۳۱ باشد.", False
                    db.save_session(chat_id, 'month', name=session['name'], mother=session['mother'], 
                                  day=day, jafr_type=session.get('jafr_type', 'both'), fortune_type=fortune_type)
                    return "📅 **ماه تولد (۱ تا ۱۲):**", False
                except ValueError:
                    return "❌ لطفاً یک عدد معتبر وارد کنید:", False
            
            elif step == 'month':
                try:
                    month = int(text)
                    if not 1 <= month <= 12:
                        return "❌ ماه باید بین ۱ تا ۱۲ باشد.", False
                    db.save_session(chat_id, 'year', name=session['name'], mother=session['mother'],
                                  day=session['day'], month=month, jafr_type=session.get('jafr_type', 'both'),
                                  fortune_type=fortune_type)
                    return "📅 **سال تولد (۱۳۰۰ تا ۱۵۰۰):**", False
                except ValueError:
                    return "❌ لطفاً یک عدد معتبر وارد کنید:", False
            
            elif step == 'year':
                try:
                    year = int(text)
                    if not 1300 <= year <= 1500:
                        return "❌ سال باید بین ۱۳۰۰ تا ۱۵۰۰ باشد.", False
                    db.save_session(chat_id, 'question', name=session['name'], mother=session['mother'],
                                  day=session['day'], month=session['month'], year=year, 
                                  jafr_type=session.get('jafr_type', 'both'), fortune_type=fortune_type)
                    return "❓ **سوال خود را بپرسید:**", False
                except ValueError:
                    return "❌ لطفاً یک عدد معتبر وارد کنید:", False
            
            elif step == 'question':
                if fortune_type == 'coffee':
                    return UserManager.do_coffee_fortune(chat_id, text), True
                elif fortune_type == 'hafez':
                    return UserManager.do_hafez_fal(chat_id, text), True
                elif fortune_type == 'quran':
                    return UserManager.do_quran_fal(chat_id, text), True
                elif fortune_type == 'istikhara':
                    return UserManager.do_istikhara(chat_id, text), True
                elif fortune_type == 'palm':
                    return UserManager.do_palm_reading(chat_id, text), True
                else:
                    return UserManager.calculate_jafr(chat_id, text), True
            
            return None, False
            
        except Exception as e:
            logger.error(f"خطا: {e}")
            return "⚠️ خطایی رخ داد. دوباره تلاش کنید.", False
    
    @staticmethod
    def calculate_jafr(chat_id: str, question: str) -> str:
        session = db.get_session(chat_id)
        if not session:
            return "❌ جلسه منقضی شده."
        
        name = session['name']
        mother = session['mother']
        day = session['day']
        month = session['month']
        year = session['year']
        jafr_type = session.get('jafr_type', 'both')
        
        result = predictor.predict_complete(name, mother, day, month, year, question)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, question, result.get('jafar_36'), result.get('jafar_360'))
        db.delete_session(chat_id)
        
        response = f"""
🔮 **نتیجه جفر برای {name}**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        if jafr_type in ['36', 'both'] and result.get('jafar_36'):
            j36 = result['jafar_36']
            response += f"""
📖 **جفر ۳۶**
{j36['answer']}
⭐ امتیاز: {j36['score']}/100
💡 توصیه: {j36['advice']}
"""
        
        if jafr_type in ['360', 'both'] and result.get('jafar_360'):
            j360 = result['jafar_360']
            response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖 **جفر ۳۶۰**
{j360['answer']}
⭐ امتیاز: {j360['score']}/100
📊 درجه: {j360.get('degree', '---')}
💡 توصیه: {j360['advice']}
"""
        
        # اضافه کردن اطلاعات تکمیلی
        response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎲 **پیشگویی رملی**
شکل: {result['raml']['sign']} {result['raml']['shape']}
معنی: {result['raml']['meaning']}

👹 **همزاد**
اسم ملکی: {result['hamzad_name']['malaki']}
اسم جنی: {result['hamzad_name']['jinni']}

⚗️ **زایجه عدل**
طبع: {result['zayejah']['tab']}
تعبیر: {result['zayejah']['text']}

⭐ **عدد سرنوشت:** {result['life_path']} - {result['life_path_name']}
💎 **سنگ:** {result['zodiac_stone']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_coffee_fortune(chat_id: str, symbols_text: str) -> str:
        symbols = [s.strip() for s in symbols_text.split(',')]
        result = coffee_reading(symbols)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, symbols_text, {}, {}, "coffee", result)
        db.delete_session(chat_id)
        
        response = f"""
☕ **فال قهوه**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **نمادهای دیده شده:**
{', '.join(symbols)}

📊 **نتیجه کلی:**
{result['overall']}

⭐ **امتیاز: {result['average_score']}/100**

💡 **توصیه:**
{result['advice']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_palm_reading(chat_id: str, text: str) -> str:
        # پارس کردن خطوط
        lines_input = {}
        for line in text.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                lines_input[key.strip()] = value.strip()
        
        result = palm_reading(lines_input, [])
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, text, {}, {}, "palm", result)
        db.delete_session(chat_id)
        
        response = f"""
✋ **کف‌بینی**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        for line_key, info in result['lines_interpretation'].items():
            response += f"\n📝 **{info['name']}**\n"
            response += f"   حالت: {info['position']}\n"
            response += f"   معنی: {info['meaning']}\n"
        
        response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔮 **شخصیت کلی:**
{result['overall_personality']}

💪 **نقاط قوت:**
"""
        for strength in result['strengths']:
            response += f"   ✅ {strength}\n"
        
        response += f"\n⚠️ **نقاط ضعف:**\n"
        for weakness in result['weaknesses']:
            response += f"   ❌ {weakness}\n"
        
        response += f"""
💡 **توصیه:**
{result['advice']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_hafez_fal(chat_id: str, niyat: str) -> str:
        result = hafez_fal(niyat)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, niyat, {}, {}, "hafez", result)
        db.delete_session(chat_id)
        
        response = f"""
📖 **فال حافظ**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **نیت شما:** {niyat if niyat else 'بدون نیت خاص'}

🕌 **غزل شماره {result['ghazal_id']}**
«{result['opening_beyt']}»

📖 **تعبیر:**
{result['interpretation']}

{result['additional_interpretation'] if result['additional_interpretation'] else ''}

💡 **توصیه:**
{result['advice']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_quran_fal(chat_id: str, question: str) -> str:
        result = quran_fal(question)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, question, {}, {}, "quran", result)
        db.delete_session(chat_id)
        
        response = f"""
📖 **فال قرآن**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **سوال شما:** {question if question else 'عمومی'}

🕌 **سوره {result['surah']} - آیه {result['verse_number']}**
«{result['verse_text']}»

📖 **تعبیر:**
{result['interpretation']}

{result['specific_interpretation'] if result['specific_interpretation'] else ''}

💡 **توصیه:**
{result['advice']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_tarot_fortune(chat_id: str, spread_type: str = "three_card") -> str:
        result = tarot_reading(spread_type)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, spread_type, {}, {}, "tarot", result)
        
        response = f"""
🃏 **فال تاروت**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 **نوع چیدمان:** {spread_type}

🎴 **کارت‌های انتخاب شده:**
"""
        for card in result['cards_drawn']:
            response += f"\n{card['position']}. {card['card_persian']} ({card['card_name']})\n"
            response += f"   معنی: {card['meaning']}\n"
            response += f"   تعبیر: {card['upright_meaning']}\n"
        
        response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔮 **نتیجه کلی:**
{result['overall']}

💡 **توصیه:**
{result['advice']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def do_istikhara(chat_id: str, issue: str) -> str:
        result = istikhara(issue)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, issue, {}, {}, "istikhara", result)
        db.delete_session(chat_id)
        
        response = f"""
🤲 **استخاره**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **موضوع:** {issue}

{result['sign']} **نتیجه: {result['result']}**
⭐ **امتیاز: {result['score']}/100**

📖 **دعای استخاره:**
{result['prayer']}

💡 **توصیه:**
{result['advice']}

📌 **پیشنهاد:**
{result['recommendation']}

📝 **دستورالعمل:**
{result['instruction']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response
    
    @staticmethod
    def get_talismans_info(chat_id: str, password: str = None) -> str:
        if password:
            talisman_protector.unlock(password)
        
        talismans = get_talismans(with_protected=talisman_protector.is_unlocked(), password=password)
        
        response = f"""
🔐 **طلسمات**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        if talisman_protector.is_unlocked():
            response += "🔓 **طلسمات ویژه فعال است**\n\n"
        else:
            response += "🔒 **طلسمات ویژه قفل است**\nبرای فعال‌سازی رمز ۱۳۶۴۰۶۲۴ را وارد کنید\n\n"
        
        for t in talismans:
            response += f"📜 **{t.get('طلسم', '')}**\n"
            response += f"   کاربرد: {t.get('کاربرد', '')}\n"
            response += f"   روز: {t.get('روز', '')} - ساعت: {t.get('ساعت', '')}\n"
            if t.get('درجه'):
                response += f"   درجه: {t['درجه']}\n"
            response += "\n"
        
        response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""
        return response

# ==================== وب‌هوک ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        if not update:
            return jsonify({'status': 'ok'}), 200
        
        if 'message' in update:
            message = update['message']
            chat_id = str(message['chat']['id'])
            text = message.get('text', '').strip()
            
            UserManager.register_user(update)
            
            # دستورات
            if text == '/start' or text == '/menu':
                TelegramBot.send_message(
                    chat_id,
                    "🔮 **ربات جفر + علوم غریبه**\n\nسلام! 👋\nاین ربات شامل:\n• جفر ۳۶ و ۳۶۰\n• ☕ فال قهوه\n• ✋ کف‌بینی\n• 📖 فال حافظ\n• 📖 فال قرآن\n• 🃏 فال تاروت\n• 🤲 استخاره\n• 🔐 طلسمات ویژه (رمز: ۱۳۶۴۰۶۲۴)\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                    reply_markup=BotKeyboard.get_main_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '/ask':
                db.save_session(chat_id, 'name', jafr_type='both')
                TelegramBot.send_message(
                    chat_id,
                    "👤 **نام خود را وارد کنید:**",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            # دکمه‌های کیبورد
            elif text == '🔮 جفرگیری':
                db.save_session(chat_id, 'name', jafr_type='both')
                TelegramBot.send_message(
                    chat_id,
                    "👤 **نام خود را وارد کنید:**",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '☕ فال قهوه':
                db.save_session(chat_id, 'coffee_symbols', fortune_type='coffee')
                TelegramBot.send_message(
                    chat_id,
                    "☕ **فال قهوه**\n\nلطفاً نمادهایی که در ته فنجان دیده‌اید را با ویرگول (,) جدا کنید:\n\nمثال: قلب, ستاره, تاج",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '✋ کف‌بینی':
                db.save_session(chat_id, 'palm_lines', fortune_type='palm')
                TelegramBot.send_message(
                    chat_id,
                    "✋ **کف‌بینی**\n\nلطفاً وضعیت خطوط کف دست را وارد کنید:\n\nمثال:\nheart_line: بلند\nhead_line: مستقیم\nlife_line: طولانی",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📖 فال حافظ':
                db.save_session(chat_id, 'hafez_niyat', fortune_type='hafez')
                TelegramBot.send_message(
                    chat_id,
                    "📖 **فال حافظ**\n\nنیت یا سوال خود را وارد کنید (اختیاری):",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📖 فال قرآن':
                db.save_session(chat_id, 'quran_question', fortune_type='quran')
                TelegramBot.send_message(
                    chat_id,
                    "📖 **فال قرآن**\n\nسوال یا حاجت خود را وارد کنید (اختیاری):",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '🃏 فال تاروت':
                result = UserManager.do_tarot_fortune(chat_id, "three_card")
                TelegramBot.send_message(chat_id, result)
                return jsonify({'status': 'ok'}), 200
            
            elif text == '🤲 استخاره':
                db.save_session(chat_id, 'istikhara_issue', fortune_type='istikhara')
                TelegramBot.send_message(
                    chat_id,
                    "🤲 **استخاره**\n\nموضوع مورد نظر برای استخاره را وارد کنید:\n\nمثال: ازدواج با فلان شخص",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '🔐 طلسمات ویژه':
                TelegramBot.send_message(
                    chat_id,
                    "🔐 **طلسمات ویژه**\n\nبرای دیدن طلسمات ویژه، رمز را وارد کنید:\n(رمز: ۱۳۶۴۰۶۲۴)\n\nیا اگر رمز ندارید، طلسمات عمومی را ببینید.",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                db.save_session(chat_id, 'talisman_password')
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📊 تاریخچه':
                response = UserManager.get_history(chat_id)
                TelegramBot.send_message(chat_id, response)
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📖 راهنما':
                TelegramBot.send_message(chat_id, UserManager.get_help_message())
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📈 آمار من':
                response = UserManager.get_stats(chat_id)
                TelegramBot.send_message(chat_id, response)
                return jsonify({'status': 'ok'}), 200
            
            elif text == 'ℹ️ درباره':
                TelegramBot.send_message(
                    chat_id,
                    "ℹ️ **درباره ربات**\n\nنسخه ۵.۰.۰ کامل\nربات جفر + علوم غریبه\n\n🔮 **ویژگی‌ها:**\n• جفر ۳۶ و ۳۶۰\n• ☕ فال قهوه\n• ✋ کف‌بینی\n• 📖 فال حافظ\n• 📖 فال قرآن\n• 🃏 فال تاروت\n• 🤲 استخاره\n• 🎲 رمل\n• 👹 همزاد\n• 🔐 طلسمات\n• ⚗️ زایجه عدل\n• 💭 تعبیر خواب\n• 📊 عددشناسی"
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '❌ لغو عملیات':
                response = UserManager.cancel_session(chat_id)
                TelegramBot.send_message(chat_id, response)
                return jsonify({'status': 'ok'}), 200
            
            # پردازش مراحل
            session = db.get_session(chat_id)
            if session:
                step = session.get('step')
                
                if step == 'talisman_password':
                    response = UserManager.get_talismans_info(chat_id, text)
                    db.delete_session(chat_id)
                    TelegramBot.send_message(chat_id, response)
                    return jsonify({'status': 'ok'}), 200
                
                elif step in ['coffee_symbols', 'hafez_niyat', 'quran_question', 'istikhara_issue', 'palm_lines']:
                    if step == 'coffee_symbols':
                        result = UserManager.do_coffee_fortune(chat_id, text)
                    elif step == 'hafez_niyat':
                        result = UserManager.do_hafez_fal(chat_id, text)
                    elif step == 'quran_question':
                        result = UserManager.do_quran_fal(chat_id, text)
                    elif step == 'istikhara_issue':
                        result = UserManager.do_istikhara(chat_id, text)
                    elif step == 'palm_lines':
                        result = UserManager.do_palm_reading(chat_id, text)
                    db.delete_session(chat_id)
                    TelegramBot.send_message(chat_id, result)
                    return jsonify({'status': 'ok'}), 200
                
                # مراحل جفر
                elif step in ['name', 'mother', 'day', 'month', 'year', 'question']:
                    response, is_complete = UserManager.process_step(chat_id, text)
                    if response:
                        if is_complete:
                            TelegramBot.send_message(chat_id, response)
                        else:
                            if step in ['name', 'mother', 'day', 'month', 'year']:
                                TelegramBot.send_message(
                                    chat_id,
                                    response,
                                    reply_markup=BotKeyboard.get_cancel_keyboard()
                                )
                            else:
                                TelegramBot.send_message(chat_id, response)
                    return jsonify({'status': 'ok'}), 200
            
            # دستور نامشخص
            TelegramBot.send_message(
                chat_id,
                "🤔 دستور نامشخص. لطفاً از دکمه‌های پایین استفاده کنید.",
                reply_markup=BotKeyboard.get_main_keyboard()
            )
            return jsonify({'status': 'ok'}), 200
        
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        logger.error(f"خطا: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== صفحه اصلی ====================
@app.route('/')
def home():
    return """
    <h1>🔮 ربات جفر + علوم غریبه</h1>
    <p>ربات آنلاین و فعال است ✅</p>
    <p>🔮 جفر ۳۶ و ۳۶۰ | ☕ فال قهوه | ✋ کف‌بینی | 📖 فال حافظ | 📖 فال قرآن | 🃏 فال تاروت | 🤲 استخاره | 🔐 طلسمات</p>
    <p>نسخه ۵.۰.۰ کامل</p>
    """

# ==================== منوی پایین ====================
def set_bot_commands():
    try:
        commands = [
            {"command": "start", "description": "🔄 شروع مجدد"},
            {"command": "ask", "description": "🔮 جفرگیری"},
            {"command": "menu", "description": "📋 منوی اصلی"},
            {"command": "history", "description": "📊 تاریخچه سوالات"},
            {"command": "stats", "description": "📈 آمار من"},
            {"command": "help", "description": "📖 راهنما"},
            {"command": "cancel", "description": "❌ لغو عملیات"}
        ]
        
        url = f"https://api.telegram.org/bot{TOKEN}/setMyCommands"
        response = requests.post(url, json={"commands": commands}, timeout=10)
        
        if response.status_code == 200:
            logger.info("✅ منوی پایین با موفقیت ثبت شد")
            return True
        else:
            logger.error(f"❌ خطا در ثبت منو: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ خطا در ثبت منو: {e}")
        return False

# ==================== اجرا ====================
if __name__ == '__main__':
    print("🔮 ثبت منوی پایین تلگرام...")
    set_bot_commands()
    
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 ربات نسخه ۵.۰.۰ کامل روی پورت {port} اجرا شد")
    app.run(host='0.0.0.0', port=port, debug=False)
# ==================== منوی کامل ====================
class BotKeyboard:
    @staticmethod
    def get_main_keyboard():
        keyboard = [
            ['🔮 جفرگیری', '📊 تاریخچه'],
            ['☕ فال قهوه', '✋ کف‌بینی'],
            ['📖 فال حافظ', '📖 فال قرآن'],
            ['🃏 فال تاروت', '🤲 استخاره'],
            ['🔐 طلسمات ویژه', '📈 آمار من'],
            ['📖 راهنما', 'ℹ️ درباره'],
            ['❌ لغو عملیات']
        ]
        return {
            'keyboard': keyboard,
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'persistent': True
        }
    
    @staticmethod
    def get_cancel_keyboard():
        keyboard = [
            ['❌ لغو عملیات']
        ]
        return {
            'keyboard': keyboard,
            'resize_keyboard': True,
            'one_time_keyboard': False,
            'persistent': True
        }
@app.route('/')
def home():
    return """
    <h1>🔮 ربات جفر هوشمند</h1>
    <p>ربات فعال است ✅</p>
    <p>برای استفاده به تلگرام بروید</p>
    """
