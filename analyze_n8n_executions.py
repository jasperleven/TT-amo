"""
Тянет все executions воркфлоу антиспама из n8n API за нужную дату,
фильтрует по домену (referer/source_domain) и считает:
- сколько дошло до реального создания лида (нода "Create Lead")
- сколько ушло в спам-логгер (нода "Send to Spam Logger")

Запуск:
    export N8N_API_KEY="..."
    python3 analyze_n8n_executions.py
"""

import os
import re
import json
import requests
from datetime import datetime

N8N_BASE_URL = "https://bodekouflaqui.beget.app"
WORKFLOW_ID = "UmoKujAAH69Lu2F6"
N8N_API_KEY = os.environ["N8N_API_KEY"]

TARGET_DATE = "2026-08-29"  # фильтр по дате (UTC, по startedAt)
TARGET_DOMAIN_SUBSTR = "v-rassrochky.site"  # искать в referer/utm_campaign

HEADERS = {"X-N8N-API-KEY": N8N_API_KEY}


def fetch_all_executions():
    executions = []
    cursor = None
    while True:
        params = {"workflowId": WORKFLOW_ID, "limit": 250, "includeData": "true"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{N8N_BASE_URL}/api/v1/executions", headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        batch = data.get("data", [])
        executions.extend(batch)
        cursor = data.get("nextCursor")
        print(f"  ...получено {len(executions)} executions так далее")
        if not cursor or not batch:
            break
    return executions


def get_referer(execution):
    try:
        webhook_runs = execution["data"]["resultData"]["runData"].get("Webhook", [])
        if not webhook_runs:
            return None
        json_data = webhook_runs[0]["data"]["main"][0][0]["json"]
        headers = json_data.get("headers", {})
        return headers.get("referer", "")
    except (KeyError, IndexError, TypeError):
        return None


def reached_node(execution, node_name):
    try:
        return node_name in execution["data"]["resultData"]["runData"]
    except (KeyError, TypeError):
        return False


def main():
    print("Тяну executions из n8n...")
    executions = fetch_all_executions()
    print(f"Всего executions получено: {len(executions)}")

    matched = []
    for ex in executions:
        started = ex.get("startedAt", "")
        if not started.startswith(TARGET_DATE):
            continue
        referer = get_referer(ex)
        if not referer or TARGET_DOMAIN_SUBSTR not in referer:
            continue
        matched.append(ex)

    print(f"\nExecutions за {TARGET_DATE} с доменом {TARGET_DOMAIN_SUBSTR}: {len(matched)}")

    leads = [ex for ex in matched if reached_node(ex, "Create Lead")]
    spam = [ex for ex in matched if reached_node(ex, "Send to Spam Logger")]
    other = [ex for ex in matched if ex not in leads and ex not in spam]

    print(f"  Дошло до Create Lead (реальный лид в AmoCRM): {len(leads)}")
    print(f"  Ушло в Send to Spam Logger (отклонено как спам): {len(spam)}")
    print(f"  Ни то ни другое (нужно смотреть отдельно): {len(other)}")

    with open("/tmp/matched_executions.json", "w") as f:
        json.dump(
            [{"id": ex["id"], "startedAt": ex["startedAt"], "referer": get_referer(ex)} for ex in matched],
            f, ensure_ascii=False, indent=2
        )
    print("\nПодробности сохранены в /tmp/matched_executions.json")


if __name__ == "__main__":
    main()
