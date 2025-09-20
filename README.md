# actions-playwright-google

## Обзор
Утилита на Playwright для сценария CryptoPanic: сбор новостей, раскрытие оригинальных ссылок, догрузка текста источников и выгрузка результата в Google Apps Script.

В составе проекта:

- `scripts/demo_playwright.py` — высокоуровневый сценарий CryptoPanic, использующий пакет `scripts.cryptopanic`.
- `scripts/cryptopanic/` — модульные вспомогательные блоки:
  - `cleaning.py` — пайплайн очистки текста и фильтрация рекламных вставок.
  - `network.py` — асинхронное обогащение карточек, retry-логика для `/news/click/`, загрузка HTML/og:image.
  - `scroll.py` — управление прокруткой, кликами `Load more`, обход всплывающих окон и выбор контейнера прокрутки.
  - `integrations.py` — отправка выборки в GAS вебхук (синхронно/асинхронно).
  - `extractor.py` — скрипт-заготовка для выполнения в браузере и извлечения карточек.
- `extensions/` — вспомогательные расширения Chromium (например, `unblock-origin-lite`).
- `tests/` — unit-тесты очистки текстов и retry-логики сетевого слоя.

## Требования
- Python 3.11 или новее.
- Playwright с установленными браузерами.
- Зависимости из `requirements-playwright.txt`.

### Установка окружения
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install
```

## CryptoPanic demo (`scripts/demo_playwright.py`)
Скрипт собирает ленту CryptoPanic, раскрывает оригинальные URL через `/news/click/`, выгружает HTML/PNG и дополняет элементы текстом источника с помощью `fetch_page_text`. При наличии переменных окружения результат отправляется в GAS вебхук.

### Ключевые переменные окружения
- `URL` — стартовая страница (по умолчанию `https://cryptopanic.com`).
- `EXTENSION_DIR` — путь к каталогу расширения Chromium, например `extensions/unblock-origin-lite`.
- `CLICK_CONCURRENCY` — максимальное число параллельных обращений к `/news/click/` (по умолчанию 8).
- `TEXT_GLOBAL_CONCURRENCY` — глобальный лимит параллельных загрузок источников (по умолчанию 20).
- `TEXT_PER_DOMAIN` — лимит одновременных загрузок на домен (по умолчанию 1).
- `GAS_WEBHOOK_URL` и `GAS_PASSWORD` — доступ к Google Apps Script для выгрузки результата.

### Пример запуска
```bash
URL=https://cryptopanic.com EXTENSION_DIR=extensions/unblock-origin-lite \
python scripts/demo_playwright.py
```

### Выходные артефакты
По завершении выполнения создаётся каталог `out/` со следующими файлами:
- `demo_<host>.html` и `demo_<host>.png` — HTML и скриншот страницы списка новостей.
- `demo.json` — итоговая выборка новостей с нормализованными данными, оригинальными ссылками и текстами источников.

## Расширения Chromium
Чтобы активировать облегчённую версию `uBlock Origin Lite`, передайте путь к расширению через переменную `EXTENSION_DIR` при запуске CryptoPanic demo:
```bash
EXTENSION_DIR=extensions/unblock-origin-lite python scripts/demo_playwright.py
```
Playwright запустит Chromium через `launch_persistent_context`, подключит расширение и попытается применить оптимальные пресеты фильтров.

## Тестирование
Для локальной проверки вспомогательных модулей используйте `pytest`:
```bash
pytest -q
```
Тесты покрывают пайплайн очистки текстов (`clean_text_pipeline`) и retry-логику `_resolve_click_sync`.

## Структура логов и артефактов
Сценарий CryptoPanic сохраняет артефакты в каталоге `out/`. Итоговый JSON включает сводку ошибок и блокировок. Логи рекомендуется направлять в стандартные потоки; при необходимости подключите сторонние обработчики.

