# -*- coding: utf-8 -*-
"""CryptoPanic demo с Playwright и вынесенными вспомогательными модулями."""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
import re
import shutil
import tempfile
from email import utils as email_utils
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from scripts.cryptopanic import (
    ACCEPT_LANGUAGE,
    CLICK_CONCURRENCY,
    EXTRACT_JS,
    NEWS_ITEM_SELECTOR,
    USER_AGENT,
    enrich_with_source_text,
    maybe_accept_cookies,
    override_title_meta_from_cp,
    pick_scroll_container,
    post_to_gas_async,
    resolve_original_urls,
    scroll_until_goals,
)

OUT_DIR = "out"
URL = os.environ.get("URL") or "https://cryptopanic.com"
LOCALE = "en-US"
TIMEZONE_ID = "Europe/Athens"
VIEWPORT = {"width": 1280, "height": 800}

EXTRA_WAIT_MS = 5000
NEWS_WAIT_TIMEOUT = 20000
SCROLL_TARGET_MIN = 300
RAND_SCROLLS_MIN = 30
RAND_SCROLLS_MAX = 40

BANNED_SUBSTRINGS = ("binance.com", "x.com", "youtube.com")


def utcnow_iso() -> str:
    """Получить текущее время в UTC в ISO-формате."""

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def safe_filename(value: str, max_len: int = 80) -> str:
    """Санитизировать строку для использования в имени файла."""

    sanitized = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._") or "file"
    return sanitized[:max_len]


def normalize_time_iso_py(value: str) -> str:
    """Привести время к формату YYYY-MM-DDTHH:MM:SSZ."""

    if not value:
        return value
    try:
        if value.endswith("Z"):
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.datetime.fromisoformat(value)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        try:
            dt = email_utils.parsedate_to_datetime(value)
            return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return value


def domain_eq(a: str, b: str) -> bool:
    """Сравнить домены без учёта www и регистра."""

    dom_a = (a or "").lower().strip()
    dom_b = (b or "").lower().strip()
    if not dom_a or not dom_b:
        return False
    if dom_a.startswith("www."):
        dom_a = dom_a[4:]
    if dom_b.startswith("www."):
        dom_b = dom_b[4:]
    return dom_a == dom_b


def dedupe_by_id(items: list[dict]) -> list[dict]:
    """Удалить дубликаты по полю id_news, сохраняя первый экземпляр."""

    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        id_news = item.get("id_news", "")
        if id_news and id_news not in seen:
            seen.add(id_news)
            out.append(item)
    return out


def filter_banned(items: list[dict]) -> list[dict]:
    """Отфильтровать новости с нежелательными доменами источника."""

    out: list[dict] = []
    for item in items:
        source = (item.get("source") or "").lower()
        original = (item.get("original_url") or "").lower()
        bad = any(b in source for b in BANNED_SUBSTRINGS) or any(b in original for b in BANNED_SUBSTRINGS)
        if not bad:
            out.append(item)
    return out


def clean_original_vs_source(items: list[dict]) -> None:
    """Снять префикс HTTP_XXX:, если домен совпадает с source."""

    for item in items:
        original = item.get("original_url") or ""
        source = (item.get("source") or "").strip()
        match = re.match(r"^HTTP_\d{3}:(https?://.+)$", original, flags=re.I)
        if not (match and source):
            continue
        url_only = match.group(1)
        domain = urlparse(url_only).netloc
        if domain_eq(domain, source):
            item["original_url"] = url_only


async def try_enable_ubol_optimal(context) -> bool:
    """Активировать фильтры uBlock Origin Lite, если подключено расширение."""

    ext_id = None
    for page in context.pages:
        if page.url.startswith("chrome-extension://"):
            try:
                ext_id = page.url.split("/")[2]
                break
            except Exception:
                pass
    candidates = []
    if ext_id:
        candidates.append(f"chrome-extension://{ext_id}/dashboard.html")
    for _ in range(3):
        try:
            page = await context.new_page()
            opened = False
            for candidate in (candidates or []):
                try:
                    await page.goto(candidate, timeout=8000)
                    opened = True
                    break
                except Exception:
                    pass
            if not opened:
                for existing in context.pages:
                    if existing.url.startswith("chrome-extension://"):
                        try:
                            await page.goto(existing.url, timeout=8000)
                            opened = True
                            break
                        except Exception:
                            pass
            if not opened:
                await page.close()
                await asyncio.sleep(0.8)
                continue
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


async def run() -> None:
    """Запустить полный цикл скрапинга и обогащения."""

    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as playwright:
        extension_dir = os.environ.get("EXTENSION_DIR")
        common_args = ["--no-sandbox", "--disable-dev-shm-usage"]

        browser = None
        user_data_dir = None

        if extension_dir and os.path.isdir(extension_dir):
            user_data_dir = tempfile.mkdtemp(prefix="pw-ext-")
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                args=[
                    f"--disable-extensions-except={extension_dir}",
                    f"--load-extension={extension_dir}",
                    *common_args,
                ],
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=VIEWPORT,
                extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
                permissions=["clipboard-read", "clipboard-write"],
            )
            try:
                await try_enable_ubol_optimal(context)
            except Exception:
                pass
        else:
            browser = await playwright.chromium.launch(headless=True, args=common_args)
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
                break

        host = urlparse(URL).netloc or "demo"
        stem = safe_filename(host)
        html_path = f"{OUT_DIR}/demo_{stem}.html"
        png_path = f"{OUT_DIR}/demo_{stem}.png"
        try:
            html_content = await page.content()
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(html_content)
        except Exception:
            pass
        try:
            await page.screenshot(path=png_path, full_page=True)
        except Exception:
            pass

        try:
            items = await page.evaluate(EXTRACT_JS)
        except Exception:
            items = []

        for item in items:
            item["time_iso"] = normalize_time_iso_py(item.get("time_iso", ""))

        items = dedupe_by_id(items)
        await resolve_original_urls(items, concurrency=CLICK_CONCURRENCY)
        clean_original_vs_source(items)
        items = filter_banned(items)
        items = dedupe_by_id(items)

        await enrich_with_source_text(context, items)
        await override_title_meta_from_cp(items)

        def starts_http(url: str) -> bool:
            return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))

        items = [item for item in items if starts_http(item.get("original_url", ""))]

        seen_ids: set[str] = set()
        deduped: list[dict] = []
        for item in items:
            ident = str(item.get("id_news", "")).strip()
            if not ident or ident in seen_ids:
                continue
            seen_ids.add(ident)
            deduped.append(item)
        items = deduped

        def parse_iso_timestamp(value: str) -> float:
            try:
                if value and value.endswith("Z"):
                    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
            return 0.0

        items.sort(key=lambda item: parse_iso_timestamp(item.get("time_iso", "")))

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
        with open(f"{OUT_DIR}/demo.json", "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)

        try:
            gas_res = await post_to_gas_async(items)
            print("GAS webhook result:", gas_res)
        except Exception as exc:
            print("GAS webhook error:", repr(exc))

        await context.close()
        if browser:
            await browser.close()
        if user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    print("Saved demo → out/demo.json (+ html/png).")


if __name__ == "__main__":
    asyncio.run(run())
