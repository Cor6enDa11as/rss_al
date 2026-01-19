#!/usr/bin/env python3
import requests
import json
import os
import time
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ИЗ SECRETS ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

AI_MODELS = [
    "google/gemini-2.0-flash-001",
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen-2.5-72b-instruct"
]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def get_auth_token():
    url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin"
    try:
        r = requests.get(url, params={'Email': USER, 'Passwd': PASS}, timeout=10)
        if r.status_code == 200:
            for line in r.text.split('\n'):
                if line.startswith('Auth='): return line.replace('Auth=', '').strip()
    except: return None

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'button', 'form']): s.decompose()
        article = (soup.find('div', {'class': 'tm-article-body'}) or soup.find('article') or soup.find('main'))
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return " ".join(text.split())[:5000]
    except: return ""

def get_ai_summary(url, seen_summaries):
    content = get_full_text(url)
    if len(content) < 150: return None
    prompt = (
        "Напиши суть новости ОДНИМ развернутым предложением (до 30 слов) на РУССКОМ языке. "
        "Отрази ключевой факт. Игнорируй заголовок. "
        "Если это дубликат тем: " + ", ".join(list(seen_summaries)[-5:]) + ", ответь ДУБЛИКАТ. "
        f"\n\nТЕКСТ:\n{content[:4000]}"
    )
    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}),
                timeout=30
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                return "SKIP" if "ДУБЛИКАТ" in res.upper() else res.rstrip('.')
        except: continue
    return None

def extract_hashtag(url):
    try:
        domain = urlparse(url).netloc.lower()
        parts = domain.replace('www.', '').split('.')
        tag = parts[0].replace('-', '')
        return f"#{tag}"
    except: return "#news"

def process_category(cat_name, use_ai, token, headers, api_base, global_seen):
    log(f"--- Категория: {cat_name} (ИИ: {use_ai}) ---")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                         params={'xt': 'user/-/state/com.google/read', 'n': 10}, headers=headers)
        items = r.json().get('items', [])
        if not items: return

        ai_msg_body = f"<b>{cat_name.upper()}:</b>\n\n"
        ai_count = 0

        for item in items:
            title = item.get('title', 'Новость')
            link = item.get('alternate', [{}])[0].get('href', '')
            tag = extract_hashtag(link)

            if use_ai:
                summary = get_ai_summary(link, global_seen)
                if summary == "SKIP": continue
                text = summary if summary else title
                # Курсив и перенос хэштега на новую строку
                ai_msg_body += f"📌 <i>{text}</i>\n📌 <a href='{link}'>{tag}</a>\n\n"
                global_seen.add(text)
                ai_count += 1
            else:
                # Настройки превью для YouTube
                preview_options = {
                    "url": link,
                    "prefer_large_media": True,
                    "show_above_text": True
                }
                # Без жирного шрифта, с новыми эмодзи 📌
                direct_msg = f"📌 <a href='{link}'>{title}</a>\n📌<a href='{link}'>{tag}</a>"

                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={
                                  "chat_id": CHAT_ID,
                                  "text": direct_msg,
                                  "parse_mode": "HTML",
                                  "link_preview_options": json.dumps(preview_options)
                              })

            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            ai_preview = {"is_disabled": True}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={
                              "chat_id": CHAT_ID,
                              "text": ai_msg_body,
                              "parse_mode": "HTML",
                              "link_preview_options": json.dumps(ai_preview)
                          })
    except Exception as e: log(f"Ошибка в {cat_name}: {e}")

def main():
    token = get_auth_token()
    if not token: return
    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen = set()

    for cat in CATEGORIES_AI:
        process_category(cat, True, token, headers, api_base, global_seen)
    for cat in CATEGORIES_DIRECT:
        process_category(cat, False, token, headers, api_base, global_seen)

if __name__ == "__main__":
    main()
