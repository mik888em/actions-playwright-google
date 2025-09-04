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

EXTRA_WAIT_MS = 5000          # доп. ожидание после загрузки страницы (5c)
NEWS_WAIT_TIMEOUT = 20000     # ждём появления новости (до 20c)

def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "file"
    return s[:max_len]

async def maybe_accept_cookies(page) -> bool:
    """
    Пытаемся убрать куки-баннер (CryptoPanic: <a class="btn btn-outline-primary">Accept</a>).
    Возвращаем True, если кликнули успешно.
    """
    selectors = [
        'a:has-text("Accept")',
        'button:has-text("Accept")',
        'a.btn.btn-outline-primary:has-text("Accept")',
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

        # Грузим страницу и даём ей "подышать"
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)

        accepted = await maybe_accept_cookies(page)
        await page.wait_for_timeout(EXTRA_WAIT_MS)

        # Ждём, чтобы появилась хотя бы одна новость .news-cell.nc-date
        news_ready = False
        try:
            # достаточно появления любого такого элемента
            await page.wait_for_selector(".news-cell.nc-date", timeout=NEWS_WAIT_TIMEOUT)
            news_ready = True
        except Exception:
            news_ready = False  # всё равно продолжим — сделаем снимок для диагностики

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

        # Немного диагностических данных
        result = {
            "scraped_at_utc": utcnow_iso(),
            "url": URL,
            "accepted_cookies": accepted,
            "news_ready": news_ready,
            "wait_ms": EXTRA_WAIT_MS,
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
