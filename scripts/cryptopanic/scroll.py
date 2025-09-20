"""Утилиты прокрутки страницы CryptoPanic и выбор контейнеров."""
from __future__ import annotations

import random
import time
from typing import Any, Optional, Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

NEWS_ITEM_SELECTOR = "div.news-row.news-row-link"
CONTAINER_CANDIDATES = [
    "div.news-container.ps",
    "div.news-container",
    "div[class*='news-container']",
]

LOAD_MORE_ROOT = "div.news-load-more"
LOAD_MORE_BTN = f"{LOAD_MORE_ROOT} button:has-text('Load more')"
LOADING_SPAN = f"{LOAD_MORE_ROOT} span:has-text('Loading...')"

SCROLL_PAUSE_MS = 350
SCROLL_MAX_STEPS = 900
STALL_LIMIT = 15


async def maybe_accept_cookies(page: Page) -> bool:
    """Best-effort нажатие на типичные кнопки согласия с куки."""

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
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=1200)
            await page.wait_for_timeout(250)
            return True
        except Exception:
            continue
    return False


async def close_annoyances_in_all_frames(page: Page) -> None:
    """Закрыть всплывающие окна и куки во всех фреймах."""

    await maybe_accept_cookies(page)
    try:
        await page.locator('span[aria-hidden="true"]:has-text("×")').first.click(timeout=800)
    except Exception:
        pass
    for frame in page.frames:
        try:
            await frame.locator('button:has-text("Accept")').first.click(timeout=600)
        except Exception:
            pass
        try:
            await frame.locator('span[aria-hidden="true"]:has-text("×")').first.click(timeout=600)
        except Exception:
            pass


async def pick_scroll_container(page: Page) -> Tuple[Optional[str], Optional[Any]]:
    """Вернуть селектор и локатор основного контейнера новостей."""

    for selector in CONTAINER_CANDIDATES:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0:
                return selector, locator
        except Exception:
            continue
    return None, None


async def wait_loading_spinner_disappear(page: Page) -> bool:
    """Ожидать исчезновения индикатора загрузки блока новостей."""

    root = page.locator(LOADING_SPAN)
    try:
        if await root.count() == 0:
            return True
        await root.wait_for(state="hidden", timeout=10_000)
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return True


async def click_load_more_until_done(page: Page) -> bool:
    """Кликать по кнопке Load more, пока она не исчезнет."""

    started = time.monotonic()
    while True:
        button = page.locator(LOAD_MORE_BTN)
        try:
            visible = await button.is_visible()
        except Exception:
            visible = False
        if not visible:
            return True
        try:
            await button.click(timeout=1500)
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


async def scroll_once(page: Page, container_locator) -> None:
    """Прокрутить контейнер новостей до низа один раз."""

    try:
        if container_locator:
            await container_locator.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        else:
            await page.evaluate(
                "window.scrollTo(0, document.scrollingElement ? document.scrollingElement.scrollHeight : document.body.scrollHeight)"
            )
    except Exception:
        try:
            await page.mouse.wheel(0, 2000)
        except Exception:
            pass


async def ensure_progress_or_reload(page: Page) -> bool:
    """Проверить прогресс загрузки списка новостей."""

    ok = await wait_loading_spinner_disappear(page)
    if not ok:
        return False
    ok = await click_load_more_until_done(page)
    if not ok:
        return False
    return True


async def scroll_until_goals(
    page: Page,
    item_selector: str,
    min_items: int,
    min_steps: int,
    container_locator,
) -> dict:
    """Прокручивать страницу до достижения заданных критериев."""

    steps = 0
    stalled = 0
    try:
        last_count = await page.locator(item_selector).count()
    except Exception:
        last_count = 0
    while steps < SCROLL_MAX_STEPS:
        if last_count >= min_items and steps >= min_steps:
            break
        await scroll_once(page, container_locator)
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
            current = await page.locator(item_selector).count()
        except Exception:
            current = last_count
        if current <= last_count:
            stalled += 1
        else:
            stalled = 0
            last_count = current
        if stalled >= STALL_LIMIT:
            break
    return {
        "final_count": last_count,
        "steps": steps,
        "stalled_iterations": stalled,
        "reached_goal": (last_count >= min_items and steps >= min_steps),
        "reload_required": False,
    }


__all__ = [
    "NEWS_ITEM_SELECTOR",
    "maybe_accept_cookies",
    "close_annoyances_in_all_frames",
    "pick_scroll_container",
    "wait_loading_spinner_disappear",
    "click_load_more_until_done",
    "scroll_once",
    "ensure_progress_or_reload",
    "scroll_until_goals",
]
