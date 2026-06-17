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
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict

# ==================== تنظیمات اولیه ====================
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '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')

# ==================== هوش مصنوعی گروک ====================
class GroqAI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
    
    def ask(self, prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
        """ارسال سوال به گروک و دریافت پاسخ"""
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "تو یک متخصص علوم غریبه و جفر هستی. پاسخ‌هایت را به فارسی و با لحنی آرام و قابل فهم بده."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 1024
            }
            
            response = requests.post(self.base_url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            else:
                logger.error(f"خطا در گروک: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"خطا در اتصال به گروک: {e}")
            return None

groq = GroqAI(GROQ_API_KEY)

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
                    groq_interpretation TEXT,
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
    
    def save_query_history(self, chat_id: str, question: str, j36: Dict, j360: Dict, interpretation: str):
        with self.get_connection() as conn:
            conn.execute(
                '''INSERT INTO query_history 
                   (chat_id, question, jafr_36_result, jafr_360_result, groq_interpretation) 
                   VALUES (?, ?, ?, ?, ?)''',
                (chat_id, question, json.dumps(j36), json.dumps(j360), interpretation)
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

# ==================== تعبیر هوشمند با گروک ====================
def get_jafr_interpretation(question: str, name: str, mother: str, j36: JafrResult, j360: JafrResult) -> str:
    """گرفتن تعبیر هوشمندانه از جفر با کمک گروک"""
    
    prompt = f"""
شما یک متخصص علوم غریبه و جفر هستید.

اطلاعات کاربر:
- نام: {name}
- نام مادر: {mother}
- سوال: {question}

نتیجه جفر ۳۶:
- پاسخ: {j36.answer}
- امتیاز: {j36.score}/100
- توصیه: {j36.advice}

نتیجه جفر ۳۶۰:
- پاسخ: {j360.answer}
- امتیاز: {j360.score}/100
- درجه: {j360.degree if j360.degree else '---'}
- توصیه: {j360.advice}

لطفاً یک تعبیر جامع، دقیق و شخصی‌سازی شده برای این کاربر ارائه بده.

تعبیرت باید شامل موارد زیر باشد:
1. تحلیل کلی وضعیت (با توجه به عدد جفر و سوال کاربر)
2. توصیه‌های عملی و روزمره
3. اگر سوال منفی است، راهکارهای رفع آن
4. اگر سوال مثبت است، راهکارهای تقویت آن
5. یک جمله الهام‌بخش پایانی

تعبیر را به فارسی روان و قابل فهم بنویس.
"""
    
    response = groq.ask(prompt)
    
    if response:
        return response
    else:
        # اگر گروک جواب نداد، از جواب‌های پیش‌فرض استفاده کن
        return f"""
🔮 **تعبیر جفر برای {name}**

بر اساس محاسبات جفر و تحلیل سوال شما:

📖 **جفر ۳۶**
{j36.answer}
امتیاز: {j36.score}/100

📖 **جفر ۳۶۰** (دقیق‌تر)
{j360.answer}
امتیاز: {j360.score}/100
درجه: {j360.degree if j360.degree else '---'}

💡 **توصیه کلی:**
{j36.advice}

⚠️ تعبیر هوشمند در دسترس نیست. لطفاً دوباره تلاش کنید.
"""

# ==================== کیبورد دائمی ====================
class BotKeyboard:
    @staticmethod
    def get_main_keyboard():
        keyboard = [
            ['🔮 جفرگیری', '📊 تاریخچه'],
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
                '''SELECT question, groq_interpretation, created_at FROM query_history 
                   WHERE chat_id = ? ORDER BY created_at DESC LIMIT 5''',
                (chat_id,)
            ).fetchall()
        
        if not results:
            return "📭 هنوز سوالی نپرسیده‌اید."
        
        history = "📜 **تاریخچه سوالات**\n\n"
        for i, row in enumerate(results, 1):
            history += f"{i}. {row['question']}\n🕐 {row['created_at'][:16]}\n"
            if row['groq_interpretation']:
                history += f"📖 {row['groq_interpretation'][:100]}...\n"
            history += "\n"
        return history
    
    @staticmethod
    def get_help_message() -> str:
        return """
📖 **راهنمای ربات جفر + هوش مصنوعی**

🔮 **چگونه کار می‌کند؟**
1. محاسبه جفر ۳۶ و ۳۶۰ با استفاده از علم ابجد
2. تعبیر و تفسیر هوشمند با کمک هوش مصنوعی گروک

📝 **مراحل استفاده:**
1. روی دکمه جفرگیری کلیک کنید
2. اطلاعات خود را وارد کنید
3. سوال خود را بپرسید
4. نتیجه را با تعبیر هوشمند دریافت کنید

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
        
        # تعبیر هوشمند با گروک
        interpretation = get_jafr_interpretation(question, name, mother, j36, j360)
        
        db.increment_queries(chat_id)
        db.save_query_history(chat_id, question, j36.to_dict(), j360.to_dict(), interpretation)
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
🤖 **تعبیر هوشمند با گروک**

{interpretation}

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
                    "🔮 **ربات جفر ۳۶ و ۳۶۰ + هوش مصنوعی**\n\nسلام! 👋\nاین ربات با ترکیب علم جفر و هوش مصنوعی گروک به سوالات شما پاسخ می‌دهد.\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
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
                    "ℹ️ **درباره ربات**\n\nنسخه ۳.۰.۰\nربات جفر هوشمند با گروک\n\n⚡ ترکیب علم جفر و هوش مصنوعی\n🔮 تعبیر شخصی‌سازی شده\n📊 تاریخچه سوالات"
                )
                return jsonify({'status': 'ok'}), 200
            
            elif text == '❌ لغو عملیات':
                response = UserManager.cancel_session(chat_id)
                TelegramBot.send_message(chat_id, response)
                return jsonify({'status': 'ok'}), 200
            
            # پردازش مراحل
            session = db.get_session(chat_id)
            if session:
                response, is_complete = UserManager.process_step(chat_id, text)
                if response:
                    if is_complete:
                        TelegramBot.send_message(chat_id, response)
                    else:
                        if session.get('step') in ['name', 'mother', 'day', 'month', 'year']:
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
    <h1>🔮 ربات جفر ۳۶ و ۳۶۰ + هوش مصنوعی</h1>
    <p>ربات آنلاین و فعال است ✅</p>
    <p>⚡ ترکیب علم جفر و هوش مصنوعی گروک</p>
    """

# ==================== منوی پایین ====================
def set_bot_commands():
    """تنظیم منوی پایین تلگرام (Command Menu)"""
    try:
        commands = [
            {"command": "start", "description": "🔄 شروع مجدد"},
            {"command": "ask", "description": "🔮 جفرگیری هوشمند"},
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
@app.route('/')
def home():
    return "ربات فعال است", 200
