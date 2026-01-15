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

# Просто добавь названия своих папок сюда, и скрипт сам найдет их ID и фиды внутри
CATEGORIES_TO_WATCH = ["Научпоп", "Технологии", "Компьютерное железо"]

def get_ai_summary(title, url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r_page = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r_page.text, 'html.parser')
        # Очистка текста для ИИ
        for s in soup(['script', 'style', 'nav', 'header']): s.decompose()
        text = ' '.join([p.get_text() for p in soup.find_all('p')])[:3000]

        prompt = f"Суть новости одним коротким предложением (до 15 слов). Сразу факт. Заголовок: {title}\nТекст: {text}"
        r_ai = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            data=json.dumps({"model": "google/gemini-flash-1.5-exp:free", "messages": [{"role": "user", "content": prompt}]}),
            timeout=25
        )
        return r_ai.json()['choices'][0]['message']['content'].strip().rstrip('.')
    except: return f"Новость: {title}"

def main():
    api_key = hashlib.md5(f"{USER}:{FEVER_PASS}".encode()).hexdigest()
    api_url = f"{BASE_URL}/api/fever.php?api"

    print("--- ЗАПУСК ОБНОВЛЕННОЙ АВТОМАТИКИ ---")

    # 1. Получаем ГРУППЫ и СВЯЗИ (отдельными вызовами для надежности)
    try:
        g_resp = requests.post(api_url, data={'api_key': api_key, 'groups': ''}).json()
        f_resp = requests.post(api_url, data={'api_key': api_key, 'feeds': ''}).json()
        i_resp = requests.post(api_url, data={'api_key': api_key, 'items': '', 'unread_item_ids': ''}).json()

        all_groups = g_resp.get('groups', [])
        feeds_groups = f_resp.get('feeds_groups', [])
        all_feeds = {f['id']: f['title'] for f in f_resp.get('feeds', [])}
        all_items = i_resp.get('items', [])
        unread_ids = set(i_resp.get('unread_item_ids', '').split(','))

        print(f"Доступно категорий в API: {[g['title'] for g in all_groups]}")

        for target_name in CATEGORIES_TO_WATCH:
            # Находим ID текущей папки
            group_id = next((g['id'] for g in all_groups if g['title'] == target_name), None)
            if not group_id:
                print(f"! Категория '{target_name}' не найдена во FreshRSS")
                continue

            # Находим все фиды в этой папке
            target_feed_ids = []
            for fg in feeds_groups:
                # В Fever API group_ids — это строка с ID через запятую
                if str(group_id) in str(fg.get('group_ids', '')).split(','):
                    target_feed_ids.append(fg['feed_id'])

            print(f"Категория '{target_name}' (ID: {group_id}) содержит {len(target_feed_ids)} источников")

            # Собираем новости для этой категории
            to_send = [i for i in all_items if i['feed_id'] in target_feed_ids and str(i['id']) in unread_ids][:10]

            if to_send:
                msg = f"<b>🤖 {target_name.upper()}:</b>\n\n"
                for item in to_send:
                    summary = get_ai_summary(item['title'], item['url'])
                    source_name = all_feeds.get(item['feed_id'], "news")
                    tag = "".join(filter(str.isalnum, source_name.lower()))
                    msg += f"⚡️ {summary}, <a href='{item['url']}'>#{tag}</a>\n\n"
                    # Маркируем как прочитанное
                    requests.post(api_url, data={'api_key': api_key, 'mark': 'item', 'as': 'read', 'id': item['id']})

                # Отправка в Telegram
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
                print(f"✅ Дайджест '{target_name}' отправлен ({len(to_send)} новостей)")
            else:
                print(f"Новых статей в '{target_name}' нет.")

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")

if __name__ == "__main__":
    main()
