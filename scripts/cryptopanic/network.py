"""Сетевые операции для сбора и обогащения данных CryptoPanic."""
from __future__ import annotations

import asyncio
import datetime
import html
import os
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from .cleaning import clean_text_pipeline
from .scroll import close_annoyances_in_all_frames

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
MOBILE_VIEWPORT = {"width": 600, "height": 1200}
TEXT_GOTO_TIMEOUT = 45_000
TEXT_EXTRA_WAIT_MS = 1200
TEXT_JITTER_MIN_SEC = 0.6
TEXT_JITTER_MAX_SEC = 1.8
TEXT_GLOBAL_CONCURRENCY = int(os.environ.get("TEXT_GLOBAL_CONCURRENCY", "20"))
TEXT_PER_DOMAIN = int(os.environ.get("TEXT_PER_DOMAIN", "1"))
CLICK_MAX_TRIES = 10
CLICK_SLEEP_MIN_SEC = 2
CLICK_SLEEP_MAX_SEC = 6
CLICK_TIMEOUT_SEC = 30
CLICK_502_MAX_TRIES = 5
CLICK_502_MIN_BASE_SEC = 0.5
CLICK_502_MAX_BASE_SEC = 1.0
CLICK_CONCURRENCY = int(os.environ.get("CLICK_CONCURRENCY", "8"))

USER_AGENTS_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    USER_AGENT,
]
IMG_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.avif']


def parse_retry_after(value: str | None) -> float | None:
    """Распарсить заголовок Retry-After в секунды ожидания."""

    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        try:
            return float(value)
        except Exception:
            return None
    try:
        dt = parsedate_to_datetime(value)
        delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def extract_url_only(raw: str | None) -> str | None:
    """Выделить URL из служебных строк вида HTTP_404:https://..."""

    if not raw:
        return None
    match = re.search(r"(https?://[^\s]+)", raw, flags=re.I)
    return match.group(1) if match else None


def is_cryptopanic(url: str) -> bool:
    """Проверить принадлежность URL домену CryptoPanic."""

    try:
        dom = urlparse(url).netloc.lower()
        return dom.endswith("cryptopanic.com")
    except Exception:
        return False


def _sanitize_image_url(raw: str) -> str:
    """Очистить значение meta og:image до валидного URL."""

    if not raw:
        return "none"
    value = raw.strip()
    match = re.search(r'[\s\S]+og:image[\s\S]+(http[\s\S]+)"', value, flags=re.I)
    if match:
        value = match.group(1)
    if not (value.startswith("http://") or value.startswith("https://")):
        match2 = re.search(r'[\s\S]+og:image[\s\S]{1,20}(http[\s\S]{1,400})"/>', value, flags=re.I)
        if match2:
            value = match2.group(1)
    value = value.replace("@png", "")
    value = html.unescape(value)
    try:
        value = unquote(value)
    except Exception:
        pass
    low = value.lower()
    for ext in IMG_EXTS:
        pos = low.find(ext)
        if pos != -1:
            next_ch = value[pos + len(ext):pos + len(ext) + 1]
            if next_ch != '/':
                value = value[:pos + len(ext)]
                break
    if not (value.startswith("http://") or value.startswith("https://")):
        return "none"
    return value


def _resolve_click_sync(id_news: str) -> str:
    """Синхронно получить целевой URL для новости через /news/click/."""

    click_url = f"https://cryptopanic.com/news/click/{id_news}/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Referer": "https://cryptopanic.com/",
    }

    last_exc: Exception | None = None
    last_status: int | None = None
    tries_502 = 0
    total_tries = max(CLICK_MAX_TRIES, CLICK_502_MAX_TRIES)

    for _ in range(1, total_tries + 1):
        try:
            response = requests.get(click_url, headers=headers, allow_redirects=True, timeout=CLICK_TIMEOUT_SEC)
            final_url = str(response.url) if getattr(response, "url", None) else ""
            status = response.status_code

            if status == 429:
                last_status = 429
                retry_after_hdr = response.headers.get("Retry-After")
                sleep_s = parse_retry_after(retry_after_hdr)
                if sleep_s is None:
                    sleep_s = random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC)
                time.sleep(sleep_s)
                continue

            if status == 502:
                tries_502 += 1
                if tries_502 >= CLICK_502_MAX_TRIES:
                    return f"HTTP_502:{final_url or click_url}"
                low = CLICK_502_MIN_BASE_SEC * (2 ** (tries_502 - 1))
                high = CLICK_502_MAX_BASE_SEC * (2 ** (tries_502 - 1))
                time.sleep(random.uniform(low, high))
                continue

            if status == 200:
                return final_url or f"HTTP_{status}:{click_url}"

            return f"HTTP_{status}:{final_url or click_url}"

        except Exception as exc:
            last_exc = exc
            time.sleep(random.uniform(CLICK_SLEEP_MIN_SEC, CLICK_SLEEP_MAX_SEC))

    if tries_502 > 0:
        return f"HTTP_502:{click_url}"
    if last_status == 429:
        return f"HTTP_429:{click_url}"
    return f"ERROR:{repr(last_exc) if last_exc else 'unknown'}"


async def resolve_original_urls(items: list[dict], concurrency: int) -> None:
    """Асинхронно дополнить элементы итоговым original_url через клики."""

    sem = asyncio.Semaphore(concurrency)

    async def one(item: dict) -> None:
        id_news = item.get("id_news", "")
        if not id_news:
            item["original_url"] = ""
            return
        async with sem:
            result = await asyncio.to_thread(_resolve_click_sync, id_news)
            item["original_url"] = result

    await asyncio.gather(*(one(it) for it in items))


async def fetch_page_text(context, url: str) -> dict[str, Any]:
    """Получить текст, изображение и заголовок источника новости."""

    page = await context.new_page()
    try:
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

        title_meta = "none"
        mt = meta_title or ""
        match1 = re.search(r'[\s\S]+og:title[\s\S]+(content="[\s\S]+)"', mt, flags=re.I)
        if match1:
            chunk = match1.group(1)
            match2 = re.search(r'content="([\s\S]+)', chunk, flags=re.I)
            if match2:
                title_meta = (match2.group(1) or "").strip('" ')
        if not title_meta or title_meta.strip() == "":
            match3 = re.search(r'<meta\s+content="([^"]{1,500})"[\s\S]+', mt, flags=re.I)
            if match3:
                title_meta = match3.group(1)
        if not title_meta:
            title_meta = "none"

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

        async def read_text_fallback() -> str:
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

        raw_text = await read_text_fallback()
        normalized = re.sub(r"\r\n?", "\n", raw_text).strip()

        if len(normalized) < 400 and "Verifying you are human" in normalized:
            await page.wait_for_timeout(random.randint(15_000, 20_000))
            raw_text = await read_text_fallback()
            normalized = re.sub(r"\r\n?", "\n", raw_text).strip()
            if len(normalized) < 400 and "Verifying you are human" in normalized:
                return {"text": "---", "image_link": image_link, "title_meta": title_meta}

        cleaned = clean_text_pipeline(normalized)
        return {"text": cleaned or "---", "image_link": image_link, "title_meta": title_meta}
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def enrich_with_source_text(context, items: list[dict]) -> None:
    """Обогатить элементы текстом и медиаданными источников."""

    for item in items:
        item["text_of_site"] = "---"
        item["image_link"] = "none"
        item["title_meta"] = "none"

    tasks = []
    url_cache: dict[str, dict[str, Any]] = {}

    urls: list[tuple[dict, str]] = []
    for item in items:
        raw = item.get("original_url") or ""
        url = extract_url_only(raw)
        if not url:
            continue
        if is_cryptopanic(url):
            continue
        urls.append((item, url))

    unique_domains = set()
    for _, url in urls:
        try:
            unique_domains.add(urlparse(url).netloc.lower())
        except Exception:
            pass

    domain_sems = {domain: asyncio.Semaphore(TEXT_PER_DOMAIN) for domain in unique_domains}
    global_sem = asyncio.Semaphore(max(1, min(TEXT_GLOBAL_CONCURRENCY, len(unique_domains) or 1)))

    async def one(item: dict, url: str) -> None:
        try:
            if url in url_cache:
                data = url_cache[url]
                item["text_of_site"] = data.get("text", "---")
                item["image_link"] = data.get("image_link", "none")
                item["title_meta"] = data.get("title_meta", "none")
                return

            try:
                domain = urlparse(url).netloc.lower()
            except Exception:
                return

            await asyncio.sleep(random.uniform(TEXT_JITTER_MIN_SEC, TEXT_JITTER_MAX_SEC))
            async with global_sem:
                async with domain_sems.get(domain, asyncio.Semaphore(1)):
                    data = await fetch_page_text(context, url)
                    url_cache[url] = data or {}
                    if data:
                        item["text_of_site"] = data.get("text", "---")
                        item["image_link"] = data.get("image_link", "none")
                        item["title_meta"] = data.get("title_meta", "none")
        except Exception:
            return

    for item, url in urls:
        tasks.append(asyncio.create_task(one(item, url)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def override_title_meta_from_cp(items: list[dict], concurrency: int = 20, timeout_sec: int = 20) -> None:
    """Перезаписать title_meta описанием с CryptoPanic."""

    sem = asyncio.Semaphore(concurrency)

    def fetch_cp_html(url_abs: str) -> str | None:
        if not url_abs:
            return None
        headers = {
            "User-Agent": random.choice(USER_AGENTS_POOL),
            "Accept-Language": ACCEPT_LANGUAGE,
            "Referer": "https://cryptopanic.com/",
        }
        try:
            response = requests.get(url_abs, headers=headers, timeout=timeout_sec)
            if response.status_code == 200 and response.text:
                return response.text
        except Exception:
            pass
        return None

    async def one(item: dict) -> None:
        url_abs = item.get("url_abs") or ""
        title = (item.get("title") or "").strip()
        async with sem:
            html_text = await asyncio.to_thread(fetch_cp_html, url_abs)
        desc = _parse_meta_description(html_text or "")
        if desc:
            item["title_meta"] = desc
        else:
            item["title_meta"] = title if len(title) >= 15 else "ni4ego_ne_ydalos_ni_otkuda_izvle4"

    await asyncio.gather(*(one(it) for it in items))


def _parse_meta_description(html_text: str) -> str:
    if not html_text:
        return ""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{5,800})["\']',
        html_text,
        flags=re.I,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{5,800})["\']',
            html_text,
            flags=re.I,
        )
    if not match:
        return ""
    desc = match.group(1)
    desc = html.unescape(desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc


__all__ = [
    "USER_AGENT",
    "ACCEPT_LANGUAGE",
    "MOBILE_VIEWPORT",
    "TEXT_GOTO_TIMEOUT",
    "TEXT_EXTRA_WAIT_MS",
    "TEXT_GLOBAL_CONCURRENCY",
    "TEXT_PER_DOMAIN",
    "CLICK_CONCURRENCY",
    "resolve_original_urls",
    "fetch_page_text",
    "enrich_with_source_text",
    "override_title_meta_from_cp",
]
