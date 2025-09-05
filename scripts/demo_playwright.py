# -*- coding: utf-8 -*-
"""
CryptoPanic demo with Playwright + enrich:
- Нормализуем time_iso в 'YYYY-MM-DDTHH:MM:SSZ' (UTC)
- Извлекаем id_news
- GET к https://cryptopanic.com/news/click/{id}/ -> original_url
  * до 10 ретраев при 429 с рандомной паузой (учитываем Retry-After если есть)
  * по итогу, если все 10 раз 429 — пишем "HTTP_429:https://cryptopanic.com/news/click/XXXXXX/"
- Если original_url имеет вид "HTTP_XXX:https://<domain>/..." и <domain> == source, то убираем префикс "HTTP_XXX:" — оставляем чистый URL
- Фильтр по source/original_url на binance.com/x.com/youtube.com
- Извлекаем coins + votes
- Дедуп по id_news
"""
import os, re, json, datetime, asyncio, random, time, email.utils
from urllib.parse import urlparse
import requests

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
    "div.news-container.ps",
    "div.news-container",
    "div[class*='news-container']",
]

# bottom controls
LOAD_MORE_ROOT = "div.news-load-more"
LOAD_MORE_BTN = f"{LOAD_MORE_ROOT} button:has-text('Load more')"
LOADING_SPAN = f"{LOAD_MORE_ROOT} span:has-text('Loading...')"

BANNED_SUBSTRINGS = ("binance.com", "x.com", "youtube.com")

# --- click resolver tuning (anti-429) ---
CLICK_CONCURRENCY = int(os.environ.get("CLICK_CONCURRENCY", "8"))  # мягче, чтобы меньше 429
CLICK_MAX_TRIES = 10
CLICK_SLEEP_MIN_SEC = 2
CLICK_SLEEP_MAX_SEC = 6
CLICK_TIMEOUT_SEC = 30

# ---------------- utils ----------------
def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "file"
    return s[:max_len]

def normalize_time_iso_py(s: str) -> str:
    """
    Привести строку даты к 'YYYY-MM-DDTHH:MM:SSZ' (UTC).
    Если не удается — вернуть исходник.
    """
    if not s:
        return s
    try:
        if s.endswith("Z"):
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.datetime.fromisoformat(s)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        try:
            dt = email.utils.parsedate_to_datetime(s)
            return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return s

def domain_eq(a: str, b: str) -> bool:
    a = (a or "").lower().strip()
    b = (b or "").lower().strip()
    if not a or not b:
        return False
    if a.startswith("www."): a = a[4:]
    if b.startswith("www."): b = b[4:]
    return a == b

def parse_retry_after(value: str) -> float | None:
    """Поддержка секундного Retry-After или даты."""
    if not value:
        return None
    value = value.strip()
    # 1) число секунд
    if re.fullmatch(r"\d+", value):
        try:
            return float(value)
        except Exception:
            return None
    # 2) http-date
    try:
        dt = email.utils.parsedate_to_datetime(value)
        # если дата в прошлом — игнор
        delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None

# ---------------- page helpers ----------------
async def maybe_accept_cookies(page) -> bool:
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
    for sel in CONTAINER_CANDIDATES:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return sel, loc
        except Exception:
            pass
    return None, None

async def wait_loading_spinner_disappear(page) -> bool:
    root = page.locator(LOADING_SPAN)
    try:
        if await root.count() == 0:
            return True
        await root.wait_for(state="hidden", timeout=10_000)
        return True
    except PWTimeout:
        return False
    except Exception:
        return True

async def click_load_more_until_done(page) -> bool:
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
        try:
            if not await page.locator(LOAD_MORE_BTN).is_visible():
                return True
        except Exception:
            return True
        if time.monotonic() - started > 15:
            return False

async def scroll_once(page, container_loc):
    try:
        if container_loc:
            await container_loc.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        else:
            await page.evaluate(
                "window.scrollTo(0, document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight)"
            )
    except Exception:
        try:
            await page.mouse.wheel(0, 2000)
        except Exception:
            pass

async def ensure_progress_or_reload(page) -> bool:
    ok = await wait_loading_spinner_disappear(page)
    if not ok:
        return False
    ok = await click_load_more_until_done(page)
    if not ok:
        return False
    return True

async def scroll_until_goals(page, item_selector, min_items, min_steps, container_loc):
    steps = 0
    stalled = 0
    try:
        last_count = await page.locator(item_selector).count()
    except Exception:
        last_count = 0
    while steps < SCROLL_MAX_STEPS:
        if last_count >= min_items and steps >= min_steps:
            break
        await scroll_once(page, container_loc)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        steps += 1
        ok = await ensure_progress_or_reload(page)
        if not ok:
            return {
                "final_count": last_count,
                "steps": steps,
                "stalled_iterations": stalled,
                "reached_goal": False,
                "reload_required": True,
            }
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

# ----------- JS extractor (coins + votes) -----------
EXTRACT_JS = r"""
() => {
  function abs(u){ try { return new URL(u, location.origin).href } catch(e){ return u || '' } }
  function isoUTCFromAttr(el){
    try{
      if(!el) return "";
      const raw = el.getAttribute("datetime") || "";
      if(!raw) return "";
      const d = new Date(raw);
      if (isNaN(d)) return "";
      return d.toISOString().replace(/\.\d{3}Z$/, "Z"); // UTC без миллисекунд
    } catch(e){ return "" }
  }
  function extractCoins(row){
    const zone = row.querySelector(".news-cell.nc-currency");
    if(!zone) return "---";
    const coins = [];
    zone.querySelectorAll("a[href]").forEach(a=>{
      const h = a.getAttribute("href") || "";
      const m = h.match(/\/news\/([^\/]+)\//i);
      if(m){
        let tick = (a.textContent || "").trim();
        if(tick && tick[0] !== "$") tick = "$" + tick + " ";
        coins.push({ coin: m[1], tick });
      }
    });
    return coins.length ? coins : "---";
  }
  function extractVotes(row){
    const votes = {comments:0, likes:0, dislikes:0, lol:0, save:0, important:0, negative:0, neutral:0};
    row.querySelectorAll("[title]").forEach(el=>{
      const t = (el.getAttribute("title") || "").toLowerCase();
      const m = t.match(/(\d+)\s+(comments?|like|dislike|lol|save|important|negative|neutral)\s+votes?/i);
      if(m){
        const n = parseInt(m[1],10);
        const key = m[2];
        if(/comment/.test(key)) votes.comments = n;
        else if(key==="like") votes.likes = n;
        else if(key==="dislike") votes.dislikes = n;
        else if(key==="lol") votes.lol = n;
        else if(key==="save") votes.save = n;
        else if(key==="important") votes.important = n;
        else if(key==="negative") votes.negative = n;
        else if(key==="neutral") votes.neutral = n;
      }
    });
    return votes;
  }

  const out = [];
  const rows = document.querySelectorAll("div.news-row.news-row-link");
  rows.forEach(row => {
    const aTitle = row.querySelector("a.news-cell.nc-title");
    const aDate  = row.querySelector("a.news-cell.nc-date");
    const href   = (aTitle?.getAttribute("href") || aDate?.getAttribute("href") || "").trim();
    const timeEl = row.querySelector("a.news-cell.nc-date time");
    const time_iso_utc = isoUTCFromAttr(timeEl);
    const time_rel = (timeEl?.textContent || "").trim();
    const title = (row.querySelector(".nc-title .title-text span")?.textContent || "").trim();
    const source = (row.querySelector(".si-source-domain")?.textContent || "").trim();
    const idm = href.match(/\/news\/(\d+)/);
    const id_news = idm ? idm[1] : "";

    if (href) {
      out.push({
        url_rel: href,
        url_abs: abs(href),
        time_iso: time_iso_utc,
        time_rel,
        title,
        source,
        id_news,
        coins: extractCoins(row),
        votes: extractVotes(row)
      });
    }
  });
  return out;
}
"""

# ----------- click resolver (c 429-ретраями) -----------
def _resolve_click_sync(id_news: str) -> str:
    """
    Возвращает финальный URL (200) или "HTTP_XXX:<final_or_click_url>".
    При 429 делает до CLICK_MAX_TRIES попыток с паузой.
    """
    click_url = f"https://cryptopanic.com/news/click/{id_news}/"  # важно: со слешем
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Referer": "https://cryptopanic.com/",
    }
    last_exc = None
    last_status = None
    for attempt in range(1, CLICK_MAX_TRIES + 1):
        try:
            r = requests.get(click_url, headers=headers, allow_redirects=True, timeout=CLICK_TIMEOUT_SEC)
            final_url = str(r.url) if getattr(r, "url", None) else ""
            status = r.status_code
            if status == 429:
                last_status = 429
                # предпочтём Retry-After, если есть
                retry_after_hdr = r.headers.get("Retry-After")
                sleep_s = parse_retry_after(retry_after_hdr)
                if sleep_s is None:
                    sleep_s = random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC)
                time.sleep(sleep_s)
                continue  # пробуем ещё
            if status != 200:
                return f"HTTP_{status}:{final_url or click_url}"
            return final_url or f"HTTP_{status}:{click_url}"
        except Exception as e:
            last_exc = e
            # мягкая пауза и ещё раз
            time.sleep(random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC))
    # Если все попытки с 429 — возвращаем специальную метку
    if last_status == 429:
        return f"HTTP_429:{click_url}"
    # Иначе ошибка сети и т.п.
    return f"ERROR:{repr(last_exc) if last_exc else 'unknown'}"

async def resolve_original_urls(items: list, concurrency: int):
    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        idn = item.get("id_news", "")
        if not idn:
            item["original_url"] = ""
            return
        async with sem:
            res = await asyncio.to_thread(_resolve_click_sync, idn)
            item["original_url"] = res

    await asyncio.gather(*(one(it) for it in items))

# ----------- pipeline utils -----------
def dedupe_by_id(items: list) -> list:
    seen = set()
    out = []
    for it in items:
        idn = it.get("id_news", "")
        if idn and idn not in seen:
            seen.add(idn)
            out.append(it)
    return out

def filter_banned(items: list) -> list:
    out = []
    for it in items:
        source = (it.get("source") or "").lower()
        orig = (it.get("original_url") or "").lower()
        bad = any(b in source for b in BANNED_SUBSTRINGS) or any(b in orig for b in BANNED_SUBSTRINGS)
        if not bad:
            out.append(it)
    return out

def clean_original_vs_source(items: list) -> None:
    """
    Если original_url начинается с 'HTTP_XXX:' и домен в URL совпадает с source,
    то убираем префикс 'HTTP_XXX:' и оставляем чистый URL.
    Пример:
    source = 'blocknews.com'
    original_url = 'HTTP_404:https://blocknews.com/....' -> 'https://blocknews.com/...'
    """
    for it in items:
        orig = it.get("original_url") or ""
        src = (it.get("source") or "").strip()
        m = re.match(r"^HTTP_\d{3}:(https?://.+)$", orig, flags=re.I)
        if not (m and src):
            continue
        url_only = m.group(1)
        dom = urlparse(url_only).netloc
        if domain_eq(dom, src):
            it["original_url"] = url_only  # чистим префикс

# ----------- main run -----------
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
        min_scrolls_required = random.randint(RAND_SCROLLS_MIN, RAND_SCROLLS_MAX)
        accepted = False
        news_ready = False

        while attempts < max_attempts:
            attempts += 1
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            accepted = await maybe_accept_cookies(page) or accepted
            await page.wait_for_timeout(EXTRA_WAIT_MS)

            try:
                await page.wait_for_selector(NEWS_ITEM_SELECTOR, timeout=NEWS_WAIT_TIMEOUT)
                news_ready = True
            except Exception:
                news_ready = False

            _, container_loc = await pick_scroll_container(page)

            if news_ready:
                scroll_stats = await scroll_until_goals(
                    page,
                    NEWS_ITEM_SELECTOR,
                    SCROLL_TARGET_MIN,
                    min_scrolls_required,
                    container_loc,
                )
                if scroll_stats.get("reload_required"):
                    continue
                else:
                    break
            else:
                continue

        # save HTML/screenshot
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
        try:
            items = await page.evaluate(EXTRACT_JS)
        except Exception:
            items = []

        # normalize time
        for it in items:
            it["time_iso"] = normalize_time_iso_py(it.get("time_iso", ""))

        # первичный дедуп по id_news
        items = dedupe_by_id(items)

        # подтянем original_url (с ограничением конкуррентности и ретраями на 429)
        await resolve_original_urls(items, concurrency=CLICK_CONCURRENCY)

        # пост-обработка original_url против source (чистим HTTP_XXX: префикс при совпадении домена)
        clean_original_vs_source(items)

        # фильтрация по нежелательным доменам
        items = filter_banned(items)

        # финальная дедупликация
        items = dedupe_by_id(items)

        result = {
            "scraped_at_utc": utcnow_iso(),
            "url": URL,
            "attempts": attempts,
            "accepted_cookies": accepted,
            "news_ready": news_ready,
            "min_scrolls_required": min_scrolls_required,
            "scroll": scroll_stats,
            "found": len(items),
            "items": items,
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
