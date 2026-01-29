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
    # Тот самый универсальный промпт "Максимум смысла"
    prompt = f"Сформулируй главную новость текста одним ёмким предложением на русском языке (строго до 30 слов). Передай конкретный результат или ключевое событие, избегая общих фраз. Запрещено: использовать Markdown (**), писать количество слов в скобках и начинать с вводных оборотов вроде 'Статья рассказывает...' или 'Автор пишет...'. Только чистый, плотный текст. Статья: {text[:3800]}"
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
    """Улучшенное вытаскивание текста: ищем самый длинный контент (для Telegram)"""
    candidates = [
        item.get('content', {}).get('content'),
        item.get('summary', {}).get('content'),
        item.get('summary'),
        item.get('content'),
        item.get('description') # Добавили прямую проверку описания
    ]
    # Выбираем самый длинный текст из доступных полей
    valid_texts = [str(c) for c in candidates if c and len(str(c)) > 0]
    raw = max(valid_texts, key=len) if valid_texts else item.get('title', "")

    soup = BeautifulSoup(raw, "html.parser")
    # Видео-детектор: УБРАЛИ 'img', оставили только плееры и файлы
    has_video = bool(soup.find(['video', 'iframe', 'embed'])) or ".mp4" in str(raw).lower()

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
            "show_above_text": show_above
        }
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        return res.status_code == 200
    except:
        return False

def process_item(item, api_name, is_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    feed_title = item.get('origin', {}).get('title', 'Source')

    full_text, has_v = extract_full_text(item)
    domain = urlparse(link).netloc.lower()
    is_yt = any(x in domain for x in ["youtube.com", "youtu.be"])

    # --- НОВАЯ ЛОГИКА ХЭШТЕГОВ (Очистка и объединение) ---
    # 1. Берем часть до первого дефиса или скобки
    clean_name = re.split(r'[-—(]', feed_title)[0].strip()
    # 2. Оставляем только буквы и цифры, убираем пробелы
    clean_name = "".join(re.findall(r'[a-zA-Zа-яА-Я0-9]+', clean_name))
    source_tag = f"#{clean_name}"

    # Видео ставим только на реальное видео или YouTube
    video_marker = "🎬 " if (has_v or is_yt) else ""

    if is_ai:
        summary = call_ai(api_name, full_text)
        content = summary if summary else item.get('title')
        line = f"📌 <a href='{link}'>→</a> {content} {video_marker}\n🏷️ {source_tag}"
    else:
        line = f"📌 <a href='{link}'>{item.get('title')}</a>\n🏷️ {source_tag}"

    return {"id": item.get('id'), "line": line}

def mark_read(api_base, headers, ids):
    if not ids: return
    try:
        data = [('i', i_id) for i_id in ids]
        data.append(('a', 'user/-/state/com.google/read'))
        requests.post(f"{api_base}/edit-tag", headers=headers, data=data, timeout=20)
    except Exception as e:
        log(f"⚠️ Ошибка отметки прочитанным: {str(e)[:50]}")

def process_category(cat_name, use_ai, headers, api_base):
    start_time = time.time()
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")

    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                        params={'n': 50, 'xt': 'user/-/state/com.google/read'}, headers=headers, timeout=20)
        items = r.json().get('items', [])
    except:
        log("❌ Ошибка получения данных из FreshRSS")
        return

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
        msg = "" if cat_tag.lower() == "#youtube" else f"{cat_tag}\n\n"

        items_to_mark = []
        for entry in final_results:
            line = entry['line'] + "\n\n"

            if not use_ai: # YouTube/Direct
                if send_tg(line.strip(), disable_preview=False, show_above=True):
                    mark_read(api_base, headers, [entry['id']])
                continue

            if len(msg) + len(line) > 4000:
                if send_tg(msg.strip(), disable_preview=True):
                    mark_read(api_base, headers, items_to_mark)
                msg = f"{cat_tag}\n\n"
                items_to_mark = []

            msg += line
            items_to_mark.append(entry['id'])

        if items_to_mark and msg and send_tg(msg.strip(), disable_preview=True):
            mark_read(api_base, headers, items_to_mark)

    duration = time.time() - start_time
    log(f"⏱️ Обработка '{cat_name}' завершена за {duration:.2f} сек.")

def main():
    log("🏁 ЗАПУСК ОБНОВЛЕННОГО БОТА")
    try:
        auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}", timeout=20)
        auth = re.search(r'Auth=(.*)', auth_res.text)
        if not auth: return log("❌ Ошибка входа")
        headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
        api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

        for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
        for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
        log("✅ ВСЕ ЗАДАЧИ ВЫПОЛНЕНЫ")
    except Exception as e:
        log(f"❌ Критическая ошибка: {e}")

if __name__ == "__main__":
    main()
