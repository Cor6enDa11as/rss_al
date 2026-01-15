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

# Список моделей, актуальных на 2026 год
AI_MODELS = [
    "google/gemini-2.0-flash-001",
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-chat",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-small-24b-instruct-2501"
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
        # Добавляем более реалистичные заголовки, чтобы сайты не блочили парсинг
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            log(f"  ⚠️ Сайт вернул код {r.status_code}")
            return ""

        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'button', 'form']): s.decompose()

        article = (
            soup.find('div', {'class': 'tm-article-body'}) or # Habr
            soup.find('div', {'class': 'article-content'}) or
            soup.find('article') or soup.find('main')
        )
        text = article.get_text(separator=' ', strip=True) if article else soup.get_text(separator=' ', strip=True)
        return " ".join(text.split())[:5000]
    except Exception as e:
        log(f"  ⚠️ Ошибка загрузки текста: {e}")
        return ""

def get_ai_summary(url, seen_summaries):
    content = get_full_text(url)
    if len(content) < 200:
        log("  ⚠️ Текст слишком короткий для анализа.")
        return None

    prompt = (
        "Ты — профессиональный технический редактор. Твоя задача: прочитать текст ниже и написать ОДНО предложение (до 15 слов) на РУССКОМ языке, "
        "которое выражает главную суть новости. Игнорируй заголовок, используй только информацию из текста. "
        "Если новость дублирует эти темы, ответь только словом ДУБЛИКАТ: " + ", ".join(list(seen_summaries)[-5:]) + ". "
        f"\n\nТЕКСТ СТАТЬИ ДЛЯ АНАЛИЗА:\n{content[:4000]}"
    )

    for model in AI_MODELS:
        try:
            log(f"    🤖 Запрос к модели: {model}...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "X-Title": "News Summary Bot"
                },
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1
                }),
                timeout=30
            )

            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip()
                if "ДУБЛИКАТ" in res.upper(): return "SKIP"
                log("    ✅ Успешно получен краткий пересказ.")
                return res.rstrip('.')
            else:
                log(f"    ❌ Ошибка API ({r.status_code}): {r.text[:200]}")
        except Exception as e:
            log(f"    ⚠️ Ошибка запроса к {model}: {e}")
            continue
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
    log("=== ЗАПУСК: ОТЛАДКА ИИ-АГЕНТОВ ===")
    token = get_auth_token()
    if not token:
        log("❌ Ошибка авторизации во FreshRSS. Проверьте логин/пароль.")
        return

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    global_seen_summaries = set()

    for cat, emoji in CATEGORIES.items():
        log(f"\n📁 Категория: {cat}")
        try:
            # Запрашиваем содержимое папки
            r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat}",
                             params={'xt': 'user/-/state/com.google/read', 'n': 10}, headers=headers)
            items = r.json().get('items', [])

            if items:
                msg = f"<b>{emoji} {cat.upper()}:</b>\n\n"
                count = 0
                for item in items:
                    link = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')
                    log(f"👉 Обработка: {item.get('title', '')[:60]}...")

                    summary = get_ai_summary(link, global_seen_summaries)

                    if summary == "SKIP":
                        log("    🚫 Дубликат по смыслу. Пропускаем.")
                    elif summary:
                        global_seen_summaries.add(summary)
                        tag = clean_hashtag(source)
                        # Формат: 📌 Суть 🔗 #тег
                        msg += f"📌 {summary} 🔗 <a href='{link}'>{tag}</a>\n\n"
                        count += 1
                    else:
                        log("    ⚠️ Не удалось получить суммаризацию ни от одной модели.")

                    # Всегда помечаем как прочитанное
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                if count > 0:
                    tg_r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                    if tg_r.status_code == 200:
                        log(f"✅ Дайджест {cat} отправлен в Telegram.")
                    else:
                        log(f"❌ Ошибка Telegram: {tg_r.text}")
            else:
                log("  Нет новых статей.")
        except Exception as e:
            log(f"  Критическая ошибка в категории {cat}: {e}")

if __name__ == "__main__":
    main()
