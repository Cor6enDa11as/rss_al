#!/usr/bin/env python3

import requests
import json
import os
import time
import re
from urllib.parse import urlparse
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

AI_MODELS = ["google/gemini-2.0-flash-001", "google/gemini-2.0-flash-lite-preview-02-05:free"]

# Храним краткие смыслы новостей текущего запуска для удаления дублей
current_run_summaries = []

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def make_hashtag(text):
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]', '', text)
    return f"#{clean}" if clean else ""

def get_smart_tag(item, url):
    source_name = item.get('origin', {}).get('title', '')
    if "telegram channel" in source_name.lower():
        clean_text = re.sub(r'(?i)\s*[-]*\s*(telegram\s*channel)\s*', '', source_name).strip()
        return make_hashtag(clean_text)
    try:
        domain = urlparse(url).netloc.lower()
        tag = domain.replace('www.', '').split('.')[0].replace('-', '')
        return f"#{tag.capitalize()}"
    except: return "#News"

def check_is_video_strict(item, soup):
    link = item.get('alternate', [{}])[0].get('href', '').lower()
    # Проверка через домены
    if any(x in link for x in ["youtube.com", "youtu.be", "vimeo.com"]): return True
    # Проверка через вложения (enclosures)
    enclosures = item.get('enclosure', [])
    if any('video' in enc.get('type', '') for enc in enclosures): return True
    # Проверка через заголовок и классы
    if "видео" in item.get('title', '').lower(): return True
    if soup and (soup.find('video') or soup.find('div', class_='tgme_widget_message_video_player')): return True
    return False

def get_full_content(item, url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        article = (soup.find('div', {'class': 'tgme_widget_message_text'}) or
                   soup.find('div', {'class': 'tm-article-body'}) or
                   soup.find('article'))
        if article: return " ".join(article.get_text(separator=' ', strip=True).split())[:3500], soup
        rss_text = item.get('summary', {}).get('content', '') or item.get('content', {}).get('content', '')
        if rss_text:
            clean_rss = BeautifulSoup(rss_text, 'html.parser').get_text(separator=' ', strip=True)
            return clean_rss[:3500], soup
    except Exception as e: log(f"⚠️ Ошибка парсинга: {e}")
    return "", None

def get_ai_summary(text, is_video=False):
    if not text or len(text) < 100: return None

    # Промт с инструкцией по дедупликации
    history_context = "\n".join(current_run_summaries[-10:])
    prompt = (
        f"Суть новости ОДНИМ предложением (30 слов) на русском. Опиши событие и результат.\n"
        f"ВАЖНО: Если новость по смыслу совпадает с чем-то из этого списка, ответь только одним словом SKIP:\n"
        f"{history_context}"
    )

    for model in AI_MODELS:
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": f"{prompt}\n\nТекст новости: {text}"}], "temperature": 0.1}), timeout=30)
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                if "SKIP" in res.upper(): return "SKIP"

                if is_video and "🎬" not in res: res += " 🎬"
                current_run_summaries.append(res) # Запоминаем для дедупликации
                log(f"✅ Модель {model} обработала новость.")
                return res
        except: continue
    return None

def send_tg(text, preview_enabled=False, link=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    p_opts = {"is_disabled": not preview_enabled}
    if preview_enabled and link: p_opts.update({"url": link, "prefer_large_media": True, "show_above_text": True})
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps(p_opts)}
    try: requests.post(url, data=payload, timeout=10)
    except: log("❌ Ошибка сети TG")

def process_category(cat_name, use_ai, headers, api_base):
    log(f"\n--- {cat_name.upper()} ---")
    try:
        # Запрашиваем 80 новостей и только непрочитанные (xt=...)
        params = {'n': 80, 'xt': 'user/-/state/com.google/read'}
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", params=params, headers=headers)
        items = r.json().get('items', [])
        if not items:
            log("Новых новостей нет.")
            return

        msg_body_ai = f"{make_hashtag(cat_name)}\n\n"
        ai_count = 0

        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if not link: continue

            text_content, soup = get_full_content(item, link)
            is_video = check_is_video_strict(item, soup)
            is_youtube = any(x in link.lower() for x in ["youtube.com", "youtu.be"])
            tag = get_smart_tag(item, link)

            if use_ai:
                summary = get_ai_summary(text_content, is_video)
                if summary == "SKIP":
                    log(f"⏭️ Пропущен дубликат: {item.get('title')[:30]}...")
                elif summary:
                    msg_body_ai += f"📌 <a href='{link}'>→</a> <i>{summary}</i>\n🏷️ {tag}\n\n"
                    ai_count += 1
                time.sleep(1)
            else:
                title = item.get('title', 'Видео')
                if is_youtube:
                    direct_msg = f"📍 <b><a href='{link}'>{title}</a></b>\n🏷️ {tag}"
                    log(f"⏳ Пауза 5с для превью YouTube...")
                    time.sleep(15)
                else:
                    title_text = f"{title} 🎬" if is_video else title
                    direct_msg = f"📍 <b><a href='{link}'>{title_text}</a></b>\n🏷️ {tag}"
                send_tg(direct_msg, True, link)
                log(f"✅ Direct: {tag}")

            # Помечаем как прочитанное во FreshRSS
            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            send_tg(msg_body_ai, False)
            log(f"📊 Сводка отправлена. Постов: {ai_count}")

    except Exception as e: log(f"❌ Ошибка в категории: {e}")

def main():
    login_url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}"
    r_auth = requests.get(login_url)
    auth = re.search(r'Auth=(.*)', r_auth.text)
    if not auth:
        log("❌ Ошибка авторизации!")
        return

    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)

if __name__ == "__main__": main()
