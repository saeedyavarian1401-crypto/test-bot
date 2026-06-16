import os
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TOKEN = "8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ"
GROQ_API_KEY = "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"

# مشتری گروک رو با کلید راه‌اندازی می‌کنیم
client = Groq(api_key=GROQ_API_KEY)

def send_message(chat_id, text):
    # ... (همون تابع قبلی)
    pass

def ask_groq(question):
    try:
        # درخواست با مدل پیشنهادی
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": question,
                }
            ],
            model="llama-3.3-70b-versatile",  # مدل به‌روز و پایدار
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        print(f"خطا در گروک: {e}")
        return f"❌ خطا: {e}"

# بقیه کد (وب‌هوک و ...) به همین صورت می‌مونه
