# -*- coding: utf-8 -*-
"""
CryptoPanic demo with Playwright:
- waits 5s after load, tries to accept cookies
- infinite-scroll until >=300 items AND at least R steps (R in [30..40])
- handles "Loading..." and "Load more" at the bottom (with reload fallback)
- extracts structured news items and saves into out/demo.json (+ html/png)
"""
import os, re, json, datetime, asyncio, random, time
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

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

# --- timing / selectors
EXTRA_WAIT_MS = 5000           # wait after first load
NEWS_WAIT_TIMEOUT = 20000      # wait for first news row
SCROLL_PAUSE_MS = 350          # pause between scrolls
SCROLL_MAX_STEPS = 900         # hard safety limit per attempt
STALL_LIMIT = 15               # how many "no progress" iterations we tolerate

SCROLL_TARGET_MIN = 300        # want at least this many news
RAND_SCROLLS_MIN = 30          # lower bound of random minimal scroll steps
RAND_SCROLLS_MAX = 40          # upper bound

NEWS_ITEM_SELECTOR = "div.news-row.news-row-link"
CONTAINER_CANDIDATES = [
    "div.news-container.ps",   # perfect-scrollbar container (most likely)
    "div.news-container",
    "div[class*='news-container']",
]

# bottom controls
LOAD_MORE_ROOT = "div.news-load-more"
LOAD_MORE_BTN = f"{LOAD_MORE_ROOT} button:has-text('Load more')"
LOADING_SPAN = f"{LOAD_MORE_ROOT} span:has-text('Loading...')"

def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "file"
    return s[:max_len]

async def maybe_accept_cookies(page) -> bool:
    """
    Tries to dismiss cookie banner on CryptoPanic.
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
    Returns (selector, locator) for the scrollable container, or (None, None).
    """
    for sel in CONTAINER_CANDIDATES:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return sel, loc
        except Exception:
            pass
    return None, None

async def wait_loading_spinner_disappear(page) -> bool:
    """
    If 'Loading...' visible at the bottom, wait up to 10s until it goes away.
    Returns True if it's gone, False if timed out.
    """
    root = page.locator(LOADING_SPAN)
    try:
        if await root.count() == 0:
            return True
        await root.wait_for(state="hidden", timeout=10_000)
        return True
    except PWTimeout:
        return False
    except Exception:
        # if the locator misbehaved, don't block the flow
        return True

async def click_load_more_until_done(page) -> bool:
    """
    If 'Load more' button is present, click it every ~5s up to 15s total.
    Returns True if button is gone (success), False if stuck (should reload).
    """
    started = time.monotonic()
    while True:
        btn = page.locator(LOAD_MORE_BTN)
        try:
            visible = await btn.is_visible()
        except Exception:
            visible = False

        if not visible:
            return True

        try:
            await btn.click(timeout=1500)
        except Exception:
            pass

        await page.wait_for_timeout(5000)
        # re-check
        try:
            if not await page.locator(LOAD_MORE_BTN).is_visible():
                return True
        except Exception:
            return True

        if time.monotonic() - started > 15:
            return False

async def scroll_once(page, container_loc):
    """
    Scrolls to bottom either inside the container or the whole page.
    """
    try:
        if container_loc:
            await container_loc.evaluate(
                "el => { el.scrollTop = el.scrollHeight; }"
            )
        else:
            await page.evaluate(
                "window.scrollTo(0, document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight)"
            )
    except Exception:
        # fallback via mouse wheel
        try:
            await page.mouse.wheel(0, 2000)
        except Exception:
            pass

async def ensure_progress_or_reload(page) -> bool:
    """
    Handles 'Loading...' and 'Load more' at the bottom.
    Returns True if OK to continue; False if we should reload the page.
    """
    # Loading...
    ok = await wait_loading_spinner_disappear(page)
    if not ok:
        return False

    # Load more
    ok = await click_load_more_until_done(page)
    if not ok:
        return False

    return True

async def scroll_until_goals(page, item_selector, min_items, min_steps, container_loc):
    """
    Scrolls until both goals are satisfied:
      - have at least `min_items` items AND
      - performed at least `min_steps` scroll steps.
    Stops earlier on STALL_LIMIT/SCROLL_MAX_STEPS or reload requirement.

    Returns dict with stats and whether reload is required.
    """
    steps = 0
    stalled = 0
    try:
        last_count = await page.locator(item_selector).count()
    except Exception:
        last_count = 0

    while steps < SCROLL_MAX_STEPS:
        # goals check first (we may already have enough)
        if last_count >= min_items and steps >= min_steps:
            break

        # do one scroll
        await scroll_once(page, container_loc)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        steps += 1

        # check bottom controls
        ok = await ensure_progress_or_reload(page)
        if not ok:
            return {
                "final_count": last_count,
                "steps": steps,
                "stalled_iterations": stalled,
                "reached_goal": False,
                "reload_required": True,
            }

        # progress check
        try:
            curr = await page.locator(item_selector).count()
        except Exception:
            curr = last_count

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
        "reached_goal": (last_count >= min_items and steps >= min_steps),
        "reload_required": False,
    }

EXTRACT_JS = """
() => {
  function abs(u){ try { return new URL(u, location.origin).href } catch(e){ return u || '' } }
  const out = [];
  const rows = document.querySelectorAll("div.news-row.news-row-link");
  rows.forEach(row => {
    const aTitle = row.querySelector("a.news-cell.nc-title");
    const aDate  = row.querySelector("a.news-cell.nc-date");
    const href   = (aTitle?.getAttribute("href") || aDate?.getAttribute("href") || "").trim();
    const timeEl = row.querySelector("a.news-cell.nc-date time");
    const time_iso = (timeEl?.getAttribute("datetime") || "").trim();
    const time_rel = (timeEl?.textContent || "").trim();
    // первый span в .title-text — это заголовок
    const title = (row.querySelector(".nc-title .title-text span")?.textContent || "").trim();
    const source = (row.querySelector(".si-source-domain")?.textContent || "").trim();
    if (href) {
      out.push({
        url_rel: href,
        url_abs: abs(href),
        time_iso,
        time_rel,
        title,
        source
      });
    }
  });
  return out;
}
"""

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

        attempts = 0
        max_attempts = 3
        scroll_stats = None
        container_sel = None
        min_scrolls_required = random.randint(RAND_SCROLLS_MIN, RAND_SCROLLS_MAX)
        accepted = False
        news_ready = False

        while attempts < max_attempts:
            attempts += 1

            # load page
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            # cookies + initial wait
            accepted = await maybe_accept_cookies(page) or accepted
            await page.wait_for_timeout(EXTRA_WAIT_MS)

            # wait for at least one news row
            try:
                await page.wait_for_selector(NEWS_ITEM_SELECTOR, timeout=NEWS_WAIT_TIMEOUT)
                news_ready = True
            except Exception:
                news_ready = False

            # pick container and scroll
            container_sel, container_loc = await pick_scroll_container(page)

            if news_ready:
                scroll_stats = await scroll_until_goals(
                    page,
                    NEWS_ITEM_SELECTOR,
                    SCROLL_TARGET_MIN,
                    min_scrolls_required,
                    container_loc,
                )
                if scroll_stats.get("reload_required"):
                    # reload and try again from scratch
                    continue
                else:
                    break
            else:
                # nothing rendered? reload
                continue

        # save HTML/screenshot for diagnostics
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

        # parse items from DOM
        items = []
        try:
            items = await page.evaluate(EXTRACT_JS)
        except Exception:
            items = []

        result = {
            "scraped_at_utc": utcnow_iso(),
            "url": URL,
            "attempts": attempts,
            "accepted_cookies": accepted,
            "news_ready": news_ready,
            "min_scrolls_required": min_scrolls_required,
            "scroll": scroll_stats,
            "found": len(items),
            "items": items,               # список словарей с полезными полями
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
