#!/usr/bin/env python3
import requests
import hashlib
import json
import time
import os
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL")
USER = os.getenv("FRESHRSS_USER")
FEVER_PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Категории как в вашем FreshRSS
CATEGORIES = ["Научпоп", "Технологии", "Компьютерное железо"]

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            s.decompose()
        article = soup.find('div', {'class': 'tm-article-body'}) or soup.find('article')
        text = article.get_text(separator=' ', strip=True) if article else ' '.join([p.get_text() for p in soup.find_all('p')])
        return text[:4000]
    except: return ""

def get_ai_summary(title, url):
    content = get_full_text(url)
    prompt = f"Суть новости одним коротким предложением (до 15 слов). Сразу факт. Заголовок: {title}\nТекст: {content[:3000]}"
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            data=json.dumps({"model": "google/gemini-flash-1.5-exp:free", "messages": [{"role": "user", "content": prompt}]}),
            timeout=25
        )
        return r.json()['choices'][0]['message']['content'].strip().rstrip('.')
    except: return f"Новость: {title}"

def main():
    api_key = hashlib.md5(f"{USER}:{FEVER_PASS}".encode()).hexdigest()
    api_url = f"{BASE_URL}/api/fever.php?api"

    print("--- ЗАПУСК ДИАГНОСТИКИ ---")

    try:
        # 1. Загружаем все данные за один запрос для экономии лимитов
        r = requests.post(api_url, data={'api_key': api_key, 'groups': '', 'feeds': '', 'items': '', 'unread_item_ids': ''})
        data = r.json()

        all_groups = data.get('groups', [])
        all_feeds = {f['id']: f['title'] for f in data.get('feeds', [])}
        feeds_groups = data.get('feeds_groups', [])
        all_items = data.get('items', [])
        unread_ids = set(data.get('unread_item_ids', '').split(','))

        print(f"Всего статей в API: {len(all_items)}")
        print(f"Непрочитанных (ID): {len(unread_ids)}")

        for cat_name in CATEGORIES:
            print(f"\nОбработка: {cat_name}")

            # Находим ID группы
            group_id = next((g['id'] for g in all_groups if g['title'] == cat_name), None)
            if not group_id:
                print(f"⚠️ Категория '{cat_name}' не найдена!")
                continue

            # Находим все фиды, принадлежащие этой группе
            target_feed_ids = []
            for fg in feeds_groups:
                if str(group_id) in str(fg.get('group_ids', '')).split(','):
                    target_feed_ids.append(fg['feed_id'])

            print(f"Фидов в категории: {len(target_feed_ids)}")

            # Фильтруем статьи: должны быть в нужных фидах И быть в списке unread_ids
            # Если unread_ids пустой (баг API), временно берем просто последние новости
            if not unread_ids or len(unread_ids) < 1:
                print("⚠️ Список непрочитанных пуст в API, беру последние доступные...")
                items = [i for i in all_items if i['feed_id'] in target_feed_ids][:5]
            else:
                items = [i for i in all_items if i['feed_id'] in target_feed_ids and str(i['id']) in unread_ids][:10]

            print(f"Итого статей к отправке: {len(items)}")

            if items:
                msg = f"<b>🤖 {cat_name.upper()}:</b>\n\n"
                for item in items:
                    print(f" - {item['title'][:50]}")
                    summary = get_ai_summary(item['title'], item['url'])
                    source = all_feeds.get(item['feed_id'], "news")
                    tag = "".join(filter(str.isalnum, source.lower()))
                    msg += f"⚡️ {summary}, <a href='{item['url']}'>#{tag}</a>\n\n"
                    # Помечаем прочитанным
                    requests.post(api_url, data={'api_key': api_key, 'mark': 'item', 'as': 'read', 'id': item['id']})

                # Отправка в TG
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})

    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    main()
