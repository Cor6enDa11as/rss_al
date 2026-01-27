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

KEYS = {
    "gemini": os.getenv("GEMINI_API_KEY"),
    "groq": os.getenv("GROQ_API_KEY"),
    "mistral": os.getenv("MISTRAL_API_KEY"),
    "cohere": os.getenv("COHERE_API_KEY")
}

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def call_ai(api_name, text, link_hint):
    prompt = (
        "Инструкция: Опиши суть новости ОДНИМ коротким предложением (до 30 слов) на русском. "
        "Фокус: Субъект + Действие + Результат. "
        "ПРАВИЛО SKIP: Отвечай ТОЛЬКО словом SKIP и только если новость на 100% идентична тем, что ты уже видел. "
        f"Текст: {text[:3500]}"
    )

    try:
        if api_name == "gemini" and KEYS["gemini"]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={KEYS['gemini']}"
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=25)
            data = r.json()
            if 'candidates' in data and data['candidates'] and 'content' in data['candidates'][0]:
                return data['candidates'][0]['content']['parts'][0]['text'], "💎"
            log(f"⚠️ Gemini блок (Safety/Quota) на {link_hint[:30]}")

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
            # Ультра-парсер для Cohere
            text_resp = data.get('text') or (data.get('generations') and data['generations'][0].get('text')) or data.get('response', {}).get('text')
            if text_resp:
                return text_resp, "🟣"
            log(f"⚠️ Cohere формат не распознан. JSON: {str(data)[:100]}")

    except Exception as e:
        log(f"❌ {api_name.upper()} Exception: {str(e)[:50]}")
    return None, ""

def process_item(args):
    item, api_name = args
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
                log(f"⏭️ {api_name.upper()} скип: {link[:30]}...")
                return {"id": item.get('id'), "skip": True}

            is_video = any(x in link.lower() for x in ["youtube.com", "youtu.be"])
            return {"link": link, "content": f"{emoji} {summary.strip().rstrip('.')}" + (" 🎬" if is_video else ""), "id": item.get('id'), "skip": False}
    except: pass
    return {"id": item.get('id'), "failed": True, "item": item} # Возвращаем item для повтора

def process_category(cat_name, use_ai, headers, api_base):
    log(f"🚀 КАТЕГОРИЯ: {cat_name.upper()}")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}",
                    params={'n': 60, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items: return log(f"☕ {cat_name} пуст.")

    if not use_ai:
        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if send_tg(f"📍 <b><a href='{link}'>{item.get('title')}</a></b>", True):
                mark_as_read(api_base, headers, item.get('id'))
        return

    active_apis = [name for name, key in KEYS.items() if key]
    n_api = len(active_apis)
    chunks = [items[i::n_api] for i in range(n_api)]

    initial_tasks = []
    for i, api in enumerate(active_apis):
        for item in chunks[i]:
            initial_tasks.append((item, api))

    final_results = []
    failed_items = []
    processed_ids = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process_item, initial_tasks))
        for res in results:
            if res:
                if res.get('failed'):
                    failed_items.append(res['item'])
                elif res.get('skip'):
                    processed_ids.append(res['id'])
                else:
                    final_results.append(res)
                    processed_ids.append(res['id'])

    # --- FAILOVER: Дожимаем упавшие новости через Groq ---
    if failed_items and KEYS.get('groq'):
        log(f"🔄 Резерв: Передаю {len(failed_items)} упавших новостей в GROQ...")
        with ThreadPoolExecutor(max_workers=4) as executor:
            retry_results = list(executor.map(process_item, [(item, 'groq') for item in failed_items]))
            for res in retry_results:
                if res and not res.get('failed') and not res.get('skip'):
                    final_results.append(res)
                    processed_ids.append(res['id'])

    if final_results:
        final_results.reverse()
        msg = f"#{cat_name}\n\n" + "\n\n".join([f"📌 <a href='{n['link']}'>→</a> <i>{n['content']}</i>" for n in final_results])
        if send_tg(msg):
            log(f"📤 Сводка {cat_name} отправлена.")
            for i in processed_ids: mark_as_read(api_base, headers, i)
        else:
            log(f"❌ Ошибка TG для {cat_name}.")

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
    if not auth: return log("❌ Ошибка входа")
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)
    log("✅ ФИНИШ")

if __name__ == "__main__": main()
