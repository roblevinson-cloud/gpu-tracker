"""
OpenRouter Token Usage + Price Collector
----------------------------------------
1) Pulls platform-wide daily token totals (backfills from 2025-01-01
   on first run) into:
     data/tokens_by_model.csv  and  data/tokens_daily.csv
2) Snapshots every model's list price daily into:
     data/model_prices.csv
Requires OPENROUTER_API_KEY.
Attribution: Source: OpenRouter (openrouter.ai/rankings).
"""

import csv
import os
import time
from datetime import date, datetime, timedelta

import requests

API_URL = "https://openrouter.ai/api/v1/datasets/rankings-daily"
MODELS_URL = "https://openrouter.ai/api/v1/models"
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DATASET_START = date(2025, 1, 1)
CHUNK_DAYS = 30
REQUEST_PAUSE = 2.5

BY_MODEL_FILE = os.path.join("data", "tokens_by_model.csv")
DAILY_FILE = os.path.join("data", "tokens_daily.csv")
PRICES_FILE = os.path.join("data", "model_prices.csv")


def extract_tokens(row):
    for key in ("total_tokens", "tokens", "token_count", "count"):
        if row.get(key) is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                pass
    p, c = row.get("prompt_tokens"), row.get("completion_tokens")
    if p is not None or c is not None:
        try:
            return int(p or 0) + int(c or 0)
        except (TypeError, ValueError):
            pass
    return None


def fetch_window(start, end):
    r = requests.get(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("data", payload if isinstance(payload, list) else [])
    out = []
    for row in rows:
        d = row.get("date") or row.get("day")
        model = (row.get("model_permaslug") or row.get("model")
                 or row.get("permaslug") or "unknown")
        tokens = extract_tokens(row)
        if d and tokens is not None:
            out.append((str(d)[:10], model, tokens))
    return out


def load_existing():
    existing = {}
    if os.path.exists(BY_MODEL_FILE):
        with open(BY_MODEL_FILE, newline="") as f:
            for row in csv.DictReader(f):
                existing[(row["date"], row["model"])] = int(row["tokens"])
    return existing


def snapshot_prices():
    """Append today's per-model pricing ($/million tokens). One per day."""
    today = date.today().isoformat()
    existing_dates = set()
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, newline="") as f:
            existing_dates = {row["date"] for row in csv.DictReader(f)}
    if today in existing_dates:
        print("[prices] today already snapshotted")
        return
    try:
        headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
        r = requests.get(MODELS_URL, headers=headers, timeout=60)
        r.raise_for_status()
        models = r.json().get("data", [])
    except Exception as e:
        print(f"[prices] fetch failed: {e}")
        return
    rows = []
    for m in models:
        mid = m.get("id") or ""
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt") or 0) * 1e6
            completion = float(pricing.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        if mid:
            rows.append([today, mid, round(prompt, 4), round(completion, 4)])
    file_exists = os.path.exists(PRICES_FILE)
    with open(PRICES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["date", "model", "prompt_usd_per_m",
                        "completion_usd_per_m"])
        w.writerows(rows)
    print(f"[prices] snapshotted {len(rows)} models for {today}")


def main():
    if not API_KEY:
        print("OPENROUTER_API_KEY not set - skipping token collection.")
        return
    os.makedirs("data", exist_ok=True)
    existing = load_existing()

    if existing:
        last = max(d for d, _ in existing.keys())
        start = max(DATASET_START,
                    datetime.strptime(last, "%Y-%m-%d").date()
                    - timedelta(days=2))
    else:
        start = DATASET_START
        print(f"First run: backfilling from {DATASET_START}")

    end_goal = date.today() - timedelta(days=1)
    if start > end_goal:
        print("Token data already up to date.")
        snapshot_prices()
        return

    cursor = start
    fetched = 0
    while cursor <= end_goal:
        window_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), end_goal)
        try:
            rows = fetch_window(cursor, window_end)
            for d, model, tokens in rows:
                existing[(d, model)] = tokens
            fetched += len(rows)
            print(f"  {cursor} -> {window_end}: {len(rows)} rows")
        except Exception as e:
            print(f"  {cursor} -> {window_end}: FAILED ({e}) - continuing")
        cursor = window_end + timedelta(days=1)
        time.sleep(REQUEST_PAUSE)

    with open(BY_MODEL_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "model", "tokens"])
        for (d, model), tokens in sorted(existing.items()):
            w.writerow([d, model, tokens])

    daily = {}
    for (d, model), tokens in existing.items():
        rec = daily.setdefault(d, {"total": 0, "top50": 0, "other": 0})
        rec["total"] += tokens
        if model == "other":
            rec["other"] += tokens
        else:
            rec["top50"] += tokens
    with open(DAILY_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "total_tokens", "top50_tokens", "other_tokens"])
        for d in sorted(daily):
            rec = daily[d]
            w.writerow([d, rec["total"], rec["top50"], rec["other"]])
    print(f"Done. {fetched} rows fetched; {len(daily)} days stored.")
    snapshot_prices()


if __name__ == "__main__":
    main()
