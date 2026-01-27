#!/usr/bin/env python3

import requests
import os
import time
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

# Используем только проверенных агентов
KEYS = {
    "groq": os.getenv("GROQ_API_KEY"),
    "mistral": os.getenv("MISTRAL_API_KEY"),
    "cohere": os.getenv("COHERE_API_KEY")
}

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def clean_ai_text(text):
    if not text: return ""
    # Удаляем ** и любые упоминания количества слов (29 слов), 30 слов и т.д.
    text = text.replace("**", "")
    text = re.sub(r"\(?\d+\s*слов\)?", "", text, flags=re.IGNORECASE)
    return text.strip()

def call_ai(api_name, text):
    prompt = f"Сделай краткое резюме ОДНИМ предложением (до 30 слов) на русском. Не используй Markdown. Статья: {text[:3800]}"
    try:
        res = None
        if api_name == "groq" and KEYS["groq"]:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']
        
        elif api_name == "mistral" and KEYS["mistral"]:
            r = requests.post("https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['mistral']}"},
                json={"model": "mistral-small-latest", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']

        elif api_name == "cohere" and KEYS["cohere"]:
            r = requests.post("https://api.cohere.ai/v1/chat", headers={"Authorization": f"Bearer {KEYS['cohere']}"},
                json={"message": prompt, "model": "command-r-plus"}, timeout=25)
            if r.status_code == 200: res = r.json().get('text')

        if res:
            log(f"📡 [{api_name.upper()}] Ответ получен успешно")
            return clean_ai_text(res)
    except Exception as e:
        log(f"❌ [{api_name.upper()}] Ошибка: {str(e)[:50]}")
    return None

def extract_full_text(item):
    """Улучшенное вытаскивание текста из RSS/FreshRSS API"""
    # Ищем во всех возможных полях, где может лежать текст поста (актуально для Telegram RSS)
    raw = (item.get('content', {}).get('content') or 
           item.get('summary', {}).get('content') or 
           item.get('summary') or 
           item.get('content') or "")
    
    if not raw or len(raw) < 20: # Если в полях пусто, берем заголовок как крайний случай
        raw = item.get('title', "")
        
    soup = BeautifulSoup(raw, "html.parser")
    # Проверяем наличие видео/медиа
    has_video = bool(soup.find(['video', 'iframe', 'embed', 'img'])) or ".mp4" in str(raw).lower()
    for s in soup(["script", "style"]): s.decompose()
    clean_text = " ".join(soup.get_text(separator=' ').split())
    return clean_text, has_video

def send_tg(text, disable_preview, show_above=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML",
        "link_preview_options": {
            "is_disabled": disable_preview,
            "show_above_text": show_above # Превью над текстом для YouTube
        }
    }
    res = requests.post(url, json=payload)
    return res.status_code == 200

def process_item(item, api_name, is_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    feed_title = item.get('origin', {}).get('title', 'Source')
    
    # Извлекаем полный текст вместо простого заголовка
    full_text, has_v = extract_full_text(item)
    
    domain = urlparse(link).netloc.lower()
    is_yt = any(x in domain for x in ["youtube.com", "youtu.be"])
    
    # Формируем тег источника
    if is_yt or "t.me" in domain:
        tag = feed_title.replace(" ", "").replace("#", "")
    else:
        tag = domain.replace("www.", "").split('.')[0].capitalize()
    
    source_tag = f"#{tag}"
    video_marker = "🎬 " if (has_v or is_yt) else ""
    
    if is_ai:
        summary = call_ai(api_name, full_text)
        content = summary if summary else item.get('title')
        line = f"📌 <a href='{link}'>→</a> {content}\n{video_marker}🏷️ {source_tag}"
    else:
        # Прямая ссылка для YouTube/Direct
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n{video_marker}🏷️ {source_tag}"

    return {"id": item.get('id'), "line": line}

def process_category(cat_name, use_ai, headers, api_base):
    start_time = time.time()
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", 
                    params={'n': 50, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    
    count = len(items)
    log(f"📥 Получено новостей: {count}")
    if count == 0: return

    final_results = []
    if use_ai:
        active_apis = [a for a in ["groq", "mistral", "cohere"] if KEYS.get(a)]
        chunks = [items[i::len(active_apis)] for i in range(len(active_apis))]
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(lambda c, a: [process_item(it, a, True) for it in c], chunks[i], active_apis[i]) 
                       for i in range(len(chunks)) if chunks[i]]
            for f in as_completed(futures): final_results.extend(f.result())
    else:
        final_results = [process_item(it, "direct", False) for it in items]

    if final_results:
        cat_tag = f"#{cat_name.replace(' ', '')}"
        # Убираем заголовок #YouTube, если он совпадает с категорией
        msg = "" if cat_tag.lower() == "#youtube" else f"{cat_tag}\n\n"
        
        items_to_mark = []
        for entry in final_results:
            line = entry['line'] + "\n\n"
            
            if not use_ai: # Для YouTube отправляем поштучно для красивого превью СВЕРХУ
                send_tg(line.strip(), disable_preview=False, show_above=True)
                requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': entry['id'], 'a': 'user/-/state/com.google/read'})
                continue

            if len(msg) + len(line) > 4000:
                if send_tg(msg.strip(), disable_preview=True):
                    for i in items_to_mark: requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': i, 'a': 'user/-/state/com.google/read'})
                msg = f"{cat_tag}\n\n"
                items_to_mark = []
            
            msg += line
            items_to_mark.append(entry['id'])
        
        if items_to_mark and msg and send_tg(msg.strip(), disable_preview=True):
            for i in items_to_mark: 
                requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': i, 'a': 'user/-/state/com.google/read'})

    duration = time.time() - start_time
    log(f"⏱️ Обработка '{cat_name}' завершена за {duration:.2f} сек.")

def main():
    log("🏁 ЗАПУСК ОБНОВЛЕННОГО БОТА")
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth: return log("❌ Ошибка входа")
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    
    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
    log("✅ ВСЕ ЗАДАЧИ ВЫПОЛНЕНЫ")

if __name__ == "__main__": main()
      
