# -*- coding: utf-8 -*-
import os, re, json, datetime, asyncio
from urllib.parse import urlparse
from playwright.async_api import async_playwright

OUT_DIR = "out"
URL = os.environ.get("URL") or "https://cryptopanic.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
LOCALE = "en-US"
TIMEZONE_ID = "Europe/Athens"
VIEWPORT = {"width": 1280, "height": 800}

# Тайминги / селекторы
EXTRA_WAIT_MS = 5000            # ждём после загрузки страницы
NEWS_WAIT_TIMEOUT = 20000       # ждём появления первой новости
NEWS_ITEM_SELECTOR = ".news-row.news-row-link, .news-cell.nc-date"
CONTAINER_CANDIDATES = [
    "div.news-container.ps",        # основной контейнер с perfect-scrollbar
    "div.news-container",
    "div[class*='news-container']",
]
SCROLL_TARGET_MIN = 300          # хотим минимум столько карточек
SCROLL_PAUSE_MS = 400            # пауза между скроллами
SCROLL_MAX_STEPS = 600           # предохранитель
STALL_LIMIT = 12                 # сколько "пустых" итераций терпим

def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "file"
    return s[:max_len]

async def maybe_accept_cookies(page) -> bool:
    """
    Пытаемся убрать куки-баннер CryptoPanic: <a class="btn btn-outline-primary">Accept</a>.
    """
    selectors = [
        'a.btn.btn-outline-primary:has-text("Accept")',
        'a:has-text("Accept")',
        'button:has-text("Accept")',
        'text=Accept',
        'a:has-text("Принять")',
        'button:has-text("Принять")',
    ]
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1200)
            await page.wait_for_timeout(300)
            return True
        except Exception:
            pass
    return False

async def pick_scroll_container(page):
    """
    Возвращает локатор скролл-контейнера, если найден, иначе None.
    """
    for sel in CONTAINER_CANDIDATES:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return sel, loc
        except Exception:
            pass
    return None, None

async def scroll_until_count(page, item_selector, min_count, container_loc=None):
    """
    Скроллит страницу/контейнер вниз, пока не наберём min_count элементов
    или не упрёмся в лимиты.
    """
    steps = 0
    stalled = 0
    last_count = await page.locator(item_selector).count()

    while steps < SCROLL_MAX_STEPS:
        if last_count >= min_count:
            break

        # Скролл
        try:
            if container_loc:
                await container_loc.evaluate(
                    "el => el.scrollTo({ top: el.scrollHeight, behavior: 'instant' })"
                )
            else:
                await page.evaluate(
                    "window.scrollTo(0, document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight)"
                )
        except Exception:
            # Фолбэк мышиным колесом
            try:
                await page.mouse.wheel(0, 1800)
            except Exception:
                pass

        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        steps += 1

        # Проверяем прогресс
        curr = await page.locator(item_selector).count()
        if curr <= last_count:
            stalled += 1
        else:
            stalled = 0
            last_count = curr

        if stalled >= STALL_LIMIT:
            break

    return {
        "final_count": last_count,
        "steps": steps,
        "stalled_iterations": stalled,
        "reached_goal": last_count >= min_count,
    }

async def run():
    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE_ID,
            viewport=VIEWPORT,
            extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
        )
        page = await context.new_page()

        # Загружаем страницу
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)

        accepted = await maybe_accept_cookies(page)
        await page.wait_for_timeout(EXTRA_WAIT_MS)

        # Ждём, чтобы появилась хоть одна новость
        news_ready = False
        try:
            await page.wait_for_selector(NEWS_ITEM_SELECTOR, timeout=NEWS_WAIT_TIMEOUT)
            news_ready = True
        except Exception:
            news_ready = False  # всё равно продолжим

        # Ищем скролл-контейнер
        container_sel, container_loc = await pick_scroll_container(page)

        # Скроллим до 300 элементов (если новости появились)
        scroll_stats = None
        if news_ready:
            scroll_stats = await scroll_until_count(
                page, NEWS_ITEM_SELECTOR, SCROLL_TARGET_MIN, container_loc
            )

        # Сохраняем HTML/скрин
        host = urlparse(URL).netloc or "demo"
        stem = safe_filename(host)
        html_path = f"{OUT_DIR}/demo_{stem}.html"
        png_path  = f"{OUT_DIR}/demo_{stem}.png"

        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(await page.content())
        except Exception:
            pass
        try:
            await page.screenshot(path=png_path, full_page=True)
        except Exception:
            pass

        # Итоговый JSON
        result = {
            "scraped_at_utc": utcnow_iso(),
            "url": URL,
            "accepted_cookies": accepted,
            "news_ready": news_ready,
            "wait_ms": EXTRA_WAIT_MS,
            "container_selector": container_sel,
            "scroll": scroll_stats,
            "html_file": html_path,
            "screenshot_file": png_path,
        }
        with open(f"{OUT_DIR}/demo.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        await context.close()
        await browser.close()

    print("Saved demo → out/demo.json (+ html/png).")

if __name__ == "__main__":
    asyncio.run(run())
