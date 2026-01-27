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
    prompt = f"Сделай краткое резюме ОДНИМ предложением (до 30 слов) на русском: {text[:3500]}"
    try:
        if api_name == "gemini":
            with DDGS() as ddgs:
                # Исправленный вызов для версии 7.0+
                res = ddgs.chat(prompt, model='gpt-4o-mini')
                return res.strip() if res else None
        # ... остальные API (Groq, Mistral, Cohere, HF) остаются как в прошлом коде
        elif api_name == "groq" and KEYS["groq"]:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            return r.json()['choices'][0]['message']['content'].strip()
        # (Аналогично для Mistral, Cohere и HF из предыдущего скрипта)
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
    
    if is_ai:
        summary = call_ai(api_name, text) if len(text) > 100 else None
        content = summary if summary else item.get('title')
        # Для AI: ссылка-стрелка, текст, затем эмодзи и тег с новой строки
        line = f"📌 <a href='{link}'>→</a> {content}\n{video_marker}🏷️ {source_tag}"
    else:
        # Для YouTube/Direct: ссылка-заголовок
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n{video_marker}🏷️ {source_tag}"

    return {"id": item.get('id'), "line": line}

def send_tg(text, disable_preview):
    # Очистка от мусорных тегов типа <plaintext>
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

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    # ... логика получения items и многопоточности ...
    # (пропускаю для краткости, она идентична прошлой версии)
    
    # При отправке:
    # disable_preview = True для AI категорий
    # disable_preview = False для Direct (YouTube) категорий
    if final_results:
        msg = f"#{cat_name.replace(' ', '')}\n\n"
        # ... цикл сборки сообщения ...
        send_tg(msg, disable_preview=use_ai) 
 
