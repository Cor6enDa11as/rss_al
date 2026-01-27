#!/usr/bin/env python3
import requests
import os
import time
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from duckduckgo_search import DDGS # Версия 6.2.4

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

def clean_ai_text(text):
    if not text: return ""
    text = text.replace("**", "")
    text = re.sub(r"\(?\d+\s*слов\)?", "", text, flags=re.IGNORECASE)
    return text.strip()

def call_ai(api_name, text):
    prompt = f"Сделай краткое резюме ОДНИМ предложением (до 30 слов) на русском. Без Markdown. Статья: {text[:3500]}"
    try:
        res = None
        if api_name == "gemini":
            with DDGS() as ddgs:
                # В версии 6.2.4 этот метод работает стабильно
                res = ddgs.chat(prompt, model='gpt-4o-mini')
        elif api_name == "groq" and KEYS["groq"]:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            log(f"📡 [GROQ] Status: {r.status_code}")
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']
        elif api_name == "mistral" and KEYS["mistral"]:
            r = requests.post("https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['mistral']}"},
                json={"model": "mistral-small-latest", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            log(f"📡 [MISTRAL] Status: {r.status_code}")
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']
        elif api_name == "hf" and KEYS["hf"]:
            API_URL = "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-72B-Instruct"
            r = requests.post(API_URL, headers={"Authorization": f"Bearer {KEYS['hf']}"},
                json={"inputs": f"User: {prompt}\nAssistant:", "parameters": {"max_new_tokens": 100}, "options": {"wait_for_model": True}}, timeout=40)
            log(f"📡 [HF] Status: {r.status_code}")
            if r.status_code == 200:
                out = r.json()
                res = out[0].get('generated_text', '').split("Assistant:")[-1] if isinstance(out, list) else out.get('generated_text', '')

        if res: return clean_ai_text(res)
    except Exception as e:
        log(f"❌ [{api_name.upper()}] API LOG: {str(e)}")
    return None

def send_tg(text, disable_preview, show_above=False):
    allowed = ['a', 'b', 'i', 'strong', 'em']
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in allowed: tag.unwrap()
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": str(soup), 
        "parse_mode": "HTML",
        "link_preview_options": {
            "is_disabled": disable_preview,
            "show_above_text": show_above # Превью СВЕРХУ для YouTube
        }
    }
    res = requests.post(url, json=payload) # Используем JSON для вложенных настроек
    return res.status_code == 200

def get_source_tag(link, feed_title):
    domain = urlparse(link).netloc.lower()
    tag = feed_title.replace(" ", "").replace("#", "") if any(x in domain for x in ["youtube.com", "youtu.be", "t.me"]) else domain.replace("www.", "").split('.')[0].capitalize()
    return f"#{tag}"

def clean_html(raw_html):
    if not raw_html: return "", False
    soup = BeautifulSoup(raw_html, "html.parser")
    has_video = bool(soup.find(['video', 'iframe', 'embed'])) or ".mp4" in raw_html.lower()
    for s in soup(["script", "style"]): s.decompose()
    return " ".join(soup.get_text(separator=' ').split()), has_video

def process_item(item, api_name, is_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    raw_html = item.get('summary', {}).get('content') or item.get('content', {}).get('content') or ""
    
    try:
        if "t.me" in link: text, has_v = clean_html(raw_html)
        else:
            r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            text, has_v = clean_html(r.text)
    except: text, has_v = clean_html(raw_html)

    source_tag = get_source_tag(link, item.get('origin', {}).get('title', 'Source'))
    video_marker = "🎬 " if (has_v or "youtube" in link.lower() or "youtu.be" in link.lower()) else ""
    
    if is_ai:
        summary = call_ai(api_name, text) if len(text) > 100 else None
        content = summary if summary else item.get('title')
        line = f"📌 <a href='{link}'>→</a> {content}\n{video_marker}🏷️ {source_tag}"
    else:
        # Для YouTube: заголовок-ссылка снизу
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n{video_marker}🏷️ {source_tag}"

    return {"id": item.get('id'), "line": line}

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", 
                    params={'n': 10 if not use_ai else 40, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items: return log("☕ Пусто.")

    final_results = []
    if use_ai:
        active_apis = [a for a in ["gemini", "groq", "mistral", "cohere", "hf"] if (a == "gemini" or KEYS.get(a))]
        n = len(active_apis)
        chunks = [items[i::n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(lambda c, a: [process_item(it, a, True) for it in c], chunks[i], active_apis[i]) for i in range(len(chunks))]
            for f in as_completed(futures): final_results.extend(f.result())
    else:
        final_results = [process_item(it, "direct", False) for it in items]

    if final_results:
        cat_tag = f"#{cat_name.replace(' ', '')}"
        msg = f"{cat_tag}\n\n"
        items_to_mark = []
        for entry in final_results:
            line = entry['line'] + "\n\n"
            # Если это YouTube (Direct), лучше отправлять по одному для корректных превью СВЕРХУ
            if not use_ai:
                if send_tg(cat_tag + "\n\n" + line.strip(), disable_preview=False, show_above=True):
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': entry['id'], 'a': 'user/-/state/com.google/read'})
                continue

            if len(msg) + len(line) > 4000:
                if send_tg(msg.strip(), disable_preview=True):
                    for i in items_to_mark: requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': i, 'a': 'user/-/state/com.google/read'})
                msg, items_to_mark = f"{cat_tag}\n\n", []
            msg += line
            items_to_mark.append(entry['id'])
        
        if items_to_mark and send_tg(msg.strip(), disable_preview=True):
            for i in items_to_mark: requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': i, 'a': 'user/-/state/com.google/read'})

def main():
    log("🏁 ЗАПУСК")
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth: return log("❌ Ошибка входа")
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
    log("✅ ЗАВЕРШЕНО")

if __name__ == "__main__": main()
