# actions-playwright-google

## Обзор
Автоматизации на Playwright для получения выдачи Google и демонстрации парсинга CryptoPanic. Репозиторий включает:
- `scripts/demo_playwright.py` — асинхронный сборщик CryptoPanic с обогащением карточек и выгрузкой в GAS.
- `scripts/google_serp_playwright.py` — headless-сценарий для базовой выдачи Google (режим gbv=1).
- `extensions/` — дополнительные расширения Chromium, в том числе `unblock-origin-lite` для блокировки трекеров.

## Требования
- Python 3.11 или новее.
- Установленный Playwright и поддерживаемые браузеры.
- Зависимости из `requirements-playwright.txt`.

Установка окружения:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install
```

## `scripts/demo_playwright.py`
Скрипт собирает ленту CryptoPanic, раскрывает оригинальные ссылки и, при необходимости, выгружает результат в Google Apps Script.

### Переменные окружения
- `URL` — адрес стартовой страницы (по умолчанию `https://cryptopanic.com`).
- `GAS_WEBHOOK_URL` и `GAS_PASSWORD` — параметры доступа к GAS вебхуку.
- `EXTENSION_DIR` — путь к каталогу расширения Chromium, например `extensions/unblock-origin-lite`.
- `CLICK_CONCURRENCY` — максимальное число одновременных переходов для раскрытия ссылок (по умолчанию 8).
- `TEXT_GLOBAL_CONCURRENCY` — глобальный лимит параллельных загрузок источников (по умолчанию 20).

### Пример запуска
```bash
URL=https://cryptopanic.com EXTENSION_DIR=extensions/unblock-origin-lite \
python scripts/demo_playwright.py
```

### Выходные файлы
По завершении выполнения создаётся каталог `out/` со следующими артефактами:
- `demo_<host>.html` и `demo_<host>.png` — HTML и скриншот страницы списка новостей.
- `demo.json` — итоговая выборка новостей с нормализованными данными.

## `scripts/google_serp_playwright.py`
Сценарий открывает упрощённую (gbv=1) выдачу Google, обходит consent-диалоги и сохраняет результат.

### Переменные окружения
- `QUERY` — поисковый запрос (по умолчанию `site:example.com`).

### Схема работы и артефакты
Запрос выполняется с параметром `gbv=1`, чтобы получать статичную HTML-версию SERP. Скрипт сохраняет:
- `out/google_<query>.html` — HTML-копию выдачи.
- `out/google_<query>.png` — скриншот страницы.
- `out/result.json` — сводка запроса, результатов и признаков блокировок.

## Расширение `extensions/unblock-origin-lite`
Каталог содержит облегчённую сборку блокировщика `uBlock Origin Lite`. Для подключения фильтрации при запуске `demo_playwright.py` передайте путь к расширению через переменную `EXTENSION_DIR`:
```bash
EXTENSION_DIR=extensions/unblock-origin-lite python scripts/demo_playwright.py
```
Playwright запустит Chromium в режиме `launch_persistent_context`, активирует расширение и попытается применить оптимальные пресеты фильтров автоматически.
