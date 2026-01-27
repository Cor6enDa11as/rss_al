#!/usr/bin/env python3
import requests
import os
import time
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from duckduckgo_search import DDGS
from urllib.parse import urlparse

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

KEYS = {
    "groq": os.getenv("GROQ_API_KEY"),
    "mistral": os.getenv("MISTRAL_API_KEY"),
    "cohere": os.getenv("COHERE_API_KEY"),
    "gemini": os.getenv("GEMINI_API_KEY"),
    "hf": os.getenv("HF_TOKEN")
}

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def get_source_tag(link, feed_title):
    domain = urlparse(link).netloc.lower()
    if any(x in domain for x in ["youtube.com", "youtu.be", "t.me"]):
        tag = feed_title.replace(" ", "").replace("#", "")
    else:
        tag = domain.replace("www.", "").split('.')[0].capitalize()
    return f"#{tag}"

def clean_html(raw_html):
    if not raw_html: return "", False
    soup = BeautifulSoup(raw_html, "html.parser")
    has_video = bool(soup.find(['video', 'iframe', 'embed'])) or ".mp4" in raw_html.lower()
    for s in soup(["script", "style"]): s.decompose()
    text = " ".join(soup.get_text(separator=' ').split())
    return text, has_video

def call_ai(api_name, text):
    char_count = len(text)
    prompt = f"Сделай краткое резюме ОДНИМ предложением (до 30 слов) на русском: {text[:3500]}"
    try:
        start_time = time.time()
        res = None
        if api_name == "gemini":
            with DDGS() as ddgs:
                res = ddgs.chat(prompt, model='gpt-4o-mini')
        elif api_name == "groq" and KEYS["groq"]:
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
                json={"message": prompt, "model": "command-r-08-2024"}, timeout=25)
            if r.status_code == 200: res = r.json().get('text')
        elif api_name == "hf" and KEYS["hf"]:
            API_URL = "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-72B-Instruct"
            r = requests.post(API_URL, headers={"Authorization": f"Bearer {KEYS['hf']}"},
                json={"inputs": f"User: {prompt}\nAssistant:", "parameters": {"max_new_tokens": 100}}, timeout=30)
            if r.status_code == 200:
                out = r.json()
                res = out[0].get('generated_text', '').split("Assistant:")[-1] if isinstance(out, list) else out.get('generated_text', '')

        duration = round(time.time() - start_time, 2)
        if res:
            log(f"✅ [{api_name.upper()}] Обработано {char_count} симв. за {duration}с")
            return res.strip()
    except Exception as e:
        log(f"❌ [{api_name.upper()}] Ошибка: {str(e)[:50]}")
    return None

def process_item(item, api_name, is_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    feed_title = item.get('origin', {}).get('title', 'Source')
    raw_html = item.get('summary', {}).get('content') or item.get('content', {}).get('content') or ""
    
    if "t.me" in link:
        text, has_v = clean_html(raw_html)
    else:
        try:
            r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            text, has_v = clean_html(r.text)
        except: text, has_v = clean_html(raw_html)

    source_tag = get_source_tag(link, feed_title)
    video_marker = "🎬 " if (has_v or "youtube" in link.lower() or "youtu.be" in link.lower()) else ""
    
    if is_ai and len(text) > 100:
        summary = call_ai(api_name, text)
        content = summary if summary else item.get('title')
    else:
        content = item.get('title')

    return {"id": item.get('id'), "line": f"📌 <a href='{link}'>→</a> {content} {video_marker}🏷️ {source_tag}"}

def api_worker(items_chunk, api_name, is_ai):
    log(f"🧬 [{api_name.upper()}] Поток взял {len(items_chunk)} задач")
    return [process_item(it, api_name, is_ai) for it in items_chunk]

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", 
                    params={'n': 50, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items: return log("☕ Пусто.")

    final_results = []
    if use_ai:
        active_apis = [a for a in ["gemini", "groq", "mistral", "cohere", "hf"] if (a == "gemini" or KEYS.get(a))]
        n = len(active_apis)
        chunks = [items[i::n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(api_worker, chunks[i], active_apis[i], True) for i in range(len(chunks))]
            for f in as_completed(futures): final_results.extend(f.result())
    else:
        final_results = [process_item(it, "direct", False) for it in items]

    if final_results:
        cat_tag = f"#{cat_name.replace(' ', '')}"
        current_msg = f"{cat_tag}\n\n"
        items_to_mark = []

        for entry in final_results:
            line = entry['line'] + "\n\n"
            if len(current_msg) + len(line) > 4000:
                if send_tg(current_msg.strip()):
                    mark_as_read(items_to_mark, headers, api_base)
                current_msg = f"{cat_tag} (продолжение)\n\n"
                items_to_mark = []
            
            current_msg += line
            items_to_mark.append(entry['id'])
        
        # Финальная отправка остатка
        if items_to_mark and send_tg(current_msg.strip()):
            mark_as_read(items_to_mark, headers, api_base)

def send_tg(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
    if res.status_code == 200:
        log("📬 Сообщение успешно отправлено в Telegram")
        return True
    log(f"❌ Ошибка Telegram: {res.text}")
    return False

def mark_as_read(ids, headers, api_base):
    if not ids: return
    # Помечаем пачкой для экономии запросов
    for item_id in ids:
        requests.post(f"{api_base}/edit-tag", headers=headers, 
                     data={'i': item_id, 'a': 'user/-/state/com.google/read'})
    log(f"📖 {len(ids)} новостей помечены как прочитанные")

def main():
    log("🏁 ЗАПУСК")
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth: return log("❌ Ошибка авторизации")
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    
    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
    log("✅ ЗАВЕРШЕНО")

if __name__ == "__main__": main()
