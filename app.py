import sys
import traceback
from flask import Flask, request
import requests

app = Flask(__name__)

TOKEN = "8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ"
GROQ_KEY = "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"

print("ربات در حال شروع است...")

try:
    test_url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    response = requests.get(test_url, headers=headers, timeout=10)
    print(f"اتصال به GROQ موفق بود: {response.status_code}")
except Exception as e:
    print(f"خطا: {e}")
    sys.exit(1)

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"خطا در ارسال: {e}")

def ask_groq(question):
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
        data = {"model": "llama3-70b-8192", "messages": [{"role": "user", "content": question}]}
        r = requests.post(url, headers=headers, json=data, timeout=20)
        return r.json()["choices"][0]["message"]["content"]
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
                send_message(chat_id, "سلام! من ربات هوشمند هستم.")
            elif text:
                send_message(chat_id, "🤔 در حال فکر کردن...")
                answer = ask_groq(text)
                send_message(chat_id, answer)
        return "ok", 200
    except Exception as e:
        print(f"خطا: {e}")
        return "error", 500

@app.route('/')
def home():
    return "ربات هوشمند فعال است", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
