"""Интеграции с внешними сервисами (например, Google Apps Script)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import requests

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL", "").strip()
GAS_PASSWORD = os.environ.get("GAS_PASSWORD", "").strip()


def post_to_gas(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Отправить подготовленные новости в GAS вебхук."""

    if not GAS_WEBHOOK_URL or not GAS_PASSWORD:
        return {"ok": False, "skipped": True, "reason": "no webhook/password in env"}

    payload = {"password": GAS_PASSWORD, "items": items}
    try:
        response = requests.post(
            GAS_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=60,
        )
        return {
            "ok": (response.status_code == 200),
            "status": response.status_code,
            "preview": (response.text or "")[:200],
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


async def post_to_gas_async(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Асинхронная обёртка над post_to_gas для удобства пайплайна."""

    return await asyncio.to_thread(post_to_gas, items)


__all__ = ["GAS_WEBHOOK_URL", "post_to_gas", "post_to_gas_async"]
