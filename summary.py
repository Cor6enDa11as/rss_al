#!/usr/bin/env python3
import requests
import os
import time
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
# Обновленный импорт для новых версий
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

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

def clean_ai_text(text):
    if not text: return ""
    # Убираем жирный шрифт Markdown (**)
    text = text.replace("**", "")
    # Убираем конструкции типа (29 слов), (30 слов) и т.д.
    text = re.sub(r"\(?\d+\s*слов\)?", "", text, flags=re.IGNORECASE)
    return text.strip()

def call_ai(api_name, text):
    # Более строгий промпт
    prompt = f"Сделай краткое резюме ОДНИМ предложением (до 30 слов) на русском. Не используй Markdown и не пиши количество слов в ответе. Статья: {text[:3500]}"
    try:
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
            # Добавили wait_for_model: True
            r = requests.post(API_URL, headers={"Authorization": f"Bearer {KEYS['hf']}"},
                json={"inputs": f"User: {prompt}\nAssistant:", "parameters": {"max_new_tokens": 100}, "options": {"wait_for_model": True}}, timeout=40)
            if r.status_code == 200:
                out = r.json()
                res = out[0].get('generated_text', '').split("Assistant:")[-1] if isinstance(out, list) else out.get('generated_text', '')

        if res:
            return clean_ai_text(res)
    except Exception as e:
        log(f"❌ [{api_name.upper()}] Ошибка: {str(e)[:50]}")
    return None

def process_item(item, api_name, is_ai):
    from urllib.parse import urlparse
    link = item.get('alternate', [{}])[0].get('href', '')
    feed_title = item.get('origin', {}).get('title', 'Source')
    raw_html = item.get('summary', {}).get('content') or item.get('content', {}).get('content') or ""
    
    source_tag = get_source_tag(link, feed_title)
    
    if "t.me" in link:
        text, has_v = clean_html(raw_html)
    else:
        try:
            r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            text, has_v = clean_html(r.text)
        except: text, has_v = clean_html(raw_html)

    video_marker = "🎬 " if (has_v or "youtube" in link.lower() or "youtu.be" in link.lower()) else ""
    
    if is_ai:
        summary = call_ai(api_name, text) if len(text) > 100 else None
        content = summary if summary else item.get('title')
        # Перенос хэштега на новую строку
        line = f"📌 <a href='{link}'>→</a> {content}\n{video_marker}🏷️ {source_tag}"
    else:
        # Для YouTube (Direct) - превью будет включено, заголовок - ссылка
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n{video_marker}🏷️ {source_tag}"

    return {"id": item.get('id'), "line": line}

def clean_html(raw_html):
    if not raw_html: return "", False
    soup = BeautifulSoup(raw_html, "html.parser")
    has_video = bool(soup.find(['video', 'iframe', 'embed'])) or ".mp4" in raw_html.lower()
    for s in soup(["script", "style"]): s.decompose()
    text = " ".join(soup.get_text(separator=' ').split())
    return text, has_video

def send_tg(text, disable_preview):
    allowed = ['a', 'b', 'i', 'strong', 'em']
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in allowed: tag.unwrap()
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": str(soup), 
        "parse_mode": "HTML", 
        "disable_web_page_preview": disable_preview
    }
    res = requests.post(url, data=payload)
    return res.status_code == 200

def mark_as_read(ids, headers, api_base):
    for item_id in ids:
        requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item_id, 'a': 'user/-/state/com.google/read'})

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", 
                    params={'n': 40, 'xt': 'user/-/state/com.google/read'}, headers=headers)
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
        msg = f"{cat_tag}\n\n"
        items_to_mark = []
        for entry in final_results:
            line = entry['line'] + "\n\n"
            if len(msg) + len(line) > 4000:
                if send_tg(msg.strip(), disable_preview=use_ai): mark_as_read(items_to_mark, headers, api_base)
                msg = f"{cat_tag}\n\n"
                items_to_mark = []
            msg += line
            items_to_mark.append(entry['id'])
        
        if items_to_mark and send_tg(msg.strip(), disable_preview=use_ai):
            mark_as_read(items_to_mark, headers, api_base)

def api_worker(items_chunk, api_name, is_ai):
    return [process_item(it, api_name, is_ai) for it in items_chunk]

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
