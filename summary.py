import requests
import json
import os
import time
import re
from urllib.parse import urlparse, urlunparse
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

DB_FILE = "seen_urls.txt"
AI_MODELS = ["google/gemini-2.0-flash-001", "google/gemini-2.0-flash-lite-preview-02-05:free"]

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def normalize_url(url):
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except: return url

def make_hashtag(text):
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]', '', text)
    return f"#{clean}" if clean else ""

def get_smart_tag(item, url):
    """Разделение логики хэштегов для Telegram и Сайтов"""
    source_name = item.get('origin', {}).get('title', '')

    # Если это Telegram (проверка по названию источника)
    if "telegram channel" in source_name.lower():
        clean_text = re.sub(r'(?i)\s*[-]*\s*(telegram\s*channel)\s*', '', source_name).strip()
        return make_hashtag(clean_text)

    # Для обычных сайтов возвращаем короткий домен (как было идеально раньше)
    try:
        domain = urlparse(url).netloc.lower()
        tag = domain.replace('www.', '').split('.')[0].replace('-', '')
        return f"#{tag.capitalize()}"
    except:
        return "#News"

def check_is_video_strict(item, soup):
    """Строгая проверка на видео (без ложных срабатываний в науке)"""
    link = item.get('alternate', [{}])[0].get('href', '').lower()
    # 1. Проверка по домену
    if any(x in link for x in ["youtube.com", "youtu.be", "vimeo.com"]): return True
    # 2. Проверка по заголовку
    if "видео" in item.get('title', '').lower(): return True
    # 3. Проверка по наличию видео-плеера в коде
    if soup and (soup.find('video') or soup.find('iframe', src=re.compile(r'video|youtube|player'))):
        return True
    return False

def get_full_content(item, url):
    """Приоритетное извлечение текста (особенно для Telegram)"""
    # Сначала пытаемся спарсить саму страницу
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Ищем в стандартных блоках Telegram и новостей
        article = (soup.find('div', {'class': 'tgme_widget_message_text'}) or
                   soup.find('div', {'class': 'tm-article-body'}) or
                   soup.find('article'))
        if article:
            return " ".join(article.get_text(separator=' ', strip=True).split())[:3500], soup

        # Если на странице пусто, берем description из RSS (там лежит текст поста TG)
        rss_text = item.get('summary', {}).get('content', '') or item.get('content', {}).get('content', '')
        if rss_text:
            clean_rss = BeautifulSoup(rss_text, 'html.parser').get_text(separator=' ', strip=True)
            return clean_rss[:3500], soup

    except Exception as e:
        log(f"⚠️ Ошибка парсинга контента: {e}")

    return "", None

def get_ai_summary(text, is_video=False):
    if not text or len(text) < 100: return None

    prompt = (
        "Сформулируй суть новости ОДНИМ информативным предложением (до 30 слов) на русском. "
        "Опиши событие и результат. "
    )
    if is_video: prompt += "В конце предложения добавь (Видео)."

    for model in AI_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": f"{prompt}\n\n{text}"}],
                    "temperature": 0.1
                }),
                timeout=30
            )
            if r.status_code == 200:
                res = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                log(f"✅ Модель {model} успешно сработала.")
                return res
            else:
                log(f"❌ Ошибка API ({model}): {r.status_code} - {r.text}")
        except Exception as e:
            log(f"❗ Сбой связи с {model}: {e}")
    return None

def send_tg(text, preview_enabled=False, link=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    p_opts = {"is_disabled": not preview_enabled}
    if preview_enabled and link: p_opts.update({"url": link, "show_above_text": True})

    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "link_preview_options": json.dumps(p_opts)}
    requests.post(url, data=payload, timeout=10)

def process_category(cat_name, use_ai, headers, api_base, global_seen_urls):
    log(f"\n--- {cat_name.upper()} ---")
    try:
        r = requests.get(f"{api_base}/stream/contents/user/-/label/{cat_name}", params={'n': 40}, headers=headers)
        items = r.json().get('items', [])
        if not items: return

        msg_body_ai = f"{make_hashtag(cat_name)}\n\n"
        ai_count = 0

        for item in items:
            link = item.get('alternate', [{}])[0].get('href', '')
            if not link or link in global_seen_urls: continue

            # 1. Получаем текст и суп для проверки видео
            text_content, soup = get_full_content(item, link)
            is_video = check_is_video_strict(item, soup)
            tag = get_smart_tag(item, link)

            if use_ai:
                summary = get_ai_summary(text_content, is_video)
                if not summary: # Заглушка, если ИИ не выдал текст
                    summary = item.get('title', 'Новость')
                    if is_video: summary += " (Видео)"

                msg_body_ai += f"📌 <i>{summary}</i>\n🏷️ <a href='{link}'>{tag}</a>\n\n"
                ai_count += 1
                time.sleep(1)
            else:
                title = item.get('title', 'Новость')
                if is_video: title += " (Видео)"
                send_tg(f"📍 <b>{title}</b>\n🏷️ <a href='{link}'>{tag}</a>", True, link)
                log(f"✅ Direct: {tag}")

            global_seen_urls.add(link)
            requests.post(f"{api_base}/edit-tag", headers=headers, data={'i': item.get('id'), 'a': 'user/-/state/com.google/read'})

        if use_ai and ai_count > 0:
            send_tg(msg_body_ai, False)
            log(f"📊 Сводка отправлена. Использовано AI агентов: {ai_count}")

    except Exception as e: log(f"❌ Критическая ошибка в категории: {e}")

def main():
    login_url = f"{BASE_URL}/api/greader.php/accounts/ClientLogin?Email={USER}&Passwd={PASS}"
    r_auth = requests.get(login_url)
    auth_search = re.search(r'Auth=(.*)', r_auth.text)
    if not auth_search: return

    headers = {'Authorization': f'GoogleLogin auth={auth_search.group(1).strip()}'}
    api_base = f"{BASE_URL}/api/greader.php/reader/api/0"

    seen = set()
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f: seen = set(line.strip() for line in f)

    for cat in CATEGORIES_AI: process_category(cat, True, headers, api_base, seen)
    for cat in CATEGORIES_DIRECT: process_category(cat, False, headers, api_base, seen)

    with open(DB_FILE, "w") as f:
        for item in list(seen)[-1000:]: f.write(f"{item}\n")

if __name__ == "__main__": main()
