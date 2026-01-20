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
    "mistralai/mistral-small-3.1-24b-instruct:free"
]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def normalize_url(url):
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except: return url

def make_hashtag(text):
    # Очистка: только буквы (включая ё) и цифры
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]', '', text)
    return f"#{clean}" if clean else ""

def get_clean_channel_tag(text):
    # Улучшенное удаление суффиксов (убирает пробелы и лишние хвосты)
    text = re.sub(r'(?i)\s*(youtube|telegramchannel)\s*$', '', text).strip()
    return make_hashtag(text)

def get_domain_tag(url):
    try:
        domain = urlparse(url).netloc.lower()
        tag = domain.replace('www.', '').split('.')[0].replace('-', '')
        return f"#{tag}"
    except: return "#news"

def load_seen():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(seen_set):
    list_to_save = list(seen_set)[-1000:]
    with open(DB_FILE, "w") as f:
        for item in list_to_save: f.write(f"{item}\n")

def get_auth_token():
    url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin"
    try:
        r = requests.get(url, params={'Email': USER, 'Passwd': PASS}, timeout=10)
        if r.status_code == 200:
            for line in r.text.split('\n'):
                if line.startswith('Auth='): return line.replace('Auth=', '').strip()
    except Exception as e: log(f"❌ Ошибка авторизации FreshRSS: {e}")
    return None

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer']): s.decompose()
        article = (soup.find('div', {'class': 'tm-article-body'}) or
                   soup.find('div', {'class': 'tgme_widget_message_text'}) or
                   soup.find('article') or soup.find('main'))
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        clean_text = " ".join(text.split())[:4500]
        log(f"🔎 Текст извлечен ({len(clean_text)} симв.): {url}")
        return clean_text
    except Exception as e:
        log(f"⚠️ Ошибка парсинга {url}: {e}")
        return ""

def get_ai_summary(url):
    content = get_full_text(url)
    if len(content) < 150:
        log(f"⏩ Пропуск: слишком короткий текст ({len(content)} симв.)")
        return None

    prompt = "Суть новости ОДНИМ предложением (до 25 слов) на РУССКОМ. Только факты."

    for model in AI_MODELS:
        try:
            log(f"🤖 Запрос к модели: {model}")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": f"{prompt}\n\n{content}"}], "temperature": 0.1}),
                timeout=35
            )
            if r.status_code == 200:
                summary = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                log(f"✅ Успешный ответ от {model}")
                return summary
            else:
                log(f"⚠️ Ошибка модели {model}: {r.status_code} {r.text[:100]}")
        except Exception as e:
            log(f"❌ Ошибка при запросе к {model}: {e}")
            continue
    return None

def send_tg(text, preview=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if preview:
        payload["link_preview_options"] = json.dumps(preview)
    else:
        payload["link_preview_options"] = json.dumps({"is_disabled": True})

    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code == 200:
            log("📲 Сообщение отправлено в Telegram")
            return True
        else:
            log(f"❌ Ошибка Telegram: {r.status_code} {r.text}")
    except Exception as e:
        log(f"❌ Критическая ошибка отправки TG: {e}")
    return False

def process_category(cat_name, use_ai, token, headers, api_base, global_seen_urls):
    log(f"🚀 ОБРАБОТКА КАТЕГОРИИ: {cat_name.upper()}")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                         params={'xt': 'user/-/state/com.google/read', 'n': 40}, headers=headers)
        items = r.json().get('items', [])
        if not items:
            log(f"📭 Категория {cat_name} пуста")
            return

        cat_tag = make_hashtag(cat_name)
        ai_msg_body = f"{cat_tag}\n\n"
        ai_count = 0

        for item in items:
            raw_url = item.get('alternate', [{}])[0].get('href', '')
            link = normalize_url(raw_url)
            title = item.get('title', 'Новость')
            source_name = item.get('origin', {}).get('title', 'news')

            if link in global_seen_urls:
                requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})
                continue

            log(f"📝 Обработка: {title[:50]}...")

            if use_ai:
                summary = get_ai_summary(link)
                if summary:
                    tag = get_domain_tag(link)
                    ai_msg_body += f"📌 <i>{summary}</i>\n🏷️ <a href='{link}'>{tag}</a>\n\n"
                    ai_count += 1
            else:
                tag = get_clean_channel_tag(source_name)
                preview = {"url": link, "prefer_large_media": True, "show_above_text": True}
                direct_msg = f"📌 <a href='{link}'>{title}</a>\n🏷️ <a href='{link}'>{tag}</a>"
                send_tg(direct_msg, preview)

            global_seen_urls.add(link)
            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            send_tg(ai_msg_body)

    except Exception as e:
        log(f"❌ Ошибка в process_category: {e}")

def main():
    log("🏁 Запуск бота")
    token = get_auth_token()
    if not token: return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_urls = load_seen()

    for cat in CATEGORIES_AI:
        process_category(cat, True, token, headers, api_base, global_seen_urls)
    for cat in CATEGORIES_DIRECT:
        process_category(cat, False, token, headers, api_base, global_seen_urls)

    save_seen(global_seen_urls)
    log("🏁 Работа завершена")

if __name__ == "__main__":
    main()
