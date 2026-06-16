from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TOKEN = "8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ"
# کلید جدید یا قدیمی رو اینجا بذار
GROQ_API_KEY = "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"

# راه‌اندازی مشتری گروک
client = Groq(api_key=GROQ_API_KEY)

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"خطا در ارسال: {e}")

def ask_groq(question):
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": question}],
            model="llama-3.3-70b-versatile",  # مدل جدید و پایدار
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        print(f"خطا در گروک: {e}")
        return f"❌ خطا: {e}"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        if update and 'message' in update:
            chat_id = update['message']['chat']['id']
            text = update['message'].get('text', '')
            if text == '/start':
                send_message(chat_id, "سلام! من ربات هوشمند با گروک جدید هستم.")
            elif text:
                send_message(chat_id, "🤔 در حال فکر کردن...")
                answer = ask_groq(text)
                send_message(chat_id, answer)
        return "ok", 200
    except Exception as e:
        return "error", 500

@app.route('/')
def home():
    return "ربات هوشمند با گروک جدید فعال است", 200
