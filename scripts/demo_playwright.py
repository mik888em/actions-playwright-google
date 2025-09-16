# -*- coding: utf-8 -*-
"""
CryptoPanic demo with Playwright + enrich:
- Нормализуем time_iso (UTC)
- Разрешаем original_url с ретраями при 429 (до 10 попыток, рандомная пауза/Retry-After)
- Чистим "HTTP_XXX:" если домен совпадает с source
- Фильтруем по нежелательным доменам
- Дедуп по id_news
- Тянем текст исходной статьи:
  * если домен == cryptopanic.com -> text_of_site = '---'
  * иначе сначала Ctrl+A / Ctrl+C + чтение клипборда; fallback: document.body/element.innerText
  * параллелим: глобально до TEXT_GLOBAL_CONCURRENCY, не более 1 запроса на домен одновременно
  * все исключения внутри задач подавляются, чтобы не отменять остальные
"""
import os, re, json, datetime, asyncio, random, time, email.utils, tempfile, shutil
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
EXTRA_WAIT_MS = 5000
NEWS_WAIT_TIMEOUT = 20000
SCROLL_PAUSE_MS = 350
SCROLL_MAX_STEPS = 900
STALL_LIMIT = 15

SCROLL_TARGET_MIN = 300
RAND_SCROLLS_MIN = 30
RAND_SCROLLS_MAX = 40

NEWS_ITEM_SELECTOR = "div.news-row.news-row-link"
CONTAINER_CANDIDATES = [
    "div.news-container.ps",
    "div.news-container",
    "div[class*='news-container']",
]

LOAD_MORE_ROOT = "div.news-load-more"
LOAD_MORE_BTN = f"{LOAD_MORE_ROOT} button:has-text('Load more')"
LOADING_SPAN = f"{LOAD_MORE_ROOT} span:has-text('Loading...')"

BANNED_SUBSTRINGS = ("binance.com", "x.com", "youtube.com")

# --- click resolver tuning (anti-429) ---
CLICK_CONCURRENCY = int(os.environ.get("CLICK_CONCURRENCY", "8"))
CLICK_MAX_TRIES = 10
CLICK_SLEEP_MIN_SEC = 2
CLICK_SLEEP_MAX_SEC = 6
CLICK_TIMEOUT_SEC = 30

# --- source-text fetcher tuning ---
TEXT_GLOBAL_CONCURRENCY = int(os.environ.get("TEXT_GLOBAL_CONCURRENCY", "20"))
TEXT_GOTO_TIMEOUT = 45000
TEXT_EXTRA_WAIT_MS = 1200
TEXT_JITTER_MIN_SEC = 0.6
TEXT_JITTER_MAX_SEC = 1.8
TEXT_PER_DOMAIN = 1  # не больше 1 запроса на домен одновременно

# ---------------- utils ----------------
def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "file"
    return s[:max_len]

def normalize_time_iso_py(s: str) -> str:
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

def parse_retry_after(value: str):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        try:
            return float(value)
        except Exception:
            return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None

def extract_url_only(s: str):
    """Из 'HTTP_404:https://site/...' или 'ERROR:...' достать сам https://..."""
    if not s:
        return None
    m = re.search(r"(https?://[^\s]+)", s, flags=re.I)
    return m.group(1) if m else None

def is_cryptopanic(url: str) -> bool:
    try:
        dom = urlparse(url).netloc.lower()
        return dom.endswith("cryptopanic.com")
    except Exception:
        return False

# ---------------- page helpers ----------------
async def maybe_accept_cookies(page) -> bool:
    selectors = [
        'button:has-text("Accept")',
        'a.btn.btn-outline-primary:has-text("Accept")',
        'a:has-text("Accept")',
        'text=Accept',
        'button:has-text("I agree")',
        'button:has-text("Allow all")',
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
      return d.toISOString().replace(/\.\d{3}Z$/, "Z");
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
    click_url = f"https://cryptopanic.com/news/click/{id_news}/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Referer": "https://cryptopanic.com/",
    }
    last_exc = None
    last_status = None
    for _ in range(1, CLICK_MAX_TRIES + 1):
        try:
            r = requests.get(click_url, headers=headers, allow_redirects=True, timeout=CLICK_TIMEOUT_SEC)
            final_url = str(r.url) if getattr(r, "url", None) else ""
            status = r.status_code
            if status == 429:
                last_status = 429
                retry_after_hdr = r.headers.get("Retry-After")
                sleep_s = parse_retry_after(retry_after_hdr)
                if sleep_s is None:
                    sleep_s = random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC)
                time.sleep(sleep_s)
                continue
            if status != 200:
                return f"HTTP_{status}:{final_url or click_url}"
            return final_url or f"HTTP_{status}:{click_url}"
        except Exception as e:
            last_exc = e
            time.sleep(random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC))
    if last_status == 429:
        return f"HTTP_429:{click_url}"
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

# ----------- source text fetcher -----------
async def fetch_page_text(context, url: str):
    """Открыть url, попытаться скопировать через буфер, fallback: innerText. Возвращает str или None."""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=TEXT_GOTO_TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        await maybe_accept_cookies(page)
        await page.wait_for_timeout(TEXT_EXTRA_WAIT_MS)

        # На всякий случай сфокусируем body.
        try:
            await page.locator("body").click(timeout=1500)
        except Exception:
            pass

        # Попытка Select All (через execCommand — устойчивее в headless)
        try:
            await page.evaluate("""() => {
                try {
                  const sel = window.getSelection();
                  if (sel) sel.removeAllRanges();
                  const range = document.createRange();
                  range.selectNodeContents(document.body || document.documentElement);
                  sel.addRange(range);
                  document.execCommand && document.execCommand('copy');
                } catch(e) {}
            }""")
        except Exception:
            pass

        # Ctrl+A / Ctrl+C
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Control+C")
        except Exception:
            pass

        clip = None
        try:
            # Может не сработать из-за политик — это нормально, есть fallback ниже.
            clip = await page.evaluate(
                "async () => { try { return await navigator.clipboard.readText(); } catch(e) { return null } }"
            )
        except Exception:
            clip = None

        if not clip or not clip.strip():
            try:
                clip = await page.evaluate(
                    "(() => {"
                    " const el = document.body || document.documentElement;"
                    " return el ? (el.innerText || el.textContent || '') : '';"
                    "})()"
                )
            except Exception:
                clip = None

        if clip is None:
            return None

        txt = re.sub(r"\r\n?", "\n", clip)
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        return txt if txt else None
    finally:
        try:
            await page.close()
        except Exception:
            pass

async def enrich_with_source_text(context, items: list):
    """
    Добавляет item['text_of_site'].
    Параллелим по доменам: не более 1 запроса на домен одновременно,
    и глобально не более TEXT_GLOBAL_CONCURRENCY.
    Любые исключения внутри задач глотаем во избежание отмены остальных.
    """
    for it in items:
        it["text_of_site"] = "---"

    tasks = []
    url_cache: dict[str, str] = {}

    urls = []
    for it in items:
        raw = it.get("original_url") or ""
        url = extract_url_only(raw)
        if not url:
            continue
        if is_cryptopanic(url):
            continue
        urls.append((it, url))

    unique_domains = set()
    for _, u in urls:
        try:
            unique_domains.add(urlparse(u).netloc.lower())
        except Exception:
            pass

    # семафоры по доменам и глобальный
    domain_sems = {dom: asyncio.Semaphore(TEXT_PER_DOMAIN) for dom in unique_domains}
    global_sem = asyncio.Semaphore(max(1, min(TEXT_GLOBAL_CONCURRENCY, len(unique_domains) or 1)))

    async def one(it, url):
        try:
            if url in url_cache:
                it["text_of_site"] = url_cache[url]
                return

            try:
                dom = urlparse(url).netloc.lower()
            except Exception:
                return

            # Лёгкий джиттер — меньше шансов триггерить защиту.
            await asyncio.sleep(random.uniform(TEXT_JITTER_MIN_SEC, TEXT_JITTER_MAX_SEC))

            async with global_sem:
                async with domain_sems.get(dom, asyncio.Semaphore(1)):
                    text = await fetch_page_text(context, url)
                    if text and text.strip():
                        it["text_of_site"] = text
                        url_cache[url] = text
        except Exception:
            # Любые ошибки источника не должны валить весь процесс.
            # Оставляем '---'.
            return

    for it, url in urls:
        tasks.append(asyncio.create_task(one(it, url)))

    if tasks:
        # Не прерываем остальные задачи, даже если часть упала.
        await asyncio.gather(*tasks, return_exceptions=True)

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
    for it in items:
        orig = it.get("original_url") or ""
        src = (it.get("source") or "").strip()
        m = re.match(r"^HTTP_\d{3}:(https?://.+)$", orig, flags=re.I)
        if not (m and src):
            continue
        url_only = m.group(1)
        dom = urlparse(url_only).netloc
        if domain_eq(dom, src):
            it["original_url"] = url_only

# ----------- main run -----------
async def run():
    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as p:
        # <<< ДОБАВЛЯЕМ/МЕНЯЕМ вот это >>>
        EXTENSION_DIR = os.environ.get("EXTENSION_DIR")  # путь к распакованному расширению
        common_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        
        browser = None  # чтобы корректно закрыть в конце
        user_data_dir = None
        
        if EXTENSION_DIR and os.path.isdir(EXTENSION_DIR):
            # Расширения требуют headful + persistent context
            user_data_dir = tempfile.mkdtemp(prefix="pw-ext-")
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                args=[
                    f"--disable-extensions-except={EXTENSION_DIR}",
                    f"--load-extension={EXTENSION_DIR}",
                    *common_args,
                ],
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
                permissions=["clipboard-read", "clipboard-write"],
            )
        else:
            # Без расширения — обычный headless браузер
            browser = await p.chromium.launch(headless=True, args=common_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
                permissions=["clipboard-read", "clipboard-write"],
            )
        # <<< КОНЕЦ ДОБАВЛЕНИЯ >>>
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
            html_content = await page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
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

        # --- подтянуть текст статей с источников ---
        await enrich_with_source_text(context, items)

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

        # Закрываем только после завершения всех задач.
        await context.close()
        if browser:
            await browser.close()
        if user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    print("Saved demo → out/demo.json (+ html/png).")

if __name__ == "__main__":
    asyncio.run(run())
