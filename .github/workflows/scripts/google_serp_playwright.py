
# -*- coding: utf-8 -*-
"""
Тестовый Playwright-скрапер:
- Имитирует Chromium (headless) в GitHub Actions.
- Открывает Google, выполняет запрос, извлекает первые результаты.
- Сохраняет JSON и скриншот в папку out/ (будет артефактом job).

Примечания:
- Это учебный пример. Для постоянной загрузки SERP используйте Google Custom Search JSON API.
- В ЕС Google может показывать cookie/consent — пытаемся нажать "Accept".
"""

import os
import json
import asyncio
import datetime
import random
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ===== Настройки =====
QUERY = os.environ.get("QUERY") or "site:example.com"
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=us&pws=0&num=10"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = "en-US,en;q=0.9"
LOCALE = "en-US"
TIMEZONE_ID = "Europe/Athens"  # не критично; просто пример
VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 30000  # 30s

MIN_PAUSE = 0.3
MAX_PAUSE = 0.7


def utcnow_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


async def maybe_accept_consent(page) -> bool:
    """
    Пытаемся нажать кнопку согласия, если всплыла (EU consent).
    Возвращает True, если кликнули что-то похожее на "Accept".
    """
    candidates = [
        'button#L2AGLb',  # частый id у Google
        'button:has-text("I agree")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button[aria-label="Accept all"]',
        'form[action*="consent"] button[type="submit"]',
        'button:has-text("Принять")',
        'button:has-text("Согласен")',
    ]
    # Сначала пробуем в основном фрейме
    for sel in candidates:
        try:
            await page.locator(sel).first.click(timeout=1200)
            await page.wait_for_timeout(400)
            return True
        except Exception:
            pass
    # Затем пробуем во фреймах (если политика в iframe)
    try:
        for frame in page.frames:
            for sel in candidates:
                try:
                    await frame.locator(sel).first.click(timeout=800)
                    await page.wait_for_timeout(400)
                    return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


async def run():
    os.makedirs("out", exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale=LOCALE,
            timezone_id=TIMEZONE_ID,
            viewport=VIEWPORT,
            extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
        )
        page = await context.new_page()

        # Переходим сразу на выдачу с параметрами
        url = GOOGLE_SEARCH_URL.format(query=quote_plus(QUERY))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            # иногда редиректит на согласие — подождём и снова попробуем
            pass

        # Если всплыло согласие — нажмём
        try:
            await maybe_accept_consent(page)
        except Exception:
            pass

        # Дождёмся блока результатов
        await page.wait_for_selector("div#search", timeout=20000)
        await page.wait_for_timeout(int(1000 * random.uniform(MIN_PAUSE, MAX_PAUSE)))

        # Собираем топовые результаты (заголовок + ссылка)
        # Берём h3 внутри ссылок в основном блоке результатов
        results = await page.evaluate("""
        () => {
          const out = [];
          document.querySelectorAll('div#search a h3').forEach(h3 => {
            const a = h3.closest('a');
            if (a && a.href) {
              const title = (h3.textContent || '').trim();
              if (title) out.push({ title, url: a.href });
            }
          });
          return out.slice(0, 10);
        }
        """)

        # Скриншот «как видим выдачу»
        safe_query = QUERY.replace(" ", "_").replace("/", "_")
        screenshot_path = f"out/google_{safe_query}.png"
        await page.screenshot(path=screenshot_path, full_page=True)

        data = {
            "scraped_at_utc": utcnow_iso(),
            "query": QUERY,
            "count": len(results),
            "results": results
        }
        with open("out/result.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        await context.close()
        await browser.close()

    print("Saved → out/result.json and screenshot")


if __name__ == "__main__":
    asyncio.run(run())
