#!/usr/bin/env python3
import requests
import json
import os
import time
from bs4 import BeautifulSoup

# --- НАСТРОЙКИ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CATEGORIES_TO_WATCH = ["Научпоп", "Технологии", "Компьютерное железо"]

AI_MODELS = [
    "google/gemini-flash-1.5-exp:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free"
]

def get_auth_token():
    url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin"
    params = {'Email': USER, 'Passwd': PASS}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            for line in r.text.split('\n'):
                if line.startswith('Auth='):
                    return line.replace('Auth=', '').strip()
    except: return None

def get_ai_summary(title, url):
    # Извлечение текста
    content = ""
    try:
        r_page = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r_page.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer']): s.decompose()
        content = soup.get_text(separator=' ', strip=True)[:3000]
    except: content = title

    prompt = f"Суть новости одним предложением до 15 слов. Факты. Заголовок: {title}\nТекст: {content}"

    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}),
                timeout=20
            )
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip().rstrip('.')
        except: continue
    return title

def main():
    token = get_auth_token()
    if not token: return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    for cat_name in CATEGORIES_TO_WATCH:
        tag_id = f"user/-/label/{cat_name}"
        params = {'xt': 'user/-/state/com.google/read', 'n': 10}

        try:
            r = requests.get(f"{api_base}/stream/contents/{tag_id}", params=params, headers=headers)
            items = r.json().get('items', [])

            if items:
                # ФОРМИРОВАНИЕ ТЕКСТА СООБЩЕНИЯ
                msg = f"<b>🤖 {cat_name.upper()}:</b>\n\n"

                for item in items:
                    title = item.get('title', 'Новость')
                    url = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')

                    summary = get_ai_summary(title, url)

                    # ЧИСТКА ХЭШТЕГА (только буквы и цифры)
                    clean_tag = "".join(filter(str.isalnum, source.lower()))

                    # СТРОКА НОВОСТИ С ЭМОДЗИ И ССЫЛКОЙ-ТЕГОМ
                    msg += f"⚡️ {summary}, <a href='{url}'>#{clean_tag}</a>\n\n"

                    # МАРКИРОВКА ПРОЧИТАННЫМ
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                # ОТПРАВКА В ТЕЛЕГРАМ
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
        except: continue

if __name__ == "__main__":
    main()
