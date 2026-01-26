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

# Список реально бесплатных агентов (Round-robin)
AI_MODELS = [
    "meta-llama/llama-3.1-405b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "qwen/qwen-2.5-vl-7b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "qwen/qwen3-4b:free",
    "openai/gpt-oss-20b:free"
]

model_index = 0
processed_summaries = []

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def get_ai_summary(text, is_video):
    global model_index
    if not text or len(text) < 100: return None

    # Ослабили фильтр: ИИ теперь знает про "серии" постов
    context = "\n".join(processed_summaries[-6:])
    prompt = (
        f"Ты — новостной аналитик. Напиши суть новости ОДНИМ коротким предложением ( 30 слов) на русском.\n"
        f"ПРАВИЛО SKIP: Отвечай ТОЛЬКО словом SKIP, если это абсолютный дубликат по смыслу (одно и то же событие).\n"
        f"ВАЖНО: Если это продолжение темы, новая часть серии постов или другой аспект новости — ПИШИ сводку. Не скипай JamClub и серийные новости.\n"
        f"Ранее было: {context}\nТекст для анализа: {text}"
    )

    # Крутим карусель моделей
    for _ in range(len(AI_MODELS)):
        model = AI_MODELS[model_index % len(AI_MODELS)]
        model_index += 1

        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4}), timeout=25)

            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                if "SKIP" in res.upper() and len(res) < 10: return "SKIP"

                final = f"{res} 🎬" if is_video and "🎬" not in res else res
                processed_summaries.append(final)
                log(f"✅ {model.split('/')[-1]} выдал базу")
                return final
            else:
                log(f"⚠️ {model.split('/')[-1]} статус {r.status_code}")
        except Exception as e:
            log(f"❌ {model.split('/')[-1]} сбой")
            continue
    return None

def get_content(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Ищем суть статьи, отсекая лишнее
        article = soup.find('div', {'class': ['tgme_widget_message_text', 'article-body', 'post__text']}) or soup.find('article')
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return " ".join(text.split())[:3500], soup
    except: return "", None

def process_category(cat_name, use_ai, headers, api_base):
    log(f"--- 📂 КАТЕГОРИЯ: {cat_name.upper()} ---")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", params={'n': 50, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items:
        log("☕ Новых новостей нет")
        return

    valid_news = []
    for item in items:
        link = item.get('alternate', [{}])[0].get('href', '')
        if not link: continue

        text, soup = get_content(link)
        is_video = any(x in link.lower() for x in ["youtube.com", "youtu.be"]) or (soup and soup.find('video'))

        if use_ai:
            summary = get_ai_summary(text, is_video)
            if summary == "SKIP":
                log(f"⏭️ SKIP: Дубликат ({link[:35]}...)")
            elif summary:
                valid_news.append({"link": link, "content": summary, "id": item.get('id')})
            time.sleep(1.5) # Пауза между запросами ИИ
        else:
            # Прямая пересылка для CATEGORIES_DIRECT
            msg = f"📍 <b><a href='{link}'>{item.get('title')}</a></b>"
            if is_video: time.sleep(15) # Пауза для прогрузки превью видео
            if send_tg(msg, True, link):
                mark_as_read(api_base, headers, item.get('id'))
                log(f"🚀 Прямой пост отправлен: {link[:40]}")

    if use_ai and valid_news:
        # Сортируем от старых к новым
        valid_news.reverse()
        msg = f"#{cat_name}\n\n" + "\n\n".join([f"📌 <a href='{n['link']}'>→</a> <i>{n['content']}</i>" for n in valid_news])
        if send_tg(msg):
            for n in valid_news: mark_as_read(api_base, headers, n['id'])
            log(f"✅ Сводка {cat_name} улетела в Telegram")

def send_tg(text, preview=False, link=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    opts = {"is_disabled": not preview}
    if preview and link: opts.update({"url": link, "prefer_large_media": True})
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps(opts)}
    try:
        res = requests.post(url, data=payload, timeout=15)
        return res.status_code == 200
    except: return False

def mark_as_read(api_base, headers, item_id):
    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item_id, 'a': 'user/-/state/com.google/read'})

def main():
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth:
        log("❌ Ошибка авторизации")
        return
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)

if __name__ == "__main__": main()
