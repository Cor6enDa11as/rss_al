#!/usr/bin/env python3
import requests
import hashlib
import json
import time
import os
import sys
from bs4 import BeautifulSoup

# Печатаем сразу, чтобы видеть в логах
print("--- ИНИЦИАЛИЗАЦИЯ СКРИПТА ---")

# Проверка секретов
BASE_URL = os.getenv("FRESHRSS_URL")
USER = os.getenv("FRESHRSS_USER")
FEVER_PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

print(f"URL сервера: {BASE_URL}")
print(f"Пользователь: {USER}")

CATEGORIES = ["Научпоп", "Компьютерное железо"]

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            s.decompose()
        article = soup.find('div', {'class': 'tm-article-body'}) or soup.find('article')
        text = article.get_text(separator=' ', strip=True) if article else ' '.join([p.get_text() for p in soup.find_all('p')])
        return text[:5000]
    except Exception as e:
        print(f" Ошибка парсинга {url}: {e}")
        return ""

def get_ai_summary(title, url):
    content = get_full_text(url)
    prompt = f"Суть новости одним предложением. Заголовок: {title}\nТекст: {content[:3000]}"

    models = ["deepseek/deepseek-chat:free", "mistralai/mistral-7b-instruct:free"]
    for model in models:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}),
                timeout=25
            )
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip()
        except: continue
    return f"Новость: {title}"

def main():
    if not all([BASE_URL, USER, FEVER_PASS]):
        print("❌ ОШИБКА: Не все секреты загружены! Проверьте Settings -> Secrets.")
        return

    # Fever Auth
    api_key = hashlib.md5(f"{USER}:{FEVER_PASS}".encode()).hexdigest()
    api_url = f"{BASE_URL}/api/fever.php?api"

    print(f"--- ЗАПУСК МОНИТОРИНГА (Категорий: {len(CATEGORIES)}) ---")

    try:
        # 1. Получаем список фидов
        print("Запрос списка фидов...")
        r_feeds = requests.post(api_url, data={'api_key': api_key, 'feeds': ''})
        feeds_info = {f['id']: f['title'] for f in r_feeds.json().get('feeds', [])}
        print(f"Получено фидов: {len(feeds_info)}")

        # 2. Получаем список групп
        print("Запрос групп...")
        r_groups = requests.post(api_url, data={'api_key': api_key, 'groups': ''})
        all_groups = r_groups.json().get('groups', [])

        # 3. Обработка по категориям
        for cat_name in CATEGORIES:
            print(f"\nПроверка категории: {cat_name}")
            group_id = next((g['id'] for g in all_groups if g['title'] == cat_name), None)

            if not group_id:
                print(f"⚠️ Категория '{cat_name}' не найдена в FreshRSS!")
                continue

            # Получаем фиды группы
            r_feeds_groups = requests.post(api_url, data={'api_key': api_key, 'feeds': ''})
            target_feeds = [rel['feed_id'] for rel in r_feeds_groups.json().get('feeds_groups', []) if str(group_id) in rel.get('group_ids', '').split(',')]

            # Получаем непрочитанные
            r_items = requests.post(api_url, data={'api_key': api_key, 'items': '', 'unread_item_ids': ''})
            unread_ids = set(r_items.json().get('unread_item_ids', '').split(','))

            items = [i for i in r_items.json().get('items', []) if str(i['id']) in unread_ids and i['feed_id'] in target_feeds][:10]

            print(f"Найдено новых статей: {len(items)}")

            if items:
                msg = f"<b>🤖 {cat_name.upper()}:</b>\n\n"
                for item in items:
                    print(f"Анализирую: {item['title'][:40]}...")
                    summary = get_ai_summary(item['title'], item['url'])
                    raw_source = feeds_info.get(item['feed_id'], "news")
                    clean_tag = "".join(filter(str.isalnum, raw_source.lower()))
                    msg += f"⚡️ {summary}, <a href='{item['url']}'>#{clean_tag}</a>\n\n"
                    # Марка прочитано
                    requests.post(api_url, data={'api_key': api_key, 'mark': 'item', 'as': 'read', 'id': item['id']})

                # Отправка
                res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                if res.status_code == 200:
                    print(f"✅ Дайджест {cat_name} отправлен!")
                else:
                    print(f"❌ Ошибка TG: {res.text}")

    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")

if __name__ == "__main__":
    main()
