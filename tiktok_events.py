"""
tiktok_events.py
Отправка Purchase-события в TikTok Events API при продаже в AmoCRM.

Максимально простая версия: один жёстко заданный пиксель, один токен.
Никакого поиска по баерам/кампаниям — просто шлём событие в этот пиксель
при каждой продаже.

Матчинг пользователя, в порядке приоритета:
  1. ttclid — если клиент кликнул по рекламе и перешёл на сайт
  2. хешированный телефон (Advanced Matching)
  3. IP-адрес / User-Agent — дополнительные сигналы
"""

import os
import time
import hashlib
import logging
from typing import Optional

import httpx

log = logging.getLogger("tiktok_events")

TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_TEST_EVENT_CODE = os.getenv("TIKTOK_TEST_EVENT_CODE", "")

# Единственный пиксель, в который слётся всё для этого BC.
PIXEL_ID = "D0SQP1RC77UEHH7PROEG"  # "Электровелосипед ОБЩИЙ", BC ООО "Артагед"

TT_HEADERS = {"Access-Token": TIKTOK_ACCESS_TOKEN, "Content-Type": "application/json"}


def _normalize_phone(phone: str) -> Optional[str]:
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    if len(digits) == 9:
        digits = "375" + digits
    elif digits.startswith("80") and len(digits) == 11:
        digits = "375" + digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = "375" + digits[1:]
    if len(digits) < 10:
        return None
    return "+" + digits


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def send_purchase_event(
    lead_id,
    buyer,
    ttclid,
    value,
    phone: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    currency: str = "RUB",
):
    """
    Отправляет событие CompletePayment в единственный настроенный пиксель.
    Параметр buyer принимается для совместимости с существующим вызовом,
    но не используется — маршрутизация по баеру отключена.
    """
    user_data = {}
    match_signals = []

    if ttclid:
        user_data["ttclid"] = ttclid
        match_signals.append("ttclid")

    if phone:
        normalized = _normalize_phone(phone)
        if normalized:
            user_data["phone_number"] = _sha256(normalized)
            match_signals.append("phone")

    if ip:
        user_data["ip"] = ip
        match_signals.append("ip")
    if user_agent:
        user_data["user_agent"] = user_agent

    if not match_signals:
        log.info("TikTok event skipped for lead %s: no ttclid, phone, or ip to match", lead_id)
        return

    if not TIKTOK_ACCESS_TOKEN:
        log.warning("TikTok event skipped: TIKTOK_ACCESS_TOKEN not set")
        return

    event_data = {
        "event": "CompletePayment",
        "event_id": f"amo_{lead_id}",
        "event_time": int(time.time()),
        "user": user_data,
        "properties": {
            "currency": currency,
            "value": str(round(value, 2)) if value else "0",
        },
    }
    payload = {
        "pixel_code": PIXEL_ID,
        "event_source": "web",
        "event_source_id": PIXEL_ID,
        "data": [event_data],
    }
    if TIKTOK_TEST_EVENT_CODE:
        payload["test_event_code"] = TIKTOK_TEST_EVENT_CODE

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{TIKTOK_API_BASE}/event/track/", headers=TT_HEADERS, json=payload)
            log.info(
                "TikTok event sent for lead %s (pixel=%s, match=%s): %s %s",
                lead_id, PIXEL_ID, "+".join(match_signals), r.status_code, r.text[:300],
            )
    except Exception as e:
        log.error("TikTok event failed for lead %s: %s", lead_id, e)
