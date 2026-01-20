#!/usr/bin/env python3

import requests
import json
import os
import time
import re
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

DB_FILE = "seen_urls.txt"

AI_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct",
    "mistralai/mistral-small-3.1-24b-instruct:free"
]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def normalize_url(url):
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except: return url

def get_domain_tag(url):
    """Хэштег домена (для AI)"""
    try:
        domain = urlparse(url).netloc.lower()
        tag = domain.replace('www.', '').split('.')[0].replace('-', '')
        return f"#{tag}"
    except: return "#news"

def get_clean_channel_tag(text):
    """Хэштег канала (для Direct)"""
    text = re.sub(r'(?i)\s*(YouTube|TelegramChannel)$', '', text)
    clean = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', text)
    return f"#{clean}" if clean else "#channel"

def load_seen():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(seen_set):
    list_to_save = list(seen_set)[-500:]
    with open(DB_FILE, "w") as f:
        for url in list_to_save: f.write(f"{url}\n")

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
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer']): s.decompose()
        article = (soup.find('div', {'class': 'tm-article-body'}) or
                   soup.find('div', {'class': 'tgme_widget_message_text'}) or
                   soup.find('article') or soup.find('main'))
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return " ".join(text.split())[:4000]
    except: return ""

def get_ai_summary(url, seen_summaries):
    content = get_full_text(url)
    if len(content) < 150: return None
    prompt = (
        "Суть новости ОДНИМ предложением (до 25 слов) на РУССКОМ. "
        "Только факты. Если тема совпадает с: " + ", ".join(list(seen_summaries)[-12:]) +
        ", ответь только ДУБЛИКАТ."
        f"\n\nТЕКСТ:\n{content}"
    )
    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}),
                timeout=30
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                return "SKIP" if "ДУБЛИКАТ" in res.upper() else res.rstrip('.')
        except: continue
    return None

def process_category(cat_name, use_ai, token, headers, api_base, global_seen_urls, global_seen_content):
    log(f"--- {cat_name} ---")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                         params={'xt': 'user/-/state/com.google/read', 'n': 20}, headers=headers)
        items = r.json().get('items', [])
        if not items: return

        # Хэштег категории (например, #Научпоп)
        cat_hashtag = f"#{cat_name.replace(' ', '')}"

        ai_msg_body = f"{cat_hashtag}\n\n"
        ai_count = 0

        for item in items:
            raw_url = item.get('alternate', [{}])[0].get('href', '')
            link = normalize_url(raw_url)
            title = item.get('title', 'Новость')
            source_name = item.get('origin', {}).get('title', 'news')

            if link in global_seen_urls:
                requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})
                continue

            if use_ai:
                summary = get_ai_summary(link, global_seen_content)
                if summary == "SKIP": continue
                tag = get_domain_tag(link)
                text = summary if summary else title
                ai_msg_body += f"📌 <i>{text}</i>\n🏷️ <a href='{link}'>{tag}</a>\n\n"
                global_seen_content.add(text)
                ai_count += 1
            else:
                tag = get_clean_channel_tag(source_name)
                preview = {"url": link, "prefer_large_media": True, "show_above_text": True}
                # Добавляем хэштег категории в конец Direct-сообщения
                direct_msg = f"📌 <a href='{link}'>{title}</a>\n🏷️ <a href='{link}'>{tag}</a> {cat_hashtag}"
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": direct_msg, "parse_mode": "HTML", "link_preview_options": json.dumps(preview)})

            global_seen_urls.add(link)
            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": ai_msg_body, "parse_mode": "HTML", "link_preview_options": json.dumps({"is_disabled": True})})
    except Exception as e: log(f"Ошибка: {e}")

def main():
    token = get_auth_token()
    if not token: return
    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_urls = load_seen()
    global_seen_content = set()
    for cat in CATEGORIES_AI: process_category(cat, True, token, headers, api_base, global_seen_urls, global_seen_content)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, token, headers, api_base, global_seen_urls, global_seen_content)
    save_seen(global_seen_urls)

if __name__ == "__main__": main()
