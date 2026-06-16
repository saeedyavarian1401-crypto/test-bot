import sys
import traceback
from flask import Flask, request
import requests

# ثبت خطاها در فایل
log_file = open("/tmp/bot_error.log", "a")

app = Flask(__name__)

TOKEN = "8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ"
GROQ_KEY = "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"

print("ربات در حال شروع است...")
log_file.write("ربات در حال شروع است...\n")

try:
    print("در حال تست اتصال به GROQ...")
    test_url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    response = requests.get(test_url, headers=headers, timeout=10)
    print(f"اتصال به GROQ موفق بود: {response.status_code}")
    log_file.write(f"اتصال به GROQ موفق بود: {response.status_code}\n")
except Exception as e:
    print(f"خطا در اتصال به GROQ: {e}")
    log_file.write(f"خطا در اتصال به GROQ: {e}\n")
    traceback.print_exc(file=log_file)
    sys.exit(1)

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"خطا در ارسال: {e}")
        log_file.write(f"خطا در ارسال: {e}\n")

def ask_groq(question):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "llama3-70b-8192",
        "messages": [{"role": "user", "content": question}]
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=20)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"خطا در گروک: {e}")
        log_file.write(f"خطا در گروک: {e}\n")
        return f"❌ خطا: {e}"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        if update and 'message' in update:
            chat_id = update['message']['chat']['id']
            text = update['message'].get('text', '')
            if text == '/start':
                send_message(chat_id, "سلام! من ربات هوشمند هستم.")
            elif text:
                send_message(chat_id, "🤔 در حال فکر کردن...")
                answer = ask_groq(text)
                send_message(chat_id, answer)
        return "ok", 200
    except Exception as e:
        print(f"خطا در وب هوک: {e}")
        log_file.write(f"خطا در وب هوک: {e}\n")
        traceback.print_exc(file=log_file)
        return "error", 500

@app.route('/')
def home():
    return "ربات هوشمند فعال است", 200
