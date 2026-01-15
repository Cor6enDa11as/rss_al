#!/usr/bin/env python3
import os
import asyncio
import requests
import logging
from datetime import datetime
from telegram import Bot
import openai
import re
from bs4 import BeautifulSoup
import time

# --- КОНФИГУРАЦИЯ ---
BASE_URL = os.getenv("FRESHRSS_URL")
USER = os.getenv("FRESHRSS_USER")
PASS = os.getenv("FRESHRSS_PASS")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Категории для фильтрации (можно настраивать в .env)
CATEGORIES = os.getenv("NEWS_CATEGORIES", "научпоп,технологии").split(",")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Словарь соответствия ключевых слов категории
CATEGORY_KEYWORDS = {
    "научпоп": ["наука", "научпоп", "научный", "исследование", "ученые", "discovery", "science", "research"],
    "технологии": ["технологии", "tech", "gadget", "гаджеты", "программы", "software", "hardware", "hi-tech"],
    "политика": ["политика", "политик", "правительство", "election", "government"],
    "экономика": ["экономика", "финансы", "business", "рынок", "инвестиции"]
}

# Список бесплатных/доступных моделей
FREE_MODELS = [
    "microsoft/wizardlm-2-8x22b",
    "google/gemma-2-9b-it",
    "mistralai/mistral-7b-instruct",
    "meta-llama/llama-3.1-8b-instruct"
]

# Эмодзи для категорий
CATEGORY_EMOJIS = {
    "научпоп": "🔬",
    "технологии": "💻",
    "политика": "🏛️",    "экономика": "💼"
}

def get_unread_entries():
    """Получение непрочитанных статей через Fever API"""
    try:
        # Шаг 1: Получаем токен авторизации
        login_url = f"{BASE_URL}/api/fever.php"
        
        login_data = {
            'api': 'fever',
            'action': 'login',
            'email': USER,
            'password': PASS
        }
        
        login_response = requests.post(login_url, data=login_data)
        
        if login_response.status_code != 200:
            logger.error(f"Ошибка аутентификации через Fever API: {login_response.status_code}")
            return []
        
        login_json = login_response.json()
        token = login_json.get('token')
        
        if not token:
            logger.error("Не удалось получить токен авторизации")
            return []
        
        logger.info("Успешная аутентификация через Fever API")

        # Шаг 2: Получаем список непрочитанных статей
        items_url = f"{BASE_URL}/api/fever.php"
        items_params = {
            'api': 'fever',
            'action': 'items',
            'token': token,
            'unread': '1'  # Только непрочитанные
        }
        
        items_response = requests.get(items_url, params=items_params)
        
        if items_response.status_code != 200:
            logger.error(f"Ошибка получения статей: {items_response.status_code}")
            return []
        
        items_json = items_response.json()
        entries = items_json.get('items', [])
        
        logger.info(f"Получено {len(entries)} непрочитанных статей через Fever API")        return entries
        
    except Exception as e:
        logger.error(f"Ошибка при получении статей через Fever API: {e}")
        return []

def matches_category(entry, categories=CATEGORIES):
    """Проверка соответствия статьи указанным категориям"""
    title = entry.get('title', '').lower()
    content = entry.get('content', '').lower()
    feed_title = entry.get('feed', {}).get('title', '').lower()
    
    combined_text = f"{title} {content} {feed_title}"
    
    for category in categories:
        category_lower = category.strip().lower()
        if category_lower in CATEGORY_KEYWORDS:
            keywords = CATEGORY_KEYWORDS[category_lower]
            if any(keyword.lower() in combined_text for keyword in keywords):
                return True, category_lower
        else:
            if category_lower in combined_text:
                return True, category_lower
    
    return False, None

def extract_article_text(url):
    """Извлечение текста статьи с помощью BeautifulSoup"""
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Удаление script и style элементов
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        
        return text[:2000]  # Ограничение длины текста
    except Exception as e:
        logger.error(f"Ошибка извлечения текста из {url}: {e}")
        return ""

def summarize_with_openrouter(text, model_index=0):
    """Генерация краткой сводки с помощью ИИ"""
    if model_index >= len(FREE_MODELS):
        logger.error("Все модели исчерпаны")        return None
        
    model = FREE_MODELS[model_index]
    
    try:
        client = openai.OpenAI(
            api_key=OPENROUTER_KEY,
            base_url="https://openrouter.ai/api/v1"
        )

        prompt = f"""Сделай краткое изложение текста одним предложением, описывающим суть новости. Не добавляй заголовок, только суть новости. Текст:\n\n{text}"""

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=100,
            temperature=0.3
        )
        
        summary = response.choices[0].message.content.strip()
        logger.info(f"Успешно обработана статья с помощью модели {model}")
        return summary
        
    except Exception as e:
        logger.warning(f"Ошибка с моделью {model}: {e}. Пробуем следующую...")
        time.sleep(1)  # Задержка перед следующей попыткой
        return summarize_with_openrouter(text, model_index + 1)

def clean_source_name(title):
    """Очистка названия источника для хэштега"""
    # Убираем спецсимволы, оставляем только буквы и цифры
    cleaned = re.sub(r'[^\w\s-]', '', title.lower())
    cleaned = re.sub(r'\s+', '_', cleaned.strip())
    return cleaned.replace('-', '_')

async def main():
    logger.info(f"Запуск обработки новостей по категориям: {CATEGORIES}")
    
    all_entries = get_unread_entries()
    logger.info(f"Получено {len(all_entries)} статей из FreshRSS")
    
    # Группируем статьи по категориям
    categorized_news = {}
    for entry in all_entries:
        match, category = matches_category(entry)
        if match:
            if category not in categorized_news:
                categorized_news[category] = []            categorized_news[category].append(entry)
    
    logger.info(f"Найдено {len(categorized_news)} категорий с новостями")
    
    # Формируем сводку по категориям
    summary_messages = []
    
    for category, entries in categorized_news.items():
        if not entries:
            continue
            
        emoji = CATEGORY_EMOJIS.get(category, "📰")
        category_display = category.replace('_', ' ').capitalize()
        
        # Формируем заголовок категории
        category_summary = f"{emoji} <b>Новости {category_display}</b>\n\n"
        
        # Обрабатываем статьи в категории
        for entry in entries:
            article_url = entry.get('url')
            title = entry.get('title', '')
            content = entry.get('content', '')
            feed_title = entry.get('feed', {}).get('title', 'unknown')
            
            if not article_url:
                continue
                
            logger.info(f"Обработка статьи: {title[:50]}... ({article_url})")
            
            full_text = extract_article_text(article_url)
            if not full_text:
                logger.warning(f"Не удалось извлечь текст из {article_url}, используем заголовок и содержимое")
                full_text = f"{title} {content}"
            
            summary = summarize_with_openrouter(full_text)
            if not summary:
                logger.error(f"Не удалось создать сводку для {article_url}")
                continue
            
            # Добавляем новость в сводку
            clean_feed = clean_source_name(feed_title)
            category_summary += f"• {summary}\n  <a href='{article_url}'>#{clean_feed}</a>\n\n"
            
            # Задержка между обработками (чтобы не перегружать API)
            await asyncio.sleep(2)
        
        summary_messages.append(category_summary.strip())
    
    # Отправляем сводки по категориям
    bot = Bot(token=TELEGRAM_TOKEN)    
    for message in summary_messages:
        try:
            await bot.send_message(
                chat_id=CHAT_ID, 
                text=message, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"Отправлена сводка по категории")
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
    
    # Отправляем итоговое сообщение
    total_news = sum(len(news_list) for news_list in categorized_news.values())
    summary_footer = f"\n📊 <b>Всего обработано: {total_news} новостей</b>"
    
    try:
        await bot.send_message(
            chat_id=CHAT_ID, 
            text=summary_footer, 
            parse_mode='HTML',
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Ошибка отправки итогового сообщения: {e}")
    
    logger.info("Обработка завершена")

if __name__ == "__main__":
    asyncio.run(main())
        
