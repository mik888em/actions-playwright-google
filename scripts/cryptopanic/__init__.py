"""Пакет утилит для сбора и обработки новостей CryptoPanic."""
from .cleaning import clean_text_pipeline
from .extractor import EXTRACT_JS
from .integrations import GAS_WEBHOOK_URL, post_to_gas, post_to_gas_async
from .network import (
    ACCEPT_LANGUAGE,
    CLICK_CONCURRENCY,
    MOBILE_VIEWPORT,
    TEXT_EXTRA_WAIT_MS,
    TEXT_GOTO_TIMEOUT,
    TEXT_GLOBAL_CONCURRENCY,
    TEXT_PER_DOMAIN,
    USER_AGENT,
    enrich_with_source_text,
    fetch_page_text,
    override_title_meta_from_cp,
    resolve_original_urls,
)
from .scroll import (
    NEWS_ITEM_SELECTOR,
    click_load_more_until_done,
    close_annoyances_in_all_frames,
    ensure_progress_or_reload,
    maybe_accept_cookies,
    pick_scroll_container,
    scroll_once,
    scroll_until_goals,
    wait_loading_spinner_disappear,
)

__all__ = [
    "clean_text_pipeline",
    "EXTRACT_JS",
    "GAS_WEBHOOK_URL",
    "post_to_gas",
    "post_to_gas_async",
    "ACCEPT_LANGUAGE",
    "CLICK_CONCURRENCY",
    "MOBILE_VIEWPORT",
    "TEXT_EXTRA_WAIT_MS",
    "TEXT_GOTO_TIMEOUT",
    "TEXT_GLOBAL_CONCURRENCY",
    "TEXT_PER_DOMAIN",
    "USER_AGENT",
    "enrich_with_source_text",
    "fetch_page_text",
    "override_title_meta_from_cp",
    "resolve_original_urls",
    "NEWS_ITEM_SELECTOR",
    "click_load_more_until_done",
    "close_annoyances_in_all_frames",
    "ensure_progress_or_reload",
    "maybe_accept_cookies",
    "pick_scroll_container",
    "scroll_once",
    "scroll_until_goals",
    "wait_loading_spinner_disappear",
]
