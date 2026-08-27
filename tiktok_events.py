"""
tiktok_events.py
Отправка Purchase-события в TikTok Events API при продаже в AmoCRM.

Простая версия: список жёстко заданных пикселей (каждый со своим токеном
и привязкой к своему Business Center — для ясности, не для маршрутизации).
При каждой продаже событие отправляется во ВСЕ пиксели из списка,
независимо от BC. Никакого поиска по баерам/кампаниям.

Матчинг пользователя, в порядке приоритета:
  1. ttclid — если клиент кликнул по рекламе и перешёл на сайт
  2. хешированный телефон (Advanced Matching)
  3. IP-адрес / User-Agent — дополнительные сигналы
"""

import time
import hashlib
import logging
from typing import Optional

import httpx

log = logging.getLogger("tiktok_events")

TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_TEST_EVENT_CODE = ""  # оставить пустым для боевого режима

# Пиксели, сгруппированные по Business Center, для наглядности.
# Каждая запись: (pixel_id, access_token, короткое имя для логов)
PIXELS_BY_BC = {
    "7410341052470607888": [  # ООО "Артагед"
        ("D0SQP1RC77UEHH7PROEG", "2ae8954093162bbb7b843b613f948ebc53073f17", "electrovelo_obshy"),
    ],
    "7632400042631888913": [  # TechnoWave_shop
        ("D7PKT3BC77UDOFSGA31G", "52c18f6d246ee85cfff98dca449bed26837f2a10", "vld_obshy"),
    ],
}

# Плоский список для отправки — собирается из PIXELS_BY_BC.
PIXELS = [
    (pixel_id, token, name, bc_id)
    for bc_id, pixels in PIXELS_BY_BC.items()
    for pixel_id, token, name in pixels
]


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
    Отправляет событие CompletePayment во все настроенные пиксели (PIXELS),
    из обоих Business Center сразу. Параметр buyer принимается для
    совместимости с существующим вызовом, но не используется —
    маршрутизация по баеру отключена.
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

    async with httpx.AsyncClient(timeout=10) as client:
        for pixel_id, token, name, bc_id in PIXELS:
            payload = {
                "pixel_code": pixel_id,
                "event_source": "web",
                "event_source_id": pixel_id,
                "data": [event_data],
            }
            if TIKTOK_TEST_EVENT_CODE:
                payload["test_event_code"] = TIKTOK_TEST_EVENT_CODE

            headers = {"Access-Token": token, "Content-Type": "application/json"}
            try:
                r = await client.post(f"{TIKTOK_API_BASE}/event/track/", headers=headers, json=payload)
                log.info(
                    "TikTok event sent for lead %s (bc=%s, pixel=%s/%s, match=%s): %s %s",
                    lead_id, bc_id, name, pixel_id, "+".join(match_signals), r.status_code, r.text[:300],
                )
            except Exception as e:
                log.error("TikTok event failed for lead %s (bc=%s, pixel=%s): %s", lead_id, bc_id, name, e)
