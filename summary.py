#!/usr/bin/env python3
import requests
import json
import os
import time
import sys
from bs4 import BeautifulSoup

# --- НАСТРОЙКИ (Берутся из GitHub Secrets) ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")  # API пароль
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Категории для мониторинга
CATEGORIES = ["Научпоп", "Технологии", "Компьютерное железо"]

# Список моделей для ротации (если одна не ответит, пробуем следующую)
AI_MODELS = [
    "google/gemini-flash-1.5-exp:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free",
    "openchat/openchat-7b:free"
]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def get_auth_token():
    log("Попытка авторизации в Google Reader API...")
    url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin"
    try:
        r = requests.get(url, params={'Email': USER, 'Passwd': PASS}, timeout=10)
        if r.status_code == 200:
            for line in r.text.split('\n'):
                if line.startswith('Auth='):
                    log("✅ Авторизация успешна.")
                    return line.replace('Auth=', '').strip()
        log(f"❌ Ошибка авторизации: Код {r.status_code}")
    except Exception as e:
        log(f"❌ Ошибка соединения: {e}")
    return None

def get_ai_summary(title, url):
    log(f"  Парсинг текста: {url}")
    content = ""
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']): s.decompose()
        # Пробуем найти основной текст
        main_content = soup.find('article') or soup.find('main') or soup.find('div', {'class': 'content'})
        content = main_content.get_text(separator=' ', strip=True) if main_content else soup.get_text()
        content = content[:3500] # Ограничение для контекста ИИ
    except Exception as e:
        log(f"  ⚠️ Ошибка парсинга: {e}")
        content = title

    prompt = f"Напиши суть новости одним коротким предложением (до 15 слов). Только факты. Заголовок: {title}\nТекст: {content}"

    for model in AI_MODELS:
        try:
            log(f"    Запрос к ИИ ({model.split('/')[-1]})...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}),
                timeout=25
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                log("    ✅ Ответ получен.")
                return res
            log(f"    Статус {r.status_code}, пробую следующую модель...")
        except:
            continue
    return title

def main():
    log("=== ЗАПУСК ДАЙДЖЕСТА (G-READER API) ===")

    token = get_auth_token()
    if not token:
        log("Критическая ошибка: Нет токена. Завершение.")
        sys.exit(1)

    headers = {'Authorization': f'GoogleLogin auth={token}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    for cat in CATEGORIES:
        log(f"\n--- Обработка категории: {cat} ---")
        tag_id = f"user/-/label/{cat}"
        params = {'xt': 'user/-/state/com.google/read', 'n': 10} # Только непрочитанные

        try:
            r = requests.get(f"{api_base}/stream/contents/{tag_id}", params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                log(f"Ошибка API при чтении категории: {r.status_code}")
                continue

            items = r.json().get('items', [])
            log(f"Найдено новых статей: {len(items)}")

            if items:
                msg = f"<b>🤖 {cat.upper()}:</b>\n\n"
                for item in items:
                    title = item.get('title', 'Без заголовка')
                    link = item.get('alternate', [{}])[0].get('href', '')
                    source = item.get('origin', {}).get('title', 'news')

                    log(f"👉 Обработка: {title[:50]}...")
                    summary = get_ai_summary(title, link)

                    # Формируем хэштег-ссылку
                    clean_tag = "".join(filter(str.isalnum, source.lower()))
                    msg += f"⚡️ {summary}, <a href='{link}'>#{clean_tag}</a>\n\n"

                    # Пометка прочитанным
                    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

                # Отправка в Телеграм
                log(f"Отправка сообщения в Telegram...")
                tg_r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                if tg_r.status_code == 200:
                    log(f"✅ Категория {cat} успешно отправлена.")
                else:
                    log(f"❌ Ошибка Telegram: {tg_r.text}")
            else:
                log(f"Новых новостей в '{cat}' не обнаружено.")

        except Exception as e:
            log(f"❌ Ошибка в цикле категории: {e}")

    log("\n=== РАБОТА ЗАВЕРШЕНА ===")

if __name__ == "__main__":
    main()
