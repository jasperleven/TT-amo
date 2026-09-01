"""
Бэкфилл продаж (статус "Договор подписан") в пиксель VLD_общий
(BC TechnoWave_shop, 7632400042631888913).

В отличие от версии для BC Насти, список "своих" байеров тут строится
динамически: скрипт спрашивает у самого TikTok, какие рекламные кабинеты
(advertiser_id) числятся в этом BC, тянет их кампании и парсит из названий
байеров (формат "Оффер | БАЕР | домен"). Дальше сверяет с полем
"Компания:" (FIELD_COMPANY) сделки AmoCRM — там точно такой же формат.

Запуск:
    export AMO_ACCESS_TOKEN="..."
    export TIKTOK_ACCESS_TOKEN="..."        # per-pixel token для отправки событий
    export TIKTOK_READ_TOKEN="..."          # OAuth-токен с доступом на чтение кампаний ("google s")
    python3 backfill_vld.py --dry-run --yesterday
"""

import argparse
import hashlib
import os
import re
import time
import requests
from datetime import datetime, timedelta

# ── Конфиг ────────────────────────────────────────────────────────────
AMO_BASE_URL = "https://daangrah000.amocrm.ru"
AMO_ACCESS_TOKEN = os.environ["AMO_ACCESS_TOKEN"]

TIKTOK_PIXEL_ID = "D7PKT3BC77UDOFSGA31G"           # VLD_общий
TIKTOK_ACCESS_TOKEN = os.environ["TIKTOK_ACCESS_TOKEN"]     # per-pixel токен для event/track
TIKTOK_READ_TOKEN = os.environ["TIKTOK_READ_TOKEN"]         # OAuth токен с доступом campaign:read
TIKTOK_BC_ID = "7632400042631888913"               # BC TechnoWave_shop
TIKTOK_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
TIKTOK_EVENT_URL = f"{TIKTOK_API_BASE}/event/track/"

SALE_STATUS_ID = 69561406
FIELD_COMPANY_ID = 1092699  # "Компания:", формат "Оффер | БАЕР | домен"
# ─────────────────────────────────────────────────────────────────────


def get_yesterday_range():
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    end = today - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def get_date_range(date_str):
    """date_str в формате YYYY-MM-DD — весь этот день, 00:00:00 -> 23:59:59."""
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(start.timestamp()), int(end.timestamp())


def fetch_bc_advertisers(bc_id):
    """Список advertiser_id, принадлежащих указанному Business Center."""
    headers = {"Access-Token": TIKTOK_READ_TOKEN}
    r = requests.get(
        f"{TIKTOK_API_BASE}/bc/asset/get/",
        headers=headers,
        params={"bc_id": bc_id, "asset_type": "ADVERTISER"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok bc/asset/get error: {data}")
    advertisers = data.get("data", {}).get("list", [])
    if advertisers:
        print(f"  (пример элемента ответа bc/asset/get: {advertisers[0]})")
    ids = []
    for a in advertisers:
        aid = a.get("advertiser_id") or a.get("asset_id") or a.get("id")
        if aid:
            ids.append(aid)
    return ids


def fetch_campaign_names(advertiser_id):
    """Список названий кампаний конкретного advertiser_id."""
    headers = {"Access-Token": TIKTOK_READ_TOKEN}
    names = []
    page = 1
    while True:
        r = requests.get(
            f"{TIKTOK_API_BASE}/campaign/get/",
            headers=headers,
            params={
                "advertiser_id": advertiser_id,
                "page": page,
                "page_size": 100,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            print(f"  ! advertiser {advertiser_id}: campaign/get error: {data.get('message')}")
            break
        campaigns = data.get("data", {}).get("list", [])
        names.extend(c.get("campaign_name", "") for c in campaigns)
        page_info = data.get("data", {}).get("page_info", {})
        if page >= page_info.get("total_page", 1):
            break
        page += 1
    return names


def parse_buyer(campaign_or_field_name):
    """Из строки 'Оффер | БАЕР | домен' достаёт БАЕР (второй сегмент).
    Отсеивает случаи, когда во втором сегменте оказалось название оффера
    (кириллица, длинная строка) вместо короткого латинского кода байера."""
    parts = [p.strip() for p in campaign_or_field_name.split("|")]
    if len(parts) < 2:
        return None
    candidate = parts[1].upper()
    if re.fullmatch(r"[A-Z]{2,6}", candidate):
        return candidate
    return None


def resolve_bc_buyers(bc_id):
    print(f"Спрашиваю у TikTok, какие advertiser_id есть в BC {bc_id}...")
    advertiser_ids = fetch_bc_advertisers(bc_id)
    print(f"  Найдено advertiser_id: {len(advertiser_ids)}")

    buyers = set()
    for adv_id in advertiser_ids:
        names = fetch_campaign_names(adv_id)
        for name in names:
            buyer = parse_buyer(name)
            if buyer:
                buyers.add(buyer)
    print(f"  Байеры, реально встречающиеся в кампаниях этого BC: {sorted(buyers)}")
    return buyers


def fetch_status_change_events(date_from, date_to):
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    lead_times = {}  # lead_id -> unix timestamp реального перехода в статус
    page = 1
    while True:
        params = {
            "filter[type]": "lead_status_changed",
            "filter[created_at][from]": date_from,
            "filter[created_at][to]": date_to,
            "page": page,
            "limit": 250,
        }
        r = requests.get(f"{AMO_BASE_URL}/api/v4/events", headers=headers, params=params, timeout=30)
        if r.status_code == 204:
            break
        r.raise_for_status()
        data = r.json()
        events = data.get("_embedded", {}).get("events", [])
        if not events:
            break
        for ev in events:
            for v in ev.get("value_after") or []:
                status = v.get("lead_status") or {}
                if status.get("id") == SALE_STATUS_ID:
                    lead_id = ev["entity_id"]
                    event_ts = ev.get("created_at")
                    if lead_id not in lead_times:
                        lead_times[lead_id] = event_ts
        page += 1
    return lead_times


def fetch_lead(lead_id):
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    r = requests.get(f"{AMO_BASE_URL}/api/v4/leads/{lead_id}", headers=headers, params={"with": "contacts"}, timeout=30)
    r.raise_for_status()
    return r.json()


def get_field_value(custom_fields, field_id):
    for f in custom_fields or []:
        if f.get("field_id") == field_id:
            values = f.get("values") or []
            if values:
                return values[0].get("value")
    return None


def lead_matches_bc(lead, bc_buyers):
    company_raw = get_field_value(lead.get("custom_fields_values"), FIELD_COMPANY_ID)
    if not company_raw:
        return False, None
    buyer = parse_buyer(company_raw)
    return (buyer in bc_buyers if buyer else False), buyer


def fetch_contact_phone(lead):
    contacts = lead.get("_embedded", {}).get("contacts", [])
    if not contacts:
        return None
    contact_id = contacts[0]["id"]
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    r = requests.get(f"{AMO_BASE_URL}/api/v4/contacts/{contact_id}", headers=headers, timeout=30)
    r.raise_for_status()
    contact = r.json()
    for f in contact.get("custom_fields_values") or []:
        if f.get("field_code") == "PHONE":
            vals = f.get("values") or []
            if vals:
                return vals[0].get("value")
    return None


def sha256_hash(value):
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def send_tiktok_event(lead_id, phone, value, event_time, dry_run=False):
    payload = {
        "event_source": "web",
        "event_source_id": TIKTOK_PIXEL_ID,
        "data": [
            {
                "event": "CompletePayment",
                "event_id": f"amo_{lead_id}",
                "event_time": event_time,
                "user": {},
                "properties": {"value": value, "currency": "USD"},
            }
        ],
    }
    if phone:
        payload["data"][0]["user"]["phone"] = sha256_hash(phone)

    if dry_run:
        print(f"[DRY RUN] lead {lead_id}: would send {payload}")
        return

    headers = {"Access-Token": TIKTOK_ACCESS_TOKEN, "Content-Type": "application/json"}
    r = requests.post(TIKTOK_EVENT_URL, headers=headers, json=payload, timeout=15)
    print(f"lead {lead_id}: {r.status_code} {r.text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yesterday", action="store_true")
    parser.add_argument("--date", type=str, help="Конкретная дата в формате YYYY-MM-DD, например 2026-08-30")
    args = parser.parse_args()

    bc_buyers = resolve_bc_buyers(TIKTOK_BC_ID)
    if not bc_buyers:
        print("Не удалось определить ни одного байера для этого BC — прерываю, чтобы не слать всё подряд.")
        return

    if args.date:
        date_from, date_to = get_date_range(args.date)
    elif args.yesterday:
        date_from, date_to = get_yesterday_range()
    else:
        print("Укажи --yesterday или --date YYYY-MM-DD")
        return
    print(f"\nИщу переходы в статус {SALE_STATUS_ID} с {datetime.fromtimestamp(date_from)} по {datetime.fromtimestamp(date_to)}")

    lead_times = fetch_status_change_events(date_from, date_to)
    print(f"Найдено переходов в статус за период: {len(lead_times)}")

    matched = 0
    for lead_id, event_ts in lead_times.items():
        try:
            lead = fetch_lead(lead_id)
        except requests.HTTPError as e:
            print(f"lead {lead_id}: не удалось получить сделку ({e})")
            continue

        is_match, buyer = lead_matches_bc(lead, bc_buyers)
        if not is_match:
            continue

        matched += 1
        value = lead.get("price") or 0
        phone = fetch_contact_phone(lead)
        if not phone:
            print(f"lead {lead_id}: байер {buyer} совпал, но нет телефона — пропускаю")
            continue

        send_tiktok_event(lead_id, phone, value, event_ts, dry_run=args.dry_run)
        time.sleep(0.3)

    print(f"\nИз {len(lead_times)} продаж отправлено/готово к отправке в VLD_общий: {matched}")


if __name__ == "__main__":
    main()
