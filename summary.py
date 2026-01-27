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
    """Исправленные вызовы API + Жесткий промпт на 30 слов"""
    prompt = (
        "Инструкция: Опиши суть новости ОДНИМ коротким предложением (30 слов) на русском. "
        "Фокус: Субъект + Действие + Результат. "
        "ПРАВИЛО SKIP: Отвечай ТОЛЬКО словом SKIP и только если эта новость на 100% идентична тем, что ты уже видел. "
        "Если есть минимальные отличия — пиши сводку. "
        f"Текст: {text[:3500]}"
    )

    try:
        if api_name == "gemini" and KEYS["gemini"]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={KEYS['gemini']}"
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=25)
            data = r.json()
            if 'candidates' in data and data['candidates']:
                return data['candidates'][0]['content']['parts'][0]['text'], "💎"
            log(f"⚠️ Gemini пустой ответ (Safety/Quota) на {link_hint[:30]}")

        elif api_name == "groq" and KEYS["groq"]:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['groq']}"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]}, timeout=20)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'], "⚡"

        elif api_name == "mistral" and KEYS["mistral"]:
            r = requests.post("https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEYS['mistral']}"},
                json={"model": "mistral-small-latest", "messages": [{"role": "user", "content": prompt}]}, timeout=20)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'], "🌀"

        elif api_name == "cohere" and KEYS["cohere"]:
            r = requests.post("https://api.cohere.ai/v1/chat",
                headers={"Authorization": f"Bearer {KEYS['cohere']}"},
                json={"message": prompt, "model": "command-r-plus"}, timeout=25)
            data = r.json()
            text_resp = data.get('text') or (data.get('generations') and data['generations'][0].get('text'))
            if text_resp:
                return text_resp, "🟣"
            log(f"⚠️ Cohere ошибка структуры на {link_hint[:30]}")

    except Exception as e:
        log(f"❌ {api_name.upper()} Exception: {str(e)[:50]}")
    return None, ""

def process_batch(api_name, items):
    """Обработка группы новостей конкретным API"""
    results = []
    log(f"[{api_name.upper()}] Обработка пакета из {len(items)} шт.")

    for item in items:
        link = item.get('alternate', [{}])[0].get('href', '')
        try:
            r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
            soup = BeautifulSoup(r.text, 'html.parser')
            article = soup.find('div', {'class': ['tm-article-body', 'post__text', 'article-body', 'tgme_widget_message_text']}) or soup.find('article')
            text = article.get_text(separator=' ', strip=True) if article else soup.get_text(strip=True)
            text = " ".join(text.split())[:3500]

            summary, emoji = call_ai(api_name, text, link)

            if summary:
                if "SKIP" in summary.upper() and len(summary) < 10:
                    log(f"⏭️ {api_name.upper()} скип (100% дубль): {link[:30]}...")
                else:
                    is_video = any(x in link.lower() for x in ["youtube.com", "youtu.be"])
                    clean_res = summary.strip().replace('\n', ' ').rstrip('.')
                    results.append({"link": link, "content": f"{emoji} {clean_res}" + (" 🎬" if is_video else ""), "id": item.get('id')})
                    log(f"✅ {api_name.upper()} успех: {link[:30]}...")
            else:
                log(f"⚠️ {api_name.upper()} не выдал текст для {link[:30]}")
        except Exception as e:
            log(f"❌ Ошибка загрузки {link[:30]}: {e}")

    return results

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                    params={'n': 32, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    data = r.json()
    items = data.get('items', [])
    if not items: return log(f"☕ {cat_name} пуст.")

    if not use_ai:
        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if send_tg(f"📍 <b><a href='{link}'>{item.get('title')}</a></b>", True):
                mark_as_read(api_base, headers, item.get('id'))
        return

    # Динамическое деление на активные API
    active_apis = [name for name, key in KEYS.items() if key]
    if not active_apis: return log("❌ Нет ключей API!")

    n_api = len(active_apis)
    chunks = [items[i::n_api] for i in range(n_api)]

    log(f"📦 Распределение: " + ", ".join([f"{active_apis[i]}: {len(chunks[i])}" for i in range(n_api)]))

    final_results = []
    with ThreadPoolExecutor(max_workers=n_api) as executor:
        futures = [executor.submit(process_batch, active_apis[i], chunks[i]) for i in range(n_api) if chunks[i]]
        for f in futures:
            final_results.extend(f.result())

    if final_results:
        final_results.reverse()
        msg = f"#{cat_name}\n\n" + "\n\n".join([f"📌 <a href='{n['link']}'>→</a> <i>{n['content']}</i>" for n in final_results])

        if send_tg(msg):
            log(f"📤 Сводка {cat_name} отправлена.")
            for n in final_results: mark_as_read(api_base, headers, n['id'])
        else:
            log(f"❌ Ошибка отправки в TG для {cat_name}.")

def send_tg(text, preview=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": not preview}
    try:
        res = requests.post(url, data=payload, timeout=20)
        return res.status_code == 200
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
