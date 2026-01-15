#!/usr/bin/env python3
import os
import asyncio
import requests
import trafilatura
import logging
from datetime import datetime
from telegram import Bot
import openai
import re

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

def get_unread_entries():
    """Получение непрочитанных статей из FreshRSS"""
    try:
        # Аутентификация
        auth_response = requests.post(f"{BASE_URL}/api/v1/auth", json={
            'identifier': USER,
            'password': PASS
        })

        if auth_response.status_code != 201:
            logger.error(f"Ошибка аутентификации: {auth_response.status_code}")
            return []

        token = auth_response.json().get('access_token')
        headers = {'Authorization': f'Bearer {token}'}

        # Получаем все статьи (или последние N)
        entries_response = requests.get(f"{BASE_URL}/api/v1/entries", headers=headers)

        if entries_response.status_code == 200:
            all_entries = entries_response.json().get('items', [])
            logger.info(f"Получено {len(all_entries)} статей из FreshRSS")
            return all_entries
        else:
            logger.error(f"Ошибка получения статей: {entries_response.status_code}")
            return []
    except Exception as e:
        logger.error(f"Ошибка при получении статей: {e}")
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
                return True
        else:
            # Если категории нет в словаре, ищем просто как текст
            if category_lower in combined_text:
                return True

    return False

def extract_article_text(url):
    """Извлечение текста статьи"""
    try:
        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded)
        return text or ""
    except Exception as e:
        logger.error(f"Ошибка извлечения текста из {url}: {e}")
        return ""

def summarize_with_openrouter(text, model_index=0):
    """Генерация краткой сводки с помощью ИИ"""
    if model_index >= len(FREE_MODELS):
        logger.error("Все модели исчерпаны")
        return None

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
        return summarize_with_openrouter(text, model_index + 1)

async def send_to_telegram(message):
    """Отправка сообщения в Telegram"""
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            disable_web_page_preview=True
        )
        logger.info(f"Сообщение отправлено в Telegram: {message[:50]}...")
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

def clean_source_name(title):
    """Очистка названия источника для хэштега"""
    # Убираем спецсимволы, оставляем только буквы и цифры
    cleaned = re.sub(r'[^\w\s-]', '', title.lower())
    cleaned = re.sub(r'\s+', '_', cleaned.strip())
    return cleaned.replace('-', '_')

def get_category_hashtag(title, content, feed_title):
    """Определение категории для хэштега"""
    combined_text = f"{title} {content} {feed_title}".lower()

    for category in CATEGORIES:
        category_lower = category.strip().lower()
        if category_lower in CATEGORY_KEYWORDS:
            keywords = CATEGORY_KEYWORDS[category_lower]
            if any(keyword.lower() in combined_text for keyword in keywords):
                return category_lower.replace(' ', '_')
        else:
            if category_lower in combined_text:
                return category_lower.replace(' ', '_')

    # Если не нашли, используем первую категорию
    return CATEGORIES[0].strip().replace(' ', '_')

async def main():
    logger.info(f"Запуск обработки новостей по категориям: {CATEGORIES}")

    all_entries = get_unread_entries()
    logger.info(f"Получено {len(all_entries)} статей из FreshRSS")

    # Фильтруем статьи по категориям
    filtered_entries = [entry for entry in all_entries if matches_category(entry)]
    logger.info(f"Отфильтровано до {len(filtered_entries)} статей по категориям")

    for entry in filtered_entries:
        try:
            article_url = entry.get('alternate', [{}])[0].get('href') or entry.get('url')
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

            # Определяем хэштег категории
            category_hashtag = get_category_hashtag(title, content, feed_title)
            clean_feed = clean_source_name(feed_title)

            message = f"{summary}\n\n#{category_hashtag} #{clean_feed}"

            await send_to_telegram(message)

            # Маленькая задержка между обработками
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Ошибка обработки статьи: {e}")
            continue

    logger.info("Обработка завершена")

if __name__ == "__main__":
    asyncio.run(main())
