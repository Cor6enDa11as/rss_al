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

# Порядок сохранен + добавлен Gemini
KEYS = {
    "groq": os.getenv("GROQ_API_KEY"),
    "mistral": os.getenv("MISTRAL_API_KEY"),
    "cohere": os.getenv("COHERE_API_KEY"),
    "gemini": os.getenv("GEMINI_API_KEY")
}

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def clean_ai_text(text):
    if not text: return ""
    text = text.replace("**", "")
    text = re.sub(r"\(?\d+\s*слов\)?", "", text, flags=re.IGNORECASE)
    return text.strip()

def call_ai(api_name, text):
    # Защита: если текста почти нет, не мучаем ИИ
    if not text or len(text) < 150:
        log(f"⚠️ [AI-{api_name.upper()}] Текст слишком короткий для анализа")
        return None

    prompt = f"Сформулируй суть текста одним ёмким предложением на русском языке (30 слов). Передай результат, избегая общих фраз. Запрещено: Markdown, скобки с числом слов, вводные фразы. Только чистый текст. Статья: {text[:3800]}"
    try:
        log(f"🤖 [AI] Запрос к {api_name.upper()}...")
        res = None
        if api_name == "groq" and KEYS["groq"]:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']
            else: log(f"❌ [AI ERROR] Groq вернул {r.status_code}: {r.text[:200]}")

        elif api_name == "mistral" and KEYS["mistral"]:
            r = requests.post("https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['mistral']}"},
                json={"model": "mistral-small-latest", "messages": [{"role": "user", "content": prompt}]}, timeout=25)
            if r.status_code == 200: res = r.json()['choices'][0]['message']['content']
            else: log(f"❌ [AI ERROR] Mistral вернул {r.status_code}: {r.text[:200]}")

        elif api_name == "cohere" and KEYS["cohere"]:
            r = requests.post("https://api.cohere.ai/v1/chat", headers={"Authorization": f"Bearer {KEYS['cohere']}"},
                json={"message": prompt, "model": "command-r-08-2024"}, timeout=25)
            if r.status_code == 200: res = r.json().get('text')
            else: log(f"❌ [AI ERROR] Cohere вернул {r.status_code}: {r.text[:200]}")

        elif api_name == "gemini" and KEYS["gemini"]:
            # Добавлен Gemini 2.0 Flash (актуальный на 2026 год)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={KEYS['gemini']}"
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=25)
            if r.status_code == 200: res = r.json()['candidates'][0]['content']['parts'][0]['text']
            else: log(f"❌ [AI ERROR] Gemini вернул {r.status_code}: {r.text[:200]}")

        if res:
            log(f"✅ [AI] {api_name.upper()} успешно ответил")
            return clean_ai_text(res)
    except Exception as e:
        log(f"❌ [AI CRITICAL] {api_name.upper()}: {str(e)}")
    return None

def scrape_full_text(url):
    try:
        log(f"🌐 [SCRAPER] Попытка парсинга ссылки: {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
            'Referer': 'https://www.google.com/',
            'DNT': '1'
        }
        r = requests.get(url, timeout=15, headers=headers)
        if r.status_code != 200:
            log(f"⚠️ [SCRAPER] Ошибка доступа ({r.status_code})")
            return ""
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(["script", "style", "nav", "header", "footer"]): s.decompose()
        paragraphs = soup.find_all('p')
        text = " ".join([p.get_text() for p in paragraphs])
        if len(text) > 100:
            log(f"✅ [SCRAPER] Успех! Извлечено {len(text)} симв.")
            return text
        log("⚠️ [SCRAPER] Текст слишком короткий")
        return ""
    except Exception as e:
        log(f"❌ [SCRAPER ERROR] {str(e)}")
        return ""

def extract_content(item, is_tg, is_yt):
    raw = ""
    source_field = ""
    if is_tg:
        if item.get('description'):
            raw = item['description']
            source_field = "description"
        else:
            raw = item.get('content', {}).get('content') or item.get('summary', {}).get('content')
            source_field = "content/summary"
        log(f"🔍 [PARSER-TG] Взято из поля: {source_field}")
    else:
        raw = item.get('content', {}).get('content') or item.get('summary', {}).get('content') or item.get('description')
        source_field = "RSS content/summary"

    raw = raw or item.get('title', "")
    soup = BeautifulSoup(str(raw), "html.parser")

    if is_tg:
        for junk in soup.find_all(['a', 'img'], limit=3):
            junk.decompose()

    has_v = False
    if not is_yt:
        has_v = bool(soup.find(['video', 'iframe', 'embed'])) or ".mp4" in str(raw).lower()

    clean_text = " ".join(soup.get_text(separator=' ').split())
    link = item.get('alternate', [{}])[0].get('href', '')

    if is_tg or is_yt:
        return clean_text, has_v, link
    else:
        web_text = scrape_full_text(link)
        final_text = (web_text if len(web_text) > len(clean_text) else clean_text)
        log(f"📄 [CONTENT] Итоговый объем текста: {len(final_text)} симв.")
        return final_text, has_v, link

def get_hashtag(feed_title, link, is_tg_yt):
    if is_tg_yt:
        name = re.split(r'\s+[-—]\s*|\s+\(', feed_title)[0].strip()
    else:
        domain = urlparse(link).netloc.lower().replace('www.', '')
        name = domain.split('.')[0].capitalize()
    clean = "".join(re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+', name))
    return f"#{clean}"

def process_item(item, api_name, is_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    feed_title = item.get('origin', {}).get('title', 'Source')
    domain = urlparse(link).netloc.lower()
    is_tg, is_yt = "t.me" in domain, any(x in domain for x in ["youtube.com", "youtu.be"])

    log(f"📰 [PROCESS] Старт: {item.get('title', '...')[:50]}")
    full_text, has_v, link = extract_content(item, is_tg, is_yt)
    tag = get_hashtag(feed_title, link, (is_tg or is_yt))
    v_mark = "🎬 " if (has_v and not is_yt) else ""

    if is_ai:
        summary = call_ai(api_name, full_text)
        content = summary if summary else item.get('title', 'Без заголовка')
        line = f"📌 <a href='{link}'>→</a> <i>{content}</i> {v_mark}\n🏷️ {tag}"
    else:
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n🏷️ {tag}"
    return {"id": item.get('id'), "line": line, "is_yt": is_yt}

def mark_read(api_base, headers, ids):
    if not ids: return
    try:
        data = [('i', i_id) for i_id in ids]
        data.append(('a', 'user/-/state/com.google/read'))
        requests.post(f"{api_base}/edit-tag", headers=headers, data=data, timeout=20)
    except: pass

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                        params={'n': 50, 'xt': 'user/-/state/com.google/read'}, headers=headers, timeout=20)
        items = r.json().get('items', [])
        log(f"📥 Найдено новых новостей: {len(items)}")
    except: return
    if not items: return
    final_results = []
    if use_ai:
        # Gemini добавлен в список активных API
        active_apis = [a for a in ["groq", "mistral", "cohere", "gemini"] if KEYS.get(a)]
        chunks = [items[i::len(active_apis)] for i in range(len(active_apis))]
        with ThreadPoolExecutor(max_workers=len(active_apis)) as ex:
            futures = [ex.submit(lambda c, a: [process_item(it, a, True) for it in c], chunks[i], active_apis[i])
                       for i in range(len(chunks)) if chunks[i]]
            for f in as_completed(futures): final_results.extend(f.result())
    else: final_results = [process_item(it, "direct", False) for it in items]

    cat_tag = f"#{cat_name.replace(' ', '')}"
    msg, ids_to_mark = ("" if cat_tag.lower() == "#youtube" else f"{cat_tag}\n\n"), []

    for entry in final_results:
        if not use_ai or entry.get('is_yt'):
            if requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                             json={"chat_id": CHAT_ID, "text": entry['line'], "parse_mode": "HTML",
                                   "link_preview_options": {"show_above_text": True}}).status_code == 200:
                mark_read(api_base, headers, [entry['id']])
            continue
        if len(msg) + len(entry['line']) > 4000:
            if requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                             json={"chat_id": CHAT_ID, "text": msg.strip(), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}).status_code == 200:
                mark_read(api_base, headers, ids_to_mark)
            msg, ids_to_mark = f"{cat_tag}\n\n", []
        msg += entry['line'] + "\n\n"
        ids_to_mark.append(entry['id'])

    if ids_to_mark and msg:
        if requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         json={"chat_id": CHAT_ID, "text": msg.strip(), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}).status_code == 200:
            mark_read(api_base, headers, ids_to_mark)

def main():
    log("🏁 ЗАПУСК")
    try:
        auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}", timeout=20)
        auth = re.search(r'Auth=(.*)', auth_res.text)
        if not auth:
            log("❌ Ошибка авторизации FreshRSS")
            return
        headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
        api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
        for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
        for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
        log("✅ ГОТОВО")
    except Exception as e: log(f"❌ Критическая ошибка: {e}")

if __name__ == "__main__": main()
