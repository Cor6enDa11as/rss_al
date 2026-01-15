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

AI_MODELS = [
    "google/gemini-flash-1.5-exp:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free",
    "openchat/openchat-7b:free",
    "meta-llama/llama-3-8b-instruct:free"
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
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'button']): s.decompose()

        # Улучшенный поиск контента ( Habr tm-article-body + общие)
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
    if len(content) < 150:
        log("  ⚠️ Текст слишком короткий, ИИ может ошибиться.")

    prompt = (
        f"Изучи текст статьи и напиши ОДНО предложение (до 15 слов) на РУССКОМ языке, "
        f"отражающее главную суть или ключевой технический факт. "
        f"Если новость дублирует смысл этих тем: {list(seen_summaries)[-5:]}, ответь только словом 'ДУБЛИКАТ'. "
        f"Текст для анализа: {content[:4000]}"
    )

    for model in AI_MODELS:
        try:
            log(f"    🤖 Запрос к {model.split('/')[-1]}...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}),
                timeout=25
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                if "ДУБЛИКАТ" in res.upper():
                    return "SKIP_DUPLICATE"
                return res.rstrip('.')
            log(f"    ❌ Модель вернула код {r.status_code}")
        except: continue
    return None

def clean_hashtag(name):
    name_low = name.lower()
    # Спецусловие для Хабра
    if 'habr' in name_low or 'хабр' in name_low:
        return "#habr"

    # Очистка для остальных
    for junk in ['новости', 'news', 'лента', 'feed', 'статьи', 'блог']:
        name_low = name_low.replace(junk, '')
    clean = "".join(filter(str.isalnum, name_low))
    return f"#{clean}" if clean else "#news"

def main():
    log("=== START: G-READER + HABR FIX + AI LOGGING ===")
    token = get_auth_token()
    if not token:
        log("❌ Ошибка Auth")
        return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_summaries = set()

    for cat, emoji in CATEGORIES.items():
        log(f"Категория: {cat}")
        tag_id = f"user/-/label/{cat}"
        try:
            r = requests.get(f"{api_base}/stream/contents/{tag_id}", params={'xt': 'user/-/state/com.google/read', 'n': 10}, headers=headers)
            items = r.json().get('items', [])

            if items:
                msg = f"<b>{emoji} {cat.upper()}:</b>\n\n"
                count = 0
                for item in items:
                    link = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')
                    title = item.get('title', '...')

                    log(f"👉 Обработка: {title[:50]}...")
                    summary = get_ai_summary(link, global_seen_summaries)

                    if summary == "SKIP_DUPLICATE":
                        log("    🚫 Дубликат. Пропускаем.")
                    elif summary:
                        global_seen_summaries.add(summary)
                        tag = clean_hashtag(source)
                        # Форматирование: 📌 Суть 🔗 #тег
                        msg += f"📌 {summary} 🔗 <a href='{link}'>{tag}</a>\n\n"
                        count += 1

                    # Помечаем прочитанным во FreshRSS
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                if count > 0:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                    log(f"✅ Сообщение для {cat} отправлено.")
            else:
                log("  Новых статей нет.")
        except Exception as e:
            log(f"  Критическая ошибка: {e}")

if __name__ == "__main__":
    main()
