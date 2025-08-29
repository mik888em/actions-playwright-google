# -*- coding: utf-8 -*-
"""
Robust Playwright Google SERP (for CI):
- Chromium headless в GH Actions
- Базовая версия выдачи (gbv=1)
- Расширенная обработка consent/блокировок
- Всегда сохраняем HTML/скриншот
- При блокировке не падаем: result.json с blocked=true
"""
import re
import os
import json
import asyncio
import datetime
import random
from urllib.parse import quote_plus
from playwright.async_api import async_playwright

# ===== Параметры =====
QUERY = os.environ.get("QUERY") or "site:example.com"

# gbv=1 — упрощённая HTML-версия выдачи; hl/gl можно менять
GOOGLE_SEARCH_URL = (
    "https://www.google.com/search?q={query}&hl=en&gl=us&pws=0&num=10&gbv=1"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
LOCALE = "en-US"
TIMEZONE_ID = "Europe/Athens"
VIEWPORT = {"width": 1280, "height": 800}

NAV_TIMEOUT_MS = 45000
MIN_PAUSE = 0.3
MAX_PAUSE = 0.7

OUT_DIR = "out"

def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

async def maybe_accept_consent(page) -> bool:
    """
    Пытаемся нажать кнопки согласия cookie/consent (разные варианты).
    Возвращает True, если что-то нажали.
    """
    selectors = [
        'button#L2AGLb',
        'button:has-text("I agree")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button[aria-label="Accept all"]',
        'form[action*="consent"] button[type="submit"]',
        'div[role="dialog"] button:has-text("Accept")',
        'button:has-text("Принять")',
        'button:has-text("Согласен")',
    ]
    # основной фрейм
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1200)
            await page.wait_for_timeout(400)
            return True
        except Exception:
            pass
    # во фреймах
    try:
        for frame in page.frames:
            for sel in selectors:
                try:
                    await frame.locator(sel).first.click(timeout=800)
                    await page.wait_for_timeout(400)
                    return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

async def detect_google_block(page) -> str:
    """
    Возвращает строку-причину блокировки либо ''.
    Ищем характерные признаки "sorry/unusual traffic".
    """
    try:
        url = page.url or ""
        if "/sorry/" in url:
            return "Google 'Sorry' page (rate-limited/captcha)."

        body_text = ""
        try:
            body_text = await page.inner_text("body", timeout=2000)
        except Exception:
            pass
        low = (body_text or "").lower()

        markers = [
            "unusual traffic from your computer network",
            "to continue, please type the characters",
            "automated queries",
            "detected unusual traffic",
        ]
        for m in markers:
            if m in low:
                return "Unusual traffic / captcha detected."
    except Exception:
        pass
    return ""

async def extract_results(page):
    """
    Возвращает список результатов [{title, url}], стараясь покрыть базовую/обычную верстку.
    """
    # ждём любой из контейнеров выдачи
    containers = ["div#search", "div#main", "div#rso"]
    found = None
    for sel in containers:
        try:
            await page.wait_for_selector(sel, timeout=10000)
            found = sel
            break
        except Exception:
            pass

    if not found:
        return [], None

    # парсим — сперва h3 внутри ссылок
    results = await page.evaluate("""
    () => {
      function abs(u){ try { return new URL(u, location.href).href } catch(e){ return u || '' } }
      const items = [];
      document.querySelectorAll('a h3').forEach(h3 => {
        const a = h3.closest('a');
        if (a && a.href) {
          const title = (h3.textContent || '').trim();
          if (title) items.push({ title, url: abs(a.href) });
        }
      });
      // Fallback для базовой версии/иной разметки
      if (items.length < 5) {
        document.querySelectorAll('div#search a[href*="/url?"]').forEach(a => {
          const title = (a.textContent || '').trim();
          if (title) items.push({ title, url: abs(a.href) });
        });
      }
      // dedup + top10
      const seen = new Set();
      const out = [];
      for (const it of items) {
        if (it.url && !seen.has(it.url)) {
          seen.add(it.url);
          out.push(it);
        }
      }
      return out.slice(0, 10);
    }
    """)

    return results, found

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

        url = GOOGLE_SEARCH_URL.format(query=quote_plus(QUERY))
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await maybe_accept_consent(page)
        await page.wait_for_timeout(int(1000 * random.uniform(MIN_PAUSE, MAX_PAUSE)))

        # сохраняем HTML/скриншот как можно раньше для диагностики
        def safe_filename(s: str, max_len: int = 100) -> str:
            s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE)
            s = s.strip("._") or "query"
            return s[:max_len]

        
        safe = safe_filename(QUERY)
        screenshot_path = f"{OUT_DIR}/google_{safe}.png"
        html_path = f"{OUT_DIR}/google_{safe}.html"

        # Пробуем понять, не блок ли
        block_reason = await detect_google_block(page)

        # Пытаемся извлечь результаты
        results, container = await extract_results(page)

        # Сохраняем скрин/HTML в любом раскладе
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(await page.content())
        except Exception:
            pass

        data = {
            "scraped_at_utc": utcnow_iso(),
            "query": QUERY,
            "page_url": page.url,
            "container": container,
            "blocked": bool(block_reason) or (not results and not container),
            "block_reason": block_reason,
            "count": len(results),
            "results": results,
        }
        with open(f"{OUT_DIR}/result.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        await context.close()
        await browser.close()

    # Не бросаем исключение — job должен завершаться успехом.
    print("Saved → out/result.json (+ html/png).")

if __name__ == "__main__":
    asyncio.run(run())
