# ==================== imports ====================
from flask import Flask, request, jsonify
import requests
from datetime import datetime
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
import time
from typing import Dict, Optional, Tuple, Any, List
from dataclasses import dataclass, asdict
import secrets
import hashlib
import random

# ==================== تنظیمات اولیه ====================
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ')

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
            conn.commit()
    
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
                   (chat_id, step, name, mother, day, month, year, question, jafr_type) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (chat_id, step, 
                 kwargs.get('name', ''),
                 kwargs.get('mother', ''),
                 kwargs.get('day', None),
                 kwargs.get('month', None),
                 kwargs.get('year', None),
                 kwargs.get('question', ''),
                 kwargs.get('jafr_type', 'both'))
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
    
    def save_query_history(self, chat_id: str, question: str, j36: Dict, j360: Dict, fortune_type: str = None, fortune_result: Dict = None):
        with self.get_connection() as conn:
            conn.execute(
                '''INSERT INTO query_history 
                   (chat_id, question, jafr_36_result, jafr_360_result, fortune_type, fortune_result) 
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (chat_id, question, json.dumps(j36), json.dumps(j360), fortune_type, json.dumps(fortune_result) if fortune_result else None)
            )
            conn.commit()

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

# ==================== آبجد ====================
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

# ==================== جفر ۳۶ و ۳۶۰ ====================
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

# ==================== رمل ۸ شکل ====================
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

def raml_extract(name: str) -> Dict:
    total = abjad_sum(name)
    keys = list(RAML_8.keys())
    shape_key = keys[total % len(keys)]
    shape = RAML_8[shape_key]
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

# ==================== فال قهوه ====================
COFFEE_SYMBOLS = {
    "انگشتر": {"meaning": "ازدواج یا نامزدی در راه است", "type": "خوب", "score": 85},
    "قلب": {"meaning": "عشق و محبت حقیقی به زودی وارد زندگی می‌شود", "type": "خوب", "score": 90},
    "تاج": {"meaning": "موفقیت و افتخار بزرگ در انتظار شماست", "type": "خوب", "score": 95},
    "خورشید": {"meaning": "موفقیت، خوشبختی و روشنایی کامل", "type": "خوب", "score": 98},
    "ستاره": {"meaning": "آرزوها برآورده می‌شود، شانس و اقبال", "type": "خوب", "score": 92},
    "گل": {"meaning": "خوشبختی بزرگ، دوستان خوب و عشق پایدار", "type": "خوب", "score": 88},
    "مار": {"meaning": "دشمن پنهان، احتیاط کنید", "type": "بد", "score": 25},
    "عقرب": {"meaning": "خطر نزدیک است", "type": "بد", "score": 20},
    "کلاغ": {"meaning": "خبر بد در راه است", "type": "بد", "score": 15},
    "تابوت": {"meaning": "بیماری طولانی یا خبر تلخ", "type": "بد", "score": 10},
    "کلید": {"meaning": "گشایش کارها، راه حل مشکلات", "type": "خوب", "score": 87},
    "کتاب": {"meaning": "دانش، علم، موفقیت تحصیلی", "type": "خوب", "score": 86},
    "خانه": {"meaning": "آرامش، ثبات، زندگی خوب", "type": "خوب", "score": 83},
    "فرشته": {"meaning": "خبر خوب، کمک الهی", "type": "خوب", "score": 96}
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
    
    return {
        "type": "coffee_reading",
        "symbols_found": symbols,
        "detailed_results": results,
        "average_score": avg_score,
        "overall": overall,
        "advice": advice
    }

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
        result = "مثبت ✅"
        advice = "این کار برای تو خیر و برکت دارد. با توکل بر خدا اقدام کن."
        recommendation = "انجام بده"
    elif score >= 40:
        result = "متوسط ⚠️"
        advice = "نتیجه این کار به نیت و تلاش تو بستگی دارد. بهتر است بیشتر فکر کنی."
        recommendation = "مصمم شو و استخاره را تکرار کن"
    else:
        result = "منفی ❌"
        advice = "این کار برای تو خوب نیست. بهتر است منصرف شوی."
        recommendation = "انجام نده"
    
    return {
        "type": "istikhara",
        "issue": issue,
        "result": result,
        "score": score,
        "prayer": ISTIKHARA_PRAYER,
        "advice": advice,
        "recommendation": recommendation,
        "instruction": "۲ رکعت نماز استخاره بخوان، سپس این دعا را بخوان و به علامت‌ها توجه کن."
    }

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
    
    return {
        "tab": tabaye[remainder],
        "text": interpretations[tabaye[remainder]],
        "main_number": main_num,
        "remainder": remainder
    }

# ==================== کیبورد دائمی ====================
class BotKeyboard:
    @staticmethod
    def get_main_keyboard():
        keyboard = [
            ['🔮 جفرگیری', '📊 تاریخچه'],
            ['☕ فال قهوه', '📖 فال حافظ'],
            ['🃏 فال تاروت', '🤲 استخاره'],
            ['📖 راهنما', '📈 آمار من'],
            ['ℹ️ درباره', '❌ لغو عملیات']
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
📖 **راهنمای ربات جفر + فال‌های متنوع**

🔮 **قابلیت‌های ربات:**
1. 🔮 جفر ۳۶ و ۳۶۰
2. ☕ فال قهوه
3. 📖 فال حافظ
4. 🃏 فال تاروت
5. 🤲 استخاره
6. 🎲 رمل (پیشگویی رملی)
7. 👹 تشخیص همزاد

📝 **مراحل استفاده:**
1. از دکمه‌های پایین انتخاب کنید
2. اطلاعات خود را وارد کنید
3. نتیجه را دریافت کنید

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
        
        try:
            if step == 'name':
                if len(text) < 2:
                    return "❌ نام باید حداقل ۲ حرف باشد.", False
                db.save_session(chat_id, 'mother', name=text, jafr_type=session.get('jafr_type', 'both'))
                return "👩 **نام مادر خود را وارد کنید:**", False
            
            elif step == 'mother':
                if len(text) < 2:
                    return "❌ نام مادر باید حداقل ۲ حرف باشد.", False
                db.save_session(chat_id, 'day', name=session['name'], mother=text, jafr_type=session.get('jafr_type', 'both'))
                return "📅 **روز تولد (۱ تا ۳۱):**", False
            
            elif step == 'day':
                try:
                    day = int(text)
                    if not 1 <= day <= 31:
                        return "❌ روز باید بین ۱ تا ۳۱ باشد.", False
                    db.save_session(chat_id, 'month', name=session['name'], mother=session['mother'], 
                                  day=day, jafr_type=session.get('jafr_type', 'both'))
                    return "📅 **ماه تولد (۱ تا ۱۲):**", False
                except ValueError:
                    return "❌ لطفاً یک عدد معتبر وارد کنید:", False
            
            elif step == 'month':
                try:
                    month = int(text)
                    if not 1 <= month <= 12:
                        return "❌ ماه باید بین ۱ تا ۱۲ باشد.", False
                    db.save_session(chat_id, 'year', name=session['name'], mother=session['mother'],
                                  day=session['day'], month=month, jafr_type=session.get('jafr_type', 'both'))
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
                                  jafr_type=session.get('jafr_type', 'both'))
                    return "❓ **سوال خود را بپرسید:**", False
                except ValueError:
                    return "❌ لطفاً یک عدد معتبر وارد کنید:", False
            
            elif step == 'question':
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
        
        j36 = SmartJafrCalculator.calculate_36(question, name, mother, day, month, year)
        j360 = SmartJafrCalculator.calculate_360(question, name, mother, day, month, year)
        
        # رمل
        raml = raml_extract(name)
        
        # همزاد
        hamzad = check_hamzad([])
        hamzad_name_result = hamzad_name(name)
        
        # زایجه
        zayejah = zayejah_adl(question)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, question, j36.to_dict(), j360.to_dict())
        db.delete_session(chat_id)
        
        response = f"""
🔮 **نتیجه جفر برای {name}**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        if jafr_type in ['36', 'both']:
            response += f"""
📖 **جفر ۳۶**
{j36.answer}
⭐ امتیاز: {j36.score}/100
💡 توصیه: {j36.advice}
"""
        
        if jafr_type in ['360', 'both']:
            response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖 **جفر ۳۶۰**
{j360.answer}
⭐ امتیاز: {j360.score}/100
📊 درجه: {j360.degree if j360.degree else '---'}
💡 توصیه: {j360.advice}
"""
        
        response += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎲 **پیشگویی رملی**
شکل: {raml['sign']} {raml['shape']}
معنی: {raml['meaning']}

👹 **همزاد**
اسم ملکی: {hamzad_name_result['malaki']}
اسم جنی: {hamzad_name_result['jinni']}

⚗️ **زایجه عدل**
طبع: {zayejah['tab']}
تعبیر: {zayejah['text']}

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
    def do_hafez_fal(chat_id: str, niyat: str) -> str:
        result = hafez_fal(niyat)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, niyat, {}, {}, "hafez", result)
        
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
        
        response = f"""
🤲 **استخاره**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **موضوع:** {issue}

{result['sign'] if 'sign' in result else ''} **نتیجه: {result['result']}**
⭐ **امتیاز: {result['score']}/100**

📖 **دعای استخاره:**
{result['prayer']}

💡 **توصیه:**
{result['advice']}

📌 **پیشنهاد:**
{result['recommendation']}

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
                    "🔮 **ربات جفر + فال‌های متنوع**\n\nسلام! 👋\nاین ربات شامل:\n• جفر ۳۶ و ۳۶۰\n• فال قهوه ☕\n• فال حافظ 📖\n• فال تاروت 🃏\n• استخاره 🤲\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
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
            
            elif text == '📊 تاریخچه':
                response = UserManager.get_history(chat_id)
                TelegramBot.send_message(chat_id, response)
                return jsonify({'status': 'ok'}), 200
            
            elif text == '☕ فال قهوه':
                TelegramBot.send_message(
                    chat_id,
                    "☕ **فال قهوه**\n\nلطفاً نمادهایی که در ته فنجان دیده‌اید را با ویرگول (,) جدا کنید:\n\nمثال: قلب, ستاره, تاج",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                db.save_session(chat_id, 'coffee_symbols')
                return jsonify({'status': 'ok'}), 200
            
            elif text == '📖 فال حافظ':
                TelegramBot.send_message(
                    chat_id,
                    "📖 **فال حافظ**\n\nنیت یا سوال خود را وارد کنید (اختیاری):",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                db.save_session(chat_id, 'hafez_niyat')
                return jsonify({'status': 'ok'}), 200
            
            elif text == '🃏 فال تاروت':
                result = UserManager.do_tarot_fortune(chat_id, "three_card")
                TelegramBot.send_message(chat_id, result)
                return jsonify({'status': 'ok'}), 200
            
            elif text == '🤲 استخاره':
                TelegramBot.send_message(
                    chat_id,
                    "🤲 **استخاره**\n\nموضوع مورد نظر برای استخاره را وارد کنید:\n\nمثال: ازدواج با فلان شخص",
                    reply_markup=BotKeyboard.get_cancel_keyboard()
                )
                db.save_session(chat_id, 'istikhara_issue')
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
                    "ℹ️ **درباره ربات**\n\nنسخه ۴.۰.۰\nربات جفر و فال‌های متنوع\n\n🔮 ویژگی‌ها:\n• جفر ۳۶ و ۳۶۰\n• ☕ فال قهوه\n• 📖 فال حافظ\n• 🃏 فال تاروت\n• 🤲 استخاره\n• 🎲 رمل\n• 👹 همزاد"
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
                
                if step == 'coffee_symbols':
                    result = UserManager.do_coffee_fortune(chat_id, text)
                    db.delete_session(chat_id)
                    TelegramBot.send_message(chat_id, result)
                    return jsonify({'status': 'ok'}), 200
                
                elif step == 'hafez_niyat':
                    result = UserManager.do_hafez_fal(chat_id, text)
                    db.delete_session(chat_id)
                    TelegramBot.send_message(chat_id, result)
                    return jsonify({'status': 'ok'}), 200
                
                elif step == 'istikhara_issue':
                    result = UserManager.do_istikhara(chat_id, text)
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
    <h1>🔮 ربات جفر + فال‌های متنوع</h1>
    <p>ربات آنلاین و فعال است ✅</p>
    <p>🔮 جفر ۳۶ و ۳۶۰ | ☕ فال قهوه | 📖 فال حافظ | 🃏 فال تاروت | 🤲 استخاره</p>
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
    print(f"🚀 ربات روی پورت {port} اجرا شد")
    app.run(host='0.0.0.0', port=port, debug=False)
