#!/usr/bin/env python3
import requests
import hashlib
import json
import time
import os
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ИЗ СЕКРЕТОВ ---
BASE_URL = os.getenv("FRESHRSS_URL")
USER = os.getenv("FRESHRSS_USER")
FEVER_PASS = os.getenv("FRESHRSS_PASS")

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Категории можно оставить здесь или тоже вынести в секреты
CATEGORIES = ["Научпоп"]

def get_full_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'button']):
            s.decompose()
        article = soup.find('div', {'class': 'tm-article-body'}) or soup.find('article') or soup.find('main')
        text = article.get_text(separator=' ', strip=True) if article else ' '.join([p.get_text() for p in soup.find_all('p')])
        return text[:6000]
    except: return ""

def get_ai_summary(title, url):
    content = get_full_text(url)
    prompt = f"Напиши суть новости одним предложением (до 15 слов). Сразу факт. Заголовок: {title}\nТекст: {content[:4000]}"

    models = ["deepseek/deepseek-chat:free", "mistralai/mistral-7b-instruct:free"]
    for model in models:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://freshrss.org"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}),
                timeout=25
            )
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip().rstrip('.')
        except: continue
    return f"Новость: {title}"

def process_category(api_url, api_key, category_name, feeds_info):
    r_groups = requests.post(api_url, data={'api_key': api_key, 'groups': ''})
    group_id = next((g['id'] for g in r_groups.json().get('groups', []) if g['title'] == category_name), None)
    if not group_id: return

    r_feeds = requests.post(api_url, data={'api_key': api_key, 'feeds': ''})
    target_feeds = [rel['feed_id'] for rel in r_feeds.json().get('feeds_groups', []) if str(group_id) in rel.get('group_ids', '').split(',')]

    r_items = requests.post(api_url, data={'api_key': api_key, 'items': '', 'unread_item_ids': ''})
    unread_ids = set(r_items.json().get('unread_item_ids', '').split(','))
    items = [i for i in r_items.json().get('items', []) if str(i['id']) in unread_ids and i['feed_id'] in target_feeds][:12]

    if not items: return

    msg = f"<b>🤖 {category_name.upper()}:</b>\n\n"
    for item in items:
        summary = get_ai_summary(item['title'], item['url'])
        raw_source = feeds_info.get(item['feed_id'], "news")
        clean_tag = "".join(filter(str.isalnum, raw_source.lower()))
        msg += f"⚡️ {summary}, <a href='{item['url']}'>#{clean_tag}</a>\n\n"
        requests.post(api_url, data={'api_key': api_key, 'mark': 'item', 'as': 'read', 'id': item['id']})

    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})

def main():
    if not all([BASE_URL, USER, FEVER_PASS]):
        print("Ошибка: Не настроены секреты FreshRSS")
        return

    api_key = hashlib.md5(f"{USER}:{FEVER_PASS}".encode()).hexdigest()
    api_url = f"{BASE_URL}/api/fever.php?api"

    r_feeds = requests.post(api_url, data={'api_key': api_key, 'feeds': ''})
    feeds_info = {f['id']: f['title'] for f in r_feeds.json().get('feeds', [])}

    for cat in CATEGORIES:
        process_category(api_url, api_key, cat, feeds_info)
        time.sleep(2)

if __name__ == "__main__":
    main()
