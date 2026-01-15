#!/usr/bin/env python3
import requests
import json
import os
import time
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CATEGORIES = {
    "Научпоп": "🔭",
    "Технологии": "💻",
    "Компьютерное железо": "⚙️"
}

# Актуальный список мощных бесплатных моделей на OpenRouter
AI_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "nvidia/llama-3.1-nemotron-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "google/learnlm-1.5-pro-experimental:free",
    "meta-llama/llama-3.1-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "microsoft/phi-3-medium-4k-instruct:free"
]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def get_auth_token():
    url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin"
    try:
        r = requests.get(url, params={'Email': USER, 'Passwd': PASS}, timeout=10)
        if r.status_code == 200:
            for line in r.text.split('\n'):
                if line.startswith('Auth='): return line.replace('Auth=', '').strip()
    except: return None

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Удаляем лишний шум
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'button', 'form']): s.decompose()

        # Специальные контейнеры для Хабра и большинства сайтов
        article = (
            soup.find('div', {'class': 'tm-article-body'}) or
            soup.find('div', {'class': 'article-verdict'}) or
            soup.find('article') or
            soup.find('main')
        )
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return text[:5000]
    except Exception as e:
        log(f"  ⚠️ Ошибка загрузки текста: {e}")
        return ""

def get_ai_summary(url, seen_summaries):
    content = get_full_text(url)
    if len(content) < 200: return None

    prompt = (
        "Ты — новостной редактор. Проигнорируй заголовок, изучи только текст статьи ниже. "
        "Напиши ОДНО предложение (до 15 слов) на РУССКОМ языке, отражающее ключевую суть или новый технический факт. "
        "Пиши сразу суть, без вводных слов. "
        f"Если эта новость совпадает по смыслу с этими: {list(seen_summaries)[-5:]}, ответь только словом 'ДУБЛИКАТ'. "
        f"Текст для анализа: {content[:4000]}"
    )

    for model in AI_MODELS:
        try:
            log(f"    🤖 Пробую {model.split('/')[-1]}...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com"
                },
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}),
                timeout=25
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                if "ДУБЛИКАТ" in res.upper(): return "SKIP"
                log("    ✅ Успешно!")
                return res.rstrip('.')
            log(f"    ❌ Ошибка {r.status_code}")
        except: continue
    return None

def clean_hashtag(name):
    name_low = name.lower()
    # Спецусловие для Хабра
    if 'habr' in name_low or 'хабр' in name_low: return "#habr"

    for junk in ['новости', 'news', 'лента', 'feed', 'статьи', 'блог', 'подряд']:
        name_low = name_low.replace(junk, '')
    clean = "".join(filter(str.isalnum, name_low))
    return f"#{clean}" if clean else "#новости"

def main():
    log("=== START: G-READER + MULTI-AI + HABR-TAG ===")
    token = get_auth_token()
    if not token:
        log("❌ Ошибка авторизации. Проверьте FreshRSS API пароль.")
        return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_summaries = set()

    for cat, emoji in CATEGORIES.items():
        log(f"Категория: {cat}")
        try:
            r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat}",
                             params={'xt': 'user/-/state/com.google/read', 'n': 10}, headers=headers)
            items = r.json().get('items', [])

            if items:
                msg = f"<b>{emoji} {cat.upper()}:</b>\n\n"
                count = 0
                for item in items:
                    link = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')
                    log(f"👉 Анализ: {item.get('title', '')[:50]}...")

                    summary = get_ai_summary(link, global_seen_summaries)

                    if summary == "SKIP":
                        log("    🚫 Дубликат по смыслу. Пропуск.")
                    elif summary:
                        global_seen_summaries.add(summary)
                        tag = clean_hashtag(source)
                        # Формат: 📌 Суть 🔗 #тег
                        msg += f"📌 {summary} 🔗 <a href='{link}'>{tag}</a>\n\n"
                        count += 1

                    # Помечаем прочитанным во FreshRSS
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                if count > 0:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                    log(f"✅ Дайджест {cat} отправлен.")
            else: log("  Нет новых статей.")
        except Exception as e: log(f"  Ошибка в категории {cat}: {e}")

if __name__ == "__main__":
    main()
