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

# Работаем через Gemini 001 как основную
AI_MODELS = ["google/gemini-2.0-flash-001", "google/gemini-2.0-flash-lite-preview-02-05:free"]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def make_hashtag(text):
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]', '', text)
    return f"#{clean}" if clean else ""

def get_clean_channel_tag(text):
    # Убираем дефисы и приписки Telegram Channel / YouTube
    clean_text = re.sub(r'(?i)\s*[-]*\s*(telegram\s*channel|youtube)\s*', '', text).strip()
    return make_hashtag(clean_text)

def check_is_video(item, content_text):
    link = item.get('alternate', [{}])[0].get('href', '').lower()
    if any(x in link for x in ["youtube.com", "youtu.be"]): return True
    search_area = (item.get('title', '') + " " + content_text).lower()
    if any(word in search_area for word in ["видео", "ролик", "🎥", "🎬", "video"]): return True
    return False

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Ищем текст в Telegram постах и обычных статьях
        article = (soup.find('div', {'class': 'tgme_widget_message_text'}) or
                   soup.find('div', {'class': 'tm-article-body'}) or
                   soup.find('article') or soup.find('main'))
        if article:
            return " ".join(article.get_text(separator=' ', strip=True).split())[:3500]
        return ""
    except: return ""

def get_ai_summary(url, is_video=False):
    content = get_full_text(url)
    if not content or len(content) < 100: return None

    prompt = (
        "Сформулируй суть новости ОДНИМ информативным предложением (до 30 слов) на русском. "
        "Опиши событие, участников и результат."
    )
    if is_video:
        prompt += " В конце предложения обязательно добавь пометку (Видео)."

    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": f"{prompt}\n\n{content}"}],
                    "temperature": 0.1
                }),
                timeout=30
            )
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip().rstrip('.')
        except: continue
    return None

def send_tg(text, preview_enabled=False, link=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    preview_opts = {"is_disabled": not preview_enabled}
    if preview_enabled and link:
        preview_opts.update({"url": link, "prefer_large_media": True, "show_above_text": True})

    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps(preview_opts)}
    try:
        requests.post(url, data=payload, timeout=10)
    except: log("❌ Ошибка отправки в TG")

def process_category(cat_name, use_ai, token, headers, api_base, global_seen_urls):
    log(f"\n--- КАТЕГОРИЯ: {cat_name.upper()} ---")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", params={'n': 40}, headers=headers)
        items = r.json().get('items', [])
        if not items: return

        msg_body_ai = f"{make_hashtag(cat_name)}\n\n"
        ai_count = 0

        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if not link or link in global_seen_urls: continue

            source_name = item.get('origin', {}).get('title', 'news')
            tag = get_clean_channel_tag(source_name)

            # Проверяем наличие видео (в тексте или по ссылке)
            item_content = item.get('summary', {}).get('content', '')
            is_video = check_is_video(item, item_content)

            if use_ai:
                # Группируем Telegram/Новости в одну сводку
                summary = get_ai_summary(link, is_video)
                if not summary:
                    summary = item.get('title', 'Новость')
                    if is_video: summary += " (Видео)"

                msg_body_ai += f"📌 <i>{summary}</i>\n🏷️ <a href='{link}'>{tag}</a>\n\n"
                ai_count += 1
                time.sleep(2)
            else:
                # Отправляем YouTube (или DIRECT) отдельно с КАРТИНКОЙ
                title = item.get('title', 'Новость')
                if is_video: title += " (Видео)"
                direct_msg = f"📍 <b>{title}</b>\n🏷️ <a href='{link}'>{tag}</a>"
                send_tg(direct_msg, preview_enabled=True, link=link)
                log(f"✅ Direct пост: {title[:40]}...")

            global_seen_urls.add(link)
            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            send_tg(msg_body_ai, preview_enabled=False)
            log(f"✅ AI Сводка отправлена ({ai_count} шт.)")

    except Exception as e: log(f"❌ Ошибка: {e}")

def main():
    # Авторизация
    login_url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}"
    r_auth = requests.get(login_url)
    auth_match = re.search(r'Auth=(.*)', r_auth.text)
    if not auth_match:
        log("❌ Ошибка авторизации FreshRSS")
        return

    headers = {'Authorization': f'GoogleLogin auth={auth_match.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    seen = set()
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f: seen = set(line.strip() for line in f)

    # 1. Сначала обрабатываем AI (сводка)
    for cat in CATEGORIES_AI:
        process_category(cat, True, None, headers, api_base, seen)

    # 2. Затем DIRECT (YouTube с превью)
    for cat in CATEGORIES_DIRECT:
        process_category(cat, False, None, headers, api_base, seen)

    # Сохраняем базу ссылок
    with open(DB_FILE, "w") as f:
        for item in list(seen)[-1000:]: f.write(f"{item}\n")

if __name__ == "__main__": main()
