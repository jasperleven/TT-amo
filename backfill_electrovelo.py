"""
Разовый бэкфилл: находит сделки AmoCRM в статусе "Договор подписан"
(status_id=69561406) за текущую неделю и отправляет CompletePayment
в TikTok Events API ТОЛЬКО в один пиксель — "Электровелосипед общий".

Не трогает боевой tiktok_events.py. Запускать вручную на сервере:
    python3 backfill_electrovelo.py --dry-run     # сначала посмотреть, что найдётся
    python3 backfill_electrovelo.py                # реальная отправка
"""

import argparse
import hashlib
import os
import time
import requests
from datetime import datetime, timedelta

# ── Конфиг ────────────────────────────────────────────────────────────
AMO_BASE_URL = "https://daangrah000.amocrm.ru"  # уточни свой поддомен, если другой
AMO_ACCESS_TOKEN = os.environ["AMO_ACCESS_TOKEN"]  # export AMO_ACCESS_TOKEN=... перед запуском

TIKTOK_PIXEL_ID = "D0SQP1RC77UEHH7PROEG"
TIKTOK_ACCESS_TOKEN = os.environ["TIKTOK_ACCESS_TOKEN"]  # export TIKTOK_ACCESS_TOKEN=... перед запуском
TIKTOK_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"

SALE_STATUS_ID = 69561406
PIPELINE_ID = None  # если нужно ограничить одной воронкой — впиши ID, иначе None

# Байеры, работающие с BC Насти (пиксель "Электровелосипед общий"), из таблицы
# активных кампаний tt_links_2.xlsx, лист "Статистика" (колонка Advertiser ID
# + разбор названия кампании "Товар | БАЕР | ссылка"). Сверяется с тегами сделки.
# KRL исключён: в таблице у него 0.24$ расхода и 0 лидов за весь период — это
# не реальный активный байер, а заброшенная/тестовая запись.
NASTYA_BC_BUYER_TAGS = ["VAD", "VLD", "ART", "BNS"]
# ─────────────────────────────────────────────────────────────────────


def lead_matches_electrovelo(lead) -> bool:
    """True, если у сделки есть тег байера, работающего с BC Насти."""
    tags = lead.get("_embedded", {}).get("tags", [])
    tag_names = {t.get("name", "").strip().upper() for t in tags}
    return bool(tag_names & set(NASTYA_BC_BUYER_TAGS))


def get_week_range():
    """Понедельник этой недели 00:00 -> сейчас, unix timestamps."""
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp()), int(now.timestamp())


def get_yesterday_range():
    """Вчера 00:00 -> вчера 23:59:59, unix timestamps."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today - timedelta(days=1)
    yesterday_end = today - timedelta(seconds=1)
    return int(yesterday_start.timestamp()), int(yesterday_end.timestamp())


def fetch_status_change_events(date_from, date_to):
    """
    Тянет события смены статуса на SALE_STATUS_ID через /api/v4/events —
    это даёт точное время ПЕРЕХОДА в статус, а не любого обновления сделки.
    Возвращает список entity_id (lead_id), у которых переход произошёл в периоде.
    """
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    lead_ids = []
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
            value_after = ev.get("value_after") or []
            for v in value_after:
                status = (v.get("lead_status") or {})
                if status.get("id") == SALE_STATUS_ID:
                    lead_ids.append(ev["entity_id"])
        page += 1
    return list(dict.fromkeys(lead_ids))  # убираем дубли, сохраняя порядок


def fetch_lead(lead_id):
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    params = {"with": "contacts"}  # теги уже приходят в _embedded по умолчанию для одиночной сделки
    r = requests.get(f"{AMO_BASE_URL}/api/v4/leads/{lead_id}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_contact_phone(contact_id):
    headers = {"Authorization": f"Bearer {AMO_ACCESS_TOKEN}"}
    r = requests.get(f"{AMO_BASE_URL}/api/v4/contacts/{contact_id}", headers=headers, timeout=30)
    r.raise_for_status()
    contact = r.json()
    for field in contact.get("custom_fields_values") or []:
        if field.get("field_code") == "PHONE":
            for v in field.get("values", []):
                return v.get("value")
    return None


def sha256_hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def send_tiktok_event(lead_id, phone, value, currency="USD", dry_run=False):
    payload = {
        "event_source": "web",
        "event_source_id": TIKTOK_PIXEL_ID,
        "data": [
            {
                "event": "CompletePayment",
                "event_id": f"amo_{lead_id}",
                "event_time": int(time.time()),
                "user": {},
                "properties": {
                    "value": value,
                    "currency": currency,
                },
            }
        ],
    }
    if phone:
        payload["data"][0]["user"]["phone"] = sha256_hash(phone)

    if dry_run:
        print(f"[DRY RUN] lead {lead_id}: would send {payload}")
        return None

    headers = {"Access-Token": TIKTOK_ACCESS_TOKEN, "Content-Type": "application/json"}
    r = requests.post(TIKTOK_URL, headers=headers, json=payload, timeout=15)
    print(f"lead {lead_id}: {r.status_code} {r.text}")
    return r


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет отправлено")
    parser.add_argument("--yesterday", action="store_true", help="только вчерашний день вместо текущей недели")
    args = parser.parse_args()

    date_from, date_to = get_yesterday_range() if args.yesterday else get_week_range()
    print(f"Ищу переходы в статус {SALE_STATUS_ID}, случившиеся с {datetime.fromtimestamp(date_from)} по {datetime.fromtimestamp(date_to)}")

    lead_ids = fetch_status_change_events(date_from, date_to)
    print(f"Найдено переходов в статус за период: {len(lead_ids)}")

    leads = []
    for lead_id in lead_ids:
        try:
            leads.append(fetch_lead(lead_id))
        except requests.HTTPError as e:
            print(f"lead {lead_id}: не удалось получить сделку ({e}), пропускаю")

    leads = [lead for lead in leads if lead_matches_electrovelo(lead)]
    print(f"Из них с тегом байера BC Насти ({', '.join(NASTYA_BC_BUYER_TAGS)}): {len(leads)}")

    for lead in leads:
        lead_id = lead["id"]
        value = lead.get("price") or 0

        contact_id = None
        contacts = lead.get("_embedded", {}).get("contacts", [])
        if contacts:
            contact_id = contacts[0]["id"]

        phone = fetch_contact_phone(contact_id) if contact_id else None

        if not phone:
            print(f"lead {lead_id}: нет телефона, пропускаю (нет сигнала для матчинга)")
            continue

        send_tiktok_event(lead_id, phone, value, dry_run=args.dry_run)
        time.sleep(0.3)  # не долбить API слишком часто


if __name__ == "__main__":
    main()
