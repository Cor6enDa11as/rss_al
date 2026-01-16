#!/usr/bin/env python3
import requests
import json
import os
import time
from urllib.parse import urlparse
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
    "Компьютерное железо": "⚙️",
    "Linux": "⚙️",
}

AI_MODELS = [
    "google/gemini-2.0-flash-001",
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-chat"
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
        if r.status_code != 200: return ""

        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'button', 'form']): s.decompose()

        article = (
            soup.find('div', {'class': 'tm-article-body'}) or
            soup.find('div', {'class': 'article-content'}) or
            soup.find('article') or soup.find('main')
        )
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return " ".join(text.split())[:5000]
    except: return ""

def get_ai_summary(url, seen_summaries):
    content = get_full_text(url)
    if len(content) < 150: return None

    prompt = (
        "Ты — профессиональный технический редактор. Напиши суть новости ОДНИМ развернутым предложением (до 30 слов) "
        "на РУССКОМ языке. Отрази ключевой факт. Игнорируй заголовок. "
        "Если новость дублирует темы: " + ", ".join(list(seen_summaries)[-5:]) + ", ответь только словом ДУБЛИКАТ. "
        f"\n\nТЕКСТ:\n{content[:4000]}"
    )

    for model in AI_MODELS:
        try:
            log(f"    🤖 Запрос к {model.split('/')[-1]}...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}),
                timeout=30
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                if "ДУБЛИКАТ" in res.upper(): return "SKIP"
                return res.rstrip('.')
        except: continue
    return None

def extract_hashtag(url):
    """Вырезает имя домена из ссылки для хэштега"""
    try:
        domain = urlparse(url).netloc.lower()
        # Убираем www. и поддомены (оставляем только основное имя)
        parts = domain.replace('www.', '').split('.')
        # Для ixbt.com вернет ixbt, для naked-science.ru вернет nakedscience
        tag = parts[0].replace('-', '')
        return f"#{tag}"
    except:
        return "#news"

def main():
    log("=== ЗАПУСК: SMART URL TAGS ===")
    token = get_auth_token()
    if not token: return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_summaries = set()

    for cat, emoji in CATEGORIES.items():
        log(f"\n📂 Категория: {cat}")
        try:
            r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat}",
                             params={'xt': 'user/-/state/com.google/read', 'n': 10}, headers=headers)
            items = r.json().get('items', [])

            if items:
                msg = f"<b>{emoji} {cat.upper()}:</b>\n\n"
                count = 0
                for item in items:
                    title = item.get('title', '...')
                    link = item.get('alternate', [{}])[0].get('href', '')

                    log(f"👉 Обработка: {title[:50]}...")
                    summary = get_ai_summary(link, global_seen_summaries)

                    if summary == "SKIP":
                        log("    🚫 Дубликат. Пропуск.")
                    else:
                        if not summary:
                            summary = title # Фалбэк на заголовок

                        global_seen_summaries.add(summary)
                        tag = extract_hashtag(link)
                        # Формат: 📌 Текст 🔗 #тег
                        msg += f"📌 {summary} 🔗 <a href='{link}'>{tag}</a>\n\n"
                        count += 1

                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                if count > 0:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
            else: log("  Новых новостей нет.")
        except Exception as e: log(f"  Ошибка: {e}")

if __name__ == "__main__":
    main()
