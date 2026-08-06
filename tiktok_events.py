"""
tiktok_events.py
Отправка Purchase-события в TikTok Events API при продаже в AmoCRM.
Пиксель для баера определяется автоматически: сканируем все advertiser-аккаунты
во всех Business Center, смотрим названия кампаний ("Оффер | БАЕР | ссылка"),
для каждого встреченного баера берём pixel_id аккаунта, где он рекламируется.
Кеш обновляется по таймеру, новые аккаунты/баеры подхватываются сами.
"""

import os
import time
import logging
import asyncio
from typing import Optional

import httpx

log = logging.getLogger("tiktok_events")

TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_TEST_EVENT_CODE = os.getenv("TIKTOK_TEST_EVENT_CODE", "")

BUSINESS_CENTER_IDS = [
    "7632400042631888913",
    "7410341052470607888",
]

TT_HEADERS = {"Access-Token": TIKTOK_ACCESS_TOKEN, "Content-Type": "application/json"}
CACHE_TTL_SECONDS = 6 * 60 * 60

_buyer_pixel_cache: dict = {}
_cache_updated_at: float = 0.0
_cache_lock = asyncio.Lock()


def _parse_buyer_from_campaign_name(name: str):
    parts = [p.strip() for p in name.split("|")]
    if len(parts) < 2:
        return None
    return parts[1] or None


async def _get_advertiser_ids(client: httpx.AsyncClient):
    advertiser_ids = []
    for bc_id in BUSINESS_CENTER_IDS:
        page = 1
        while True:
            r = await client.get(
                f"{TIKTOK_API_BASE}/bc/asset/get/",
                headers=TT_HEADERS,
                params={"bc_id": bc_id, "asset_type": "ADVERTISER", "page": page, "page_size": 50},
            )
            data = r.json()
            if data.get("code") != 0:
                log.warning("bc/asset/get failed for BC %s: %s", bc_id, data)
                break
            items = data.get("data", {}).get("list", [])
            for item in items:
                adv_id = item.get("advertiser_id") or item.get("asset_id") or item.get("id")
                if adv_id:
                    advertiser_ids.append(str(adv_id))
            page_info = data.get("data", {}).get("page_info", {})
            if page >= page_info.get("total_page", 1):
                break
            page += 1
    return advertiser_ids


async def _get_campaign_buyers_for_advertiser(client: httpx.AsyncClient, advertiser_id: str):
    buyers = set()
    r = await client.get(
        f"{TIKTOK_API_BASE}/campaign/get/",
        headers=TT_HEADERS,
        params={"advertiser_id": advertiser_id, "page": 1, "page_size": 100},
    )
    data = r.json()
    if data.get("code") != 0:
        log.warning("campaign/get failed for advertiser %s: %s", advertiser_id, data)
        return buyers
    for c in data.get("data", {}).get("list", []):
        buyer = _parse_buyer_from_campaign_name(c.get("campaign_name", ""))
        if buyer:
            buyers.add(buyer)
    return buyers


async def _get_pixel_id_for_advertiser(client: httpx.AsyncClient, advertiser_id: str):
    r = await client.get(
        f"{TIKTOK_API_BASE}/pixel/list/",
        headers=TT_HEADERS,
        params={"advertiser_id": advertiser_id},
    )
    data = r.json()
    if data.get("code") != 0:
        log.warning("pixel/list failed for advertiser %s: %s", advertiser_id, data)
        return None
    pixels = data.get("data", {}).get("pixels", []) or data.get("data", {}).get("list", [])
    if not pixels:
        return None
    return pixels[0].get("pixel_id") or pixels[0].get("pixel_code")


async def _rebuild_cache():
    global _buyer_pixel_cache, _cache_updated_at
    async with httpx.AsyncClient(timeout=30) as client:
        advertiser_ids = await _get_advertiser_ids(client)
        new_cache = {}
        for adv_id in advertiser_ids:
            buyers = await _get_campaign_buyers_for_advertiser(client, adv_id)
            if not buyers:
                continue
            pixel_id = await _get_pixel_id_for_advertiser(client, adv_id)
            if not pixel_id:
                continue
            for buyer in buyers:
                new_cache[buyer.lower()] = pixel_id
    async with _cache_lock:
        _buyer_pixel_cache = new_cache
        _cache_updated_at = time.time()
    log.info("TikTok pixel cache rebuilt: %d buyers mapped", len(new_cache))


async def get_pixel_id_for_buyer(buyer: str):
    if not buyer:
        return None
    if time.time() - _cache_updated_at > CACHE_TTL_SECONDS:
        await _rebuild_cache()
    return _buyer_pixel_cache.get(buyer.lower())


async def send_purchase_event(lead_id, buyer, ttclid, value, currency="RUB"):
    if not ttclid:
        log.info("TikTok event skipped for lead %s: no ttclid", lead_id)
        return
    if not TIKTOK_ACCESS_TOKEN:
        log.warning("TikTok event skipped: TIKTOK_ACCESS_TOKEN not set")
        return

    pixel_id = await get_pixel_id_for_buyer(buyer) if buyer else None
    if not pixel_id:
        log.info("TikTok event skipped for lead %s: no pixel mapped for buyer=%s", lead_id, buyer)
        return

    event_data = {
        "event": "CompletePayment",
        "event_id": f"amo_{lead_id}",
        "event_time": int(time.time()),
        "user": {"ttclid": ttclid},
        "properties": {
            "currency": currency,
            "value": str(round(value, 2)) if value else "0",
        },
    }
    payload = {
        "pixel_code": pixel_id,
        "event_source": "web",
        "event_source_id": pixel_id,
        "data": [event_data],
    }
    if TIKTOK_TEST_EVENT_CODE:
        payload["test_event_code"] = TIKTOK_TEST_EVENT_CODE

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{TIKTOK_API_BASE}/event/track/", headers=TT_HEADERS, json=payload)
            log.info("TikTok event sent for lead %s (buyer=%s, pixel=%s): %s %s",
                      lead_id, buyer, pixel_id, r.status_code, r.text[:300])
    except Exception as e:
        log.error("TikTok event failed for lead %s: %s", lead_id, e)
