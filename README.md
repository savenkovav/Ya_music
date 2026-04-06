# Telegram-бот для Яндекс.Музыки

Бот принимает ссылку на трек Яндекс.Музыки и возвращает:
- название трека;
- имя артиста;
- длительность в формате `MM:SS` и в секундах.

## Требования

- Python `3.11+`
- Токен Telegram-бота от `@BotFather`
- Google Chrome/Chromium (для Selenium WebDriver)

## Настройка окружения

1. Скопируй значения из `env.example` в `.env` (файл уже создан в проекте).
2. Заполни переменную:
   - `BOT_TOKEN` — токен Telegram-бота.
3. При необходимости измени:
   - `REQUEST_TIMEOUT`
   - `USER_AGENT`
   - `SELENIUM_HEADLESS`
   - `SELENIUM_PAGELOAD_TIMEOUT`
   - `SELENIUM_WAIT_TIMEOUT`
   - `SELENIUM_EXTRA_WAIT_MS`
   - `USE_YANDEX_TOKEN_FALLBACK`
   - `YANDEX_MUSIC_TOKEN`
   - `LOG_LEVEL`

## Авторизация для API fallback

Бот не запрашивает логин/пароль. Токен Яндекс.Музыки задаётся только в файле `.env`:

- `YANDEX_MUSIC_TOKEN` — OAuth-токен;
- `USE_YANDEX_TOKEN_FALLBACK=true` — при капче на странице браузер парсинг дополняется запросом к API по этому токену.

При запуске бот проверяет токен из `.env`, если fallback включён.

## Запуск без Docker

```bash
pip install -r requirements.txt
python ya_music_bot.py
```

## Запуск через Docker Compose

```bash
docker compose up --build -d
```

Остановить:

```bash
docker compose down
```

## Поддерживаемые ссылки

- `https://music.yandex.ru/album/{album_id}/track/{track_id}`
- `https://music.yandex.ru/track/{track_id}`
- Ссылки с дополнительными query-параметрами (например, `?utm_source=...`)

## Что делает парсер

1. Проверяет корректность ссылки и домен.
2. Извлекает `track_id` (и `album_id`, если есть).
3. Загружает страницу трека с retry-механизмом.
4. Пытается извлечь данные из:
   - отрендеренного DOM через Selenium,
   - `application/ld+json`,
   - meta-тегов (`og:*`),
   - JSON-блоков страницы (`__NEXT_DATA__`, `window.__INITIAL_STATE__`).
5. Возвращает результат в Telegram.

## Обработка ошибок

Бот корректно сообщает о типовых проблемах:
- невалидная ссылка;
- трек недоступен в регионе;
- временная ошибка сети;
- не удалось извлечь данные со страницы.
