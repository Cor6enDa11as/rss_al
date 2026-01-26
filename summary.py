
import requests
import json
import os
import time
import re
import random
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL", "").rstrip('/')
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CATEGORIES_AI = [c.strip() for c in os.getenv("CATEGORIES_AI", "").split(",") if c.strip()]
CATEGORIES_DIRECT = [c.strip() for c in os.getenv("CATEGORIES_DIRECT", "").split(",") if c.strip()]

# Только 100% бесплатные и проверенные ID на текущий момент
AI_MODELS = [
    "google/gemini-2.0-flash-lite-preview-02-05:free",
    "google/gemini-2.0-pro-experimental-02-05:free",
    "google/gemini-2.0-flash-lite-001",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1:free"
]

processed_summaries = []

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def make_hashtag(text):
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]', '', text)
    return f"#{clean}" if clean else ""

def get_smart_tag(item, url):
    source_name = item.get('origin', {}).get('title', '')
    if "telegram channel" in source_name.lower():
        clean_text = re.sub(r'(?i)\s*[-]*\s*(telegram\s*channel)\s*', '', source_name).strip()
        return make_hashtag(clean_text)
    try:
        tag = urlparse(url).netloc.lower().replace('www.', '').split('.')[0].replace('-', '')
        return f"#{tag.capitalize()}"
    except: return "#News"

def check_video(item, soup):
    link = item.get('alternate', [{}])[0].get('href', '').lower()
    if any(x in link for x in ["youtube.com", "youtu.be", "vimeo.com"]): return True
    encs = item.get('enclosure', [])
    if any('video' in e.get('type', '') for e in encs): return True
    if soup and (soup.find('video') or soup.find('div', class_='tgme_widget_message_video_player')): return True
    return False

def get_content(item, url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        article = soup.find('div', {'class': 'tgme_widget_message_text'}) or soup.find('article')
        if article: return " ".join(article.get_text(separator=' ', strip=True).split())[:3500], soup
        content = item.get('summary', {}).get('content', '') or item.get('content', {}).get('content', '')
        return BeautifulSoup(content, 'html.parser').get_text(separator=' ', strip=True)[:3500], soup
    except: return "", None

def get_ai_summary(text, is_video):
    if not text or len(text) < 100: return None
    context = "\n".join(processed_summaries[-8:]) # Сократили контекст, чтобы не путать ИИ
    
    # Промт с ослабленным фильтром дублей
    prompt = (
        f"Суть новости ОДНИМ предложением ( 30 слов) на русском. Опиши конкретное событие и результат.\n"
        f"ВАЖНО: Если текст В ТОЧНОСТИ ПОВТОРЯЕТ смысл этих строк, ответь только словом SKIP. "
        f"Но если это продолжение темы или другое событие из той же серии — ОБЯЗАТЕЛЬНО пиши сводку.\n"
        f"Контекст прошлых новостей:\n{context}\n\nТекст новости: {text}"
    )
    
    models_to_try = AI_MODELS.copy()
    random.shuffle(models_to_try)

    for model in models_to_try:
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}), timeout=30)
            
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                if "SKIP" in res.upper() and len(res) < 10: return "SKIP"
                final = f"{res} 🎬" if is_video and "🎬" not in res else res
                processed_summaries.append(final)
                log(f"✅ {model.split('/')[-1]} success")
                return final
            else:
                log(f"⚠️ {model.split('/')[-1]} ошибка: {r.status_code}")
        except Exception as e:
            log(f"❌ {model.split('/')[-1]} сбой: {str(e)}")
            continue
    return None

def send_tg(text, preview=False, link=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    opts = {"is_disabled": not preview}
    if preview and link: opts.update({"url": link, "prefer_large_media": True, "show_above_text": True})
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps(opts)}
    try:
        res = requests.post(url, data=payload, timeout=20)
        return res.status_code == 200
    except: return False

def mark_as_read(api_base, headers, item_id):
    requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item_id, 'a': 'user/-/state/com.google/read'})

def process_single_item(item, use_ai):
    link = item.get('alternate', [{}])[0].get('href', '')
    if not link: return None
    
    text, soup = get_content(item, link)
    is_video = check_video(item, soup)
    tag = get_smart_tag(item, link)
    is_yt = any(x in link.lower() for x in ["youtube.com", "youtu.be"])

    if use_ai:
        summary = get_ai_summary(text, is_video)
        return {"id": item.get('id'), "type": "ai", "content": summary, "link": link, "tag": tag} if summary else None
    else:
        title = f"{item.get('title', 'News')} 🎬" if is_video and not is_yt else item.get('title', 'News')
        return {"id": item.get('id'), "type": "direct", "title": title, "link": link, "tag": tag, "is_yt": is_yt}

def process_category(cat_name, use_ai, headers, api_base):
    log(f"--- {cat_name.upper()} ---")
    r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", params={'n': 80, 'xt': 'user/-/state/com.google/read'}, headers=headers)
    items = r.json().get('items', [])
    if not items: return

    results = []
    # Снизили до 2 потоков, чтобы не ловить 429
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(process_single_item, it, use_ai) for it in items]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res: results.append(res)

    if use_ai:
        valid_news = []
        for r in results:
            if r['content'] == "SKIP":
                log(f"⏭️ SKIP: Дубликат {r['link'][:40]}...")
            else:
                valid_news.append(r)
        
        if valid_news:
            # Сортируем по ID, чтобы сохранить примерный порядок из ленты
            valid_news.reverse()
            msg = f"{make_hashtag(cat_name)}\n\n" + "\n\n".join([f"📌 <a href='{n['link']}'>→</a> <i>{n['content']}</i>\n🏷️ {n['tag']}" for n in valid_news])
            if send_tg(msg):
                for n in results: mark_as_read(api_base, headers, n['id'])
                log(f"✅ Сводка {cat_name} отправлена")
    else:
        for n in results:
            msg = f"📍 <b><a href='{n['link']}'>{n['title']}</a></b>\n🏷️ {n['tag']}"
            if n['is_yt']: 
                log("⏳ YouTube пауза 15с...")
                time.sleep(15)
            if send_tg(msg, True, n['link']):
                mark_as_read(api_base, headers, n['id'])
                log(f"✅ Пост отправлен: {n['tag']}")

def main():
    auth_res = requests.get(f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}")
    auth = re.search(r'Auth=(.*)', auth_res.text)
    if not auth: return
    headers = {'Authorization': f'GoogleLogin auth={auth.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"
    
    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base)

if __name__ == "__main__": main()
