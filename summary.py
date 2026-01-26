#!/usr/bin/env python3
import requests
import json
import os
import time
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

# Ключи API
KEYS = {
    "gemini": os.getenv("GEMINI_API_KEY"),
    "groq": os.getenv("GROQ_API_KEY"),
    "mistral": os.getenv("MISTRAL_API_KEY"),
    "cohere": os.getenv("COHERE_API_KEY")
}

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def call_ai(api_name, text, link_hint):
    """Фокусированный промпт на 30 слов и жесткий фильтр дублей"""
    prompt = (
        "Инструкция: Опиши суть новости ОДНИМ предложением (макс. 30 слов) на русском языке. "
        "Фокус: Кто сделал? Что произошло? Какой результат? "
        "ПРАВИЛО SKIP: Если новость полностью идентична предыдущим по смыслу, ответь ТОЛЬКО словом SKIP. "
        f"Текст для анализа: {text[:3500]}"
    )

    try:
        # --- ЛОГИКА ВЫЗОВОВ ---
        if api_name == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={KEYS['gemini']}"
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
            return r.json()['candidates'][0]['content']['parts'][0]['text'], "💎"

        elif api_name == "groq":
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=20)
            return r.json()['choices'][0]['message']['content'], "⚡"

        elif api_name == "mistral":
            r = requests.post("https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['mistral']}"},
                json={"model": "mistral-small-latest", "messages": [{"role": "user", "content": prompt}]}, timeout=20)
            return r.json()['choices'][0]['message']['content'], "🌀"

        elif api_name == "cohere":
            r = requests.post("https://api.cohere.ai/v1/chat",
                headers={"Authorization": f"Bearer {KEYS['cohere']}"},
                json={"message": prompt, "model": "command-r-plus"}, timeout=20)
            return r.json()['text'], "🟣"

    except Exception as e:
        log(f"⚠️ {api_name.upper()} ошибка на {link_hint[:30]}: {str(e)}")
    return None, ""

def process_batch(api_name, items):
    """Обработка группы новостей одним API"""
    results = []
    log(f"[{api_name.upper()}] Начинаю обработку пакета из {len(items)} шт.")

    for item in items:
        link = item.get('alternate', [{}])[0].get('href', '')
        try:
            r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
            soup = BeautifulSoup(r.text, 'html.parser')
            article = soup.find('div', {'class': ['tm-article-body', 'post__text', 'article-body']}) or soup.find('article')
            text = article.get_text(separator=' ', strip=True) if article else soup.get_text(strip=True)
            text = " ".join(text.split())[:3500]

            summary, emoji = call_ai(api_name, text, link)

            if summary and "SKIP" not in summary.upper():
                is_video = any(x in link.lower() for x in ["youtube.com", "youtu.be"])
                clean_res = summary.strip().rstrip('.') + (" 🎬" if is_video else "")
                results.append({"link": link, "content": f"{emoji} {clean_res}", "id": item.get('id')})
                log(f"✅ {api_name.upper()} готов: {link[:30]}...")
            else:
                log(f"⏭️ {api_name.upper()} пропустил дубль: {link[:30]}...")
        except Exception as e:
            log(f"❌ Ошибка в потоке {api_name} на {link[:30]}: {e}")

    return results

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                    params={'n': 32, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items: return log(f"☕ {cat_name} пуст.")

    if not use_ai:
        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if send_tg(f"📍 <b><a href='{link}'>{item.get('title')}</a></b>", True):
                mark_as_read(api_base, headers, item.get('id'))
        return

    # Разделение на 4 пакета
    active_apis = [name for name, key in KEYS.items() if key]
    n_api = len(active_apis)
    chunks = [items[i::n_api] for i in range(n_api)]

    log(f"📦 Распределение: " + ", ".join([f"{active_apis[i]}: {len(chunks[i])}" for i in range(n_api)]))

    final_valid_results = []
    with ThreadPoolExecutor(max_workers=n_api) as executor:
        future_to_api = {executor.submit(process_batch, active_apis[i], chunks[i]): active_apis[i] for i in range(n_api)}
        for future in future_to_api:
            final_valid_results.extend(future.result())

    if final_valid_results:
        final_valid_results.reverse()
        msg = f"#{cat_name}\n\n" + "\n\n".join([f"📌 <a href='{n['link']}'>→</a> <i>{n['content']}</i>" for n in final_valid_results])

        if send_tg(msg):
            log(f"📤 Сводка {cat_name} отправлена. Помечаю прочитанным...")
            for n in final_valid_results: mark_as_read(api_base, headers, n['id'])
        else:
            log(f"❌ Ошибка отправки в Telegram для {cat_name}.")

def send_tg(text, preview=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": not preview}
    try: return requests.post(url, data=payload, timeout=20).status_code == 200
    except: return False

def mark_as_read(api_base, headers, item_id):
    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item_id, 'a': 'user/-/state/com.google/read'})

def main():
    log("🏁 СТАРТ")
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth: return log("❌ Ошибка входа в RSS")

    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
    log("✅ ФИНИШ")

if __name__ == "__main__": main()
