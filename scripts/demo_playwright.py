# -*- coding: utf-8 -*-
"""
CryptoPanic demo with Playwright + enrich:
- time_iso (UTC) normalize
- resolve original_url with retries on 429
- clean "HTTP_XXX:" when domain matches source
- filter banned domains
- dedupe by id_news
- pull source text with mobile viewport + uBlock Lite + deep cleaning (9 steps)
- extract og:image -> image_link; og:title fallback
"""
import os, re, json, datetime, asyncio, random, time, email.utils, tempfile, shutil, html
from urllib.parse import urlparse, unquote
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
# основной контекст для списка новостей
VIEWPORT = {"width": 1280, "height": 800}
# мобильная ориентация для чтения источников
MOBILE_VIEWPORT = {"width": 600, "height": 1200}

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

# --- 502 backoff tuning ---
CLICK_502_MAX_TRIES = 5             # максимум попыток именно для 502
CLICK_502_MIN_BASE_SEC = 0.5        # первая пауза: 0.5–1.0 c
CLICK_502_MAX_BASE_SEC = 1.0

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

# ---------------- text helpers (BAS → Python) ----------------
PUNCTS = ['.', ',', '?', '!', '"', ':', "\u201D", "\u201C", "\u201D", "\u201C", "\u201C"]
SYMBOLS_FOR_STRIP = ['!', ':', '?', ',', '.', '"', '”', '“', '”', '“', '“']
BLACK_LIST = [
    'our website','read more:','can send him leads at','@theblockcrypto.com','follow him on twitter',
    'read more','about author','read more:','read more','image:','disclosure:','redeem now!','redeem now',
    'follow our','our twitter','thanks for reading','please','your inbox','subscribe','first name','select your',
    'topics','share article','disclaimer','image credit','related posts','sign up','sign in','email address',
    'your email','exclusive offers','newsletter','you may also like','banner','related news','privacy policy',
    'terms of services','twitter.com/','advertisement','you agree','subscribing','newsletter','related:',
    'in this article','also like:','by the author','©','about us','advertise','terms and conditions','write for us',
    'pixabay','informational purposes only','your deposit','this link','to register','code to receive','sponsored',
    'special offer','submit a press release','shutterstock','http','featured image','not investment advice',
    'join now','newsletters','subscribe to','(@','my website','follow me','related image','news writer','his articles',
    'journalist','about the author','article image','the link','contact:','from author','related articles',
    'from author','gamza','гамза','университет','закончил','окончил',
]

def _last_meaningful_char(s: str) -> str:
    if not s: return ''
    i = len(s) - 1
    while i >= 0 and s[i] in (' ', '\t', '\r', '\n'):
        i -= 1
    skip = "\"'”’»)]}"
    while i >= 0 and s[i] in skip:
        i -= 1
    if i >= 0 and s[i] == ',':
        i -= 1
        while i >= 0 and s[i] in skip:
            i -= 1
    return s[i] if i >= 0 else ''

def _ends_with_punct(line: str) -> bool:
    return _last_meaningful_char(line) in PUNCTS

def _normalize_space_line(s: str) -> str:
    if s is None: return ""
    s = s.replace('\t', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()

def clean_text_pipeline(raw: str) -> str:
    """Реализация ваших 1–9 шагов очистки."""
    if not raw:
        return ""

    # предварительно: в «реальные» переносы, разбиваем на строки и выбрасываем «совсем пустые»
    txt = re.sub(r"\r\n?", "\n", raw)
    lines = [l for l in txt.split("\n")]

    # удаляем все «совсем пустые» строки
    lines = [l for l in lines if len(l)]

    # шаг 1: найти серию из 3 строк, заканчивающихся на пунктуацию (с оговоркой для :)
    def is_sentence_like(line: str, is_last: bool) -> bool:
        last = _last_meaningful_char(line)
        if last == ':':
            return not is_last
        return last in ('.', '!', '?') and len(line) >= 40 and (line.count(' ') >= 4)

    start_idx = -1
    for i in range(len(lines)):
        ok = True
        for k in range(3):
            j = i + k
            if j >= len(lines) or not is_sentence_like(lines[j], j == len(lines)-1):
                ok = False
                break
        if ok:
            start_idx = i
            break
    if start_idx == -1:
        for i in range(len(lines)):
            if is_sentence_like(lines[i], i == len(lines)-1):
                start_idx = i
                break
    if start_idx > 0:
        lines = lines[start_idx:]

    # шаг 2: если строка содержит слово из blacklist — вычищаем из неё SYMBOLS_FOR_STRIP
    low_bl = [w.lower() for w in BLACK_LIST]
    def strip_if_black(s: str) -> str:
        low = s.lower()
        if any(w in low for w in low_bl):
            for sym in SYMBOLS_FOR_STRIP:
                s = s.replace(sym, '')
        return s
    lines = [strip_if_black(x) for x in lines]

    # шаг 3: если внизу серия из 6 строк без «хорошего конца» — обрезаем до начала серии
    N_PODRYAD = 6
    cnt = 0
    start_run = -1
    for i, line in enumerate(lines):
        if _ends_with_punct(line):
            cnt = 0
        else:
            cnt += 1
            if cnt >= N_PODRYAD:
                start_run = i + 1 - N_PODRYAD
                break
    if start_run >= 0:
        lines = lines[:start_run]

    # шаг 4: максимум 25 строк
    if len(lines) > 25:
        lines = lines[:25]

    # шаг 5: удалить все строки, которые НЕ оканчиваются на разрешённую пунктуацию (кроме строго пустых "")
    i = 0
    while i < len(lines):
        if lines[i] == "":
            i += 1
            continue
        last = _last_meaningful_char(lines[i])
        if last in PUNCTS:
            i += 1
        else:
            del lines[i]
    # схлопываем подряд идущие пустые строки
    normalized = []
    prev_empty = False
    for it in lines:
        if it == "":
            if not prev_empty:
                normalized.append(it)
            prev_empty = True
        else:
            normalized.append(it)
            prev_empty = False
    lines = normalized

    # шаг 6: удаляем строки, содержащие слова из blacklist (регистронезависимо)
    def not_black(line: str) -> bool:
        low = line.lower()
        return all(w not in low for w in low_bl)
    lines = [l for l in lines if not_black(l)]

    # шаг 7: удалить последнюю строку, если оканчивается на :
    if lines and _last_meaningful_char(lines[-1]) == ':':
        lines.pop()

    # шаг 8: удалить 2 последних, если их длина < 60 (последнюю пустую — удаляем, предпоследнюю пустую — оставляем)
    def short(s): return len(s.strip()) < 60
    if lines:
        if lines[-1].strip() == "" or short(lines[-1]):
            lines.pop()
    if len(lines) >= 2:
        penult = lines[-1] if not lines else None  # уже смещено
        # реальная предпоследняя теперь:
        # (мы уже удалили последнюю при необходимости)
        if len(lines) >= 2:
            if lines[-2].strip() != "" and short(lines[-2]):
                del lines[-2]

    # шаг 9: убрать дубли (кроме пустых — их не трогаем)
    seen = set()
    kept = []
    for idx, s in enumerate(lines):
        if s.strip() == "":
            kept.append(s)
            continue
        key = s.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(s)
    lines = kept

    # финальные косметические
    lines = [_normalize_space_line(l) for l in lines]
    # собираем с РЕАЛЬНЫМИ переносами
    return "\n".join(lines).strip()

# ---------------- page helpers ----------------
async def maybe_accept_cookies(page) -> bool:
    selectors = [
        'button:has-text("Accept")',
        'a.btn.btn-outline-primary:has-text("Accept")',
        'a:has-text("Accept")',
        'button:has-text("Accept All")',
        'text=Accept All',
        'button:has-text("I agree")',
        'button:has-text("Allow all")',
        'a:has-text("Принять")',
        'button:has-text("Принять")',
        'text=Consent',
    ]
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1200)
            await page.wait_for_timeout(250)
            return True
        except Exception:
            pass
    return False

async def close_annoyances_in_all_frames(page):
    # пробуем в главной странице
    await maybe_accept_cookies(page)
    # пробуем закрыть крестики «×»
    try:
        await page.locator('span[aria-hidden="true"]:has-text("×")').first.click(timeout=800)
    except Exception:
        pass
    # пробуем во фреймах (в т.ч. mailerlite)
    for fr in page.frames:
        try:
            await fr.locator('button:has-text("Accept")').first.click(timeout=600)
        except Exception:
            pass
        try:
            await fr.locator('span[aria-hidden="true"]:has-text("×")').first.click(timeout=600)
        except Exception:
            pass

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

# -------------- uBlock Lite: попытка включить optimal ----------------
async def try_enable_ubol_optimal(context):
    """Best-effort: открыть dashboard uBOL и включить Filter lists → optimal (до 3 попыток)."""
    # найдём любой chrome-extension://* из уже открытых
    ext_id = None
    for p in context.pages:
        if p.url.startswith("chrome-extension://"):
            try:
                ext_id = p.url.split("/")[2]
                break
            except Exception:
                pass
    # если не нашли — попробуем «угадать» по известным страницам расширения
    candidates = []
    if ext_id:
        candidates.append(f"chrome-extension://{ext_id}/dashboard.html")
    # пробуем открыть dashboard и ткнуть «Filter lists» → «optimal»
    for attempt in range(3):
        try:
            page = await context.new_page()
            opened = False
            for u in (candidates or []):
                try:
                    await page.goto(u, timeout=8000)
                    opened = True
                    break
                except Exception:
                    pass
            if not opened:
                # fallback: попробуем открыть любой extension-пэйдж из уже имеющихся
                for p in context.pages:
                    if p.url.startswith("chrome-extension://"):
                        try:
                            await page.goto(p.url, timeout=8000)
                            opened = True
                            break
                        except Exception:
                            pass
            if not opened:
                await page.close()
                await asyncio.sleep(0.8)
                continue

            # кликаем Filter lists → optimal
            clicked = False
            try:
                await page.locator("a:has-text('Filter lists')").first.click(timeout=2000)
                await page.wait_for_timeout(500)
                await page.locator("text=optimal").first.click(timeout=2000)
                clicked = True
            except Exception:
                pass
            await page.close()
            if clicked:
                return True
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.7, 1.6))
    return False

# ----------- image/title helpers -----------
IMG_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.avif']

def _sanitize_image_url(raw: str) -> str:
    if not raw:
        return "none"
    s = raw.strip()
    # основная попытка
    m = re.search(r'[\s\S]+og:image[\s\S]+(http[\s\S]+)"', s, flags=re.I)
    if m:
        s = m.group(1)
    # если не начинается с http — альтернативная
    if not (s.startswith("http://") or s.startswith("https://")):
        m2 = re.search(r'[\s\S]+og:image[\s\S]{1,20}(http[\s\S]{1,400})"/>', s, flags=re.I)
        if m2:
            s = m2.group(1)
    # нормализации
    s = s.replace("@png", "")  # ваш кейс
    s = html.unescape(s)
    try:
        s = unquote(s)
    except Exception:
        pass
    low = s.lower()
    # отрезаем хвост после расширения, если после него нет '/'
    for ext in IMG_EXTS:
        pos = low.find(ext)
        if pos != -1:
            next_ch = s[pos + len(ext):pos + len(ext) + 1]
            if next_ch != '/':
                s = s[:pos + len(ext)]
                break
    if not (s.startswith("http://") or s.startswith("https://")):
        return "none"
    return s

# ----------- source text fetcher -----------
async def fetch_page_text(context, url: str):
    """
    Открыть url:
    - перевести страницу в 600x1200
    - закрыть куки/оверлеи
    - достать og:image/og:title
    - попытаться скопировать текст через буфер, fallback: innerText
    - проверить Cloudflare "Verifying you are human..."
    - прогнать через clean_text_pipeline
    Возвращает dict: {"text": "...", "image_link": "...", "title_meta": "..."}
    """
    page = await context.new_page()
    try:
        # мобильное окно перед заходом
        try:
            await page.set_viewport_size(MOBILE_VIEWPORT)
        except Exception:
            pass

        await page.goto(url, wait_until="domcontentloaded", timeout=TEXT_GOTO_TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        await close_annoyances_in_all_frames(page)
        await page.wait_for_timeout(TEXT_EXTRA_WAIT_MS)

        # meta og:image / og:title
        try:
            meta_image = await page.evaluate("""() => {
                const m = document.querySelector('meta[property="og:image"]');
                if (!m) return '';
                return m.outerHTML || m.getAttribute('content') || '';
            }""")
        except Exception:
            meta_image = ""
        try:
            meta_title = await page.evaluate("""() => {
                const m = document.querySelector('meta[property="og:title"]');
                if (!m) return '';
                return m.outerHTML || m.getAttribute('content') || '';
            }""")
        except Exception:
            meta_title = ""

        image_link = _sanitize_image_url(meta_image or "")

        # запасной разбор title
        title_meta = "none"
        mt = meta_title or ""
        m1 = re.search(r'[\s\S]+og:title[\s\S]+(content="[\s\S]+)"', mt, flags=re.I)
        if m1:
            chunk = m1.group(1)
            m2 = re.search(r'content="([\s\S]+)', chunk, flags=re.I)
            if m2:
                title_meta = (m2.group(1) or "").strip('" ')
        if not title_meta or title_meta.strip() == "":
            m3 = re.search(r'<meta\s+content="([^"]{1,500})"[\s\S]+', mt, flags=re.I)
            if m3:
                title_meta = m3.group(1)
        if not title_meta:
            title_meta = "none"

        # Попытка Select All + copy
        try:
            await page.locator("body").click(timeout=1500)
        except Exception:
            pass
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
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Control+C")
        except Exception:
            pass

        async def read_text_fallback():
            clip = None
            try:
                clip = await page.evaluate(
                    "async () => { try { return await navigator.clipboard.readText(); } catch(e) { return null } }"
                )
            except Exception:
                clip = None
            if not clip or not clip.strip():
                try:
                    clip = await page.evaluate(
                        "(() => { const el = document.body || document.documentElement; return el ? (el.innerText || el.textContent || '') : ''; })()"
                    )
                except Exception:
                    clip = None
            return clip or ""

        raw = await read_text_fallback()
        norm = re.sub(r"\r\n?", "\n", raw).strip()

        # проверка на Cloudflare
        if len(norm) < 400 and "Verifying you are human" in norm:
            await page.wait_for_timeout(random.randint(15000, 20000))
            raw = await read_text_fallback()
            norm = re.sub(r"\r\n?", "\n", raw).strip()
            if len(norm) < 400 and "Verifying you are human" in norm:
                return {"text": "---", "image_link": image_link, "title_meta": title_meta}

        cleaned = clean_text_pipeline(norm)
        return {"text": cleaned or "---", "image_link": image_link, "title_meta": title_meta}
    finally:
        try:
            await page.close()
        except Exception:
            pass

async def enrich_with_source_text(context, items: list):
    """
    Добавляет:
      - item['text_of_site']
      - item['image_link']
      - item['title_meta'] (og:title fallback)
    Параллелим по доменам.
    """
    for it in items:
        it["text_of_site"] = "---"
        it["image_link"] = "none"
        it["title_meta"] = "none"

    tasks = []
    url_cache: dict[str, dict] = {}

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

    domain_sems = {dom: asyncio.Semaphore(TEXT_PER_DOMAIN) for dom in unique_domains}
    global_sem = asyncio.Semaphore(max(1, min(TEXT_GLOBAL_CONCURRENCY, len(unique_domains) or 1)))

    async def one(it, url):
        try:
            if url in url_cache:
                d = url_cache[url]
                it["text_of_site"] = d.get("text", "---")
                it["image_link"] = d.get("image_link", "none")
                it["title_meta"] = d.get("title_meta", "none")
                return

            try:
                dom = urlparse(url).netloc.lower()
            except Exception:
                return

            await asyncio.sleep(random.uniform(TEXT_JITTER_MIN_SEC, TEXT_JITTER_MAX_SEC))
            async with global_sem:
                async with domain_sems.get(dom, asyncio.Semaphore(1)):
                    d = await fetch_page_text(context, url)
                    url_cache[url] = d or {}
                    if d:
                        it["text_of_site"] = d.get("text", "---")
                        it["image_link"] = d.get("image_link", "none")
                        it["title_meta"] = d.get("title_meta", "none")
        except Exception:
            return

    for it, url in urls:
        tasks.append(asyncio.create_task(one(it, url)))

    if tasks:
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
        EXTENSION_DIR = os.environ.get("EXTENSION_DIR")
        common_args = ["--no-sandbox", "--disable-dev-shm-usage"]

        browser = None
        user_data_dir = None

        if EXTENSION_DIR and os.path.isdir(EXTENSION_DIR):
            user_data_dir = tempfile.mkdtemp(prefix="pw-ext-")
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,  # нужно для расширений
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
            # best-effort включить фильтры uBOL
            try:
                await try_enable_ubol_optimal(context)
            except Exception:
                pass
        else:
            browser = await p.chromium.launch(headless=True, args=common_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
                permissions=["clipboard-read", "clipboard-write"],
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

        # подтянем original_url
        await resolve_original_urls(items, concurrency=CLICK_CONCURRENCY)

        # пост-обработка original_url против source
        clean_original_vs_source(items)

        # фильтрация по нежелательным доменам
        items = filter_banned(items)

        # финальная дедупликация
        items = dedupe_by_id(items)

        # --- текст/картинка/тайтл из источников ---
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

        await context.close()
        if browser:
            await browser.close()
        if user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    print("Saved demo → out/demo.json (+ html/png).")

if __name__ == "__main__":
    asyncio.run(run())
