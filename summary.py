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

# Расширенный список 6 бесплатных ИИ-агентов
AI_MODELS = [
    "google/gemini-flash-1.5-exp:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free",
    "openchat/openchat-7b:free",
    "phi3/phi-3-mini-128k-instruct:free",
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
    """Специальный парсер для Хабра и других сайтов"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')

        # Удаляем мусор
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']): s.decompose()

        # Пытаемся найти тело статьи (специально для Habr и стандартных тегов)
        article = (
            soup.find('div', {'class': 'tm-article-body'}) or
            soup.find('div', {'id': 'post-content-body'}) or
            soup.find('article') or
            soup.find('main')
        )

        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return text[:4000] # Берем побольше для ИИ
    except Exception as e:
        log(f"  ⚠️ Ошибка парсинга {url}: {e}")
        return ""

def get_ai_summary(title, url, seen_summaries):
    content = get_full_text(url)
    if not content: content = title

    # Промпт с защитой от дублей и обязательным русским языком
    prompt = (
        f"Ты — профессиональный редактор. Напиши суть новости ОДНИМ коротким предложением (до 15 слов) на РУССКОМ языке. "
        f"ВАЖНО: Если эта новость по смыслу дублирует одну из этих: {list(seen_summaries)[-5:]}, напиши 'ДУБЛИКАТ'. "
        f"Оригинал может быть на английском, но ответ всегда на русском. "
        f"Заголовок: {title}\nТекст: {content[:3500]}"
    )

    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com"
                },
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2
                }),
                timeout=25
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                if "ДУБЛИКАТ" in res.upper():
                    return None
                return res.rstrip('.')
        except: continue
    return title.rstrip('.')

def clean_hashtag(name):
    name = name.lower()
    # Удаляем Habr и прочие хвосты
    for junk in ['habr', 'хабр', 'новости', 'news', 'лента', 'feed', 'статьи']:
        name = name.replace(junk, '')
    clean = "".join(filter(str.isalnum, name))
    return f"#{clean}" if clean else "#news"

def main():
    log("=== ЗАПУСК: 6 AI-АГЕНТОВ + HABR FIX ===")
    token = get_auth_token()
    if not token: return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    global_seen_summaries = set()

    for cat, emoji in CATEGORIES.items():
        log(f"Категория: {cat}")
        tag_id = f"user/-/label/{cat}"
        try:
            r = requests.get(f"{api_base}/stream/contents/{tag_id}", params={'xt': 'user/-/state/com.google/read', 'n': 12}, headers=headers)
            items = r.json().get('items', [])

            if items:
                msg = f"<b>{emoji} {cat.upper()}:</b>\n\n"
                count = 0

                for item in items:
                    title = item.get('title', '')
                    link = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')

                    log(f"  Анализ: {title[:50]}...")
                    summary = get_ai_summary(title, link, global_seen_summaries)

                    if summary:
                        global_seen_summaries.add(summary)
                        tag = clean_hashtag(source)
                        msg += f"📌 {summary}, <a href='{link}'>{tag}</a>\n\n"
                        count += 1

                    # Помечаем как прочитанное в любом случае
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                if count > 0:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
            else:
                log("  Нет новых статей")
        except Exception as e:
            log(f"  Ошибка: {e}")

if __name__ == "__main__":
    main()
