"""
OpenRouter Token Usage Collector
--------------------------------
Pulls platform-wide daily token totals from OpenRouter's official
datasets API (the data behind openrouter.ai/rankings) and stores:

  data/tokens_by_model.csv : one row per (date, model) — full fidelity
  data/tokens_daily.csv    : one row per date — platform totals

First run backfills from 2025-01-01. Later runs only fetch recent days.
Requires OPENROUTER_API_KEY (free key from openrouter.ai).

Attribution requirement: when republishing, cite
"Source: OpenRouter (openrouter.ai/rankings)".
"""

import csv
import os
import time
from datetime import date, datetime, timedelta

import requests

API_URL = "https://openrouter.ai/api/v1/datasets/rankings-daily"
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DATASET_START = date(2025, 1, 1)
CHUNK_DAYS = 30          # request window size
REQUEST_PAUSE = 2.5      # seconds between requests (limit: 30/min)

BY_MODEL_FILE = os.path.join("data", "tokens_by_model.csv")
DAILY_FILE = os.path.join("data", "tokens_daily.csv")


def extract_tokens(row):
    """Token count field, defensively: prefer explicit total, else sum parts."""
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
    """Fetch one date window; returns list of (date, model, tokens)."""
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


def main():
    if not API_KEY:
        print("OPENROUTER_API_KEY not set — skipping token collection.")
        return

    os.makedirs("data", exist_ok=True)
    existing = load_existing()

    # Figure out where to start: 2 days before the last stored date, to
    # pick up any late revisions, or the dataset floor on first run.
    if existing:
        last = max(d for d, _ in existing.keys())
        start = max(DATASET_START,
                    datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=2))
    else:
        start = DATASET_START
        print(f"First run: backfilling from {DATASET_START} (may take a few minutes)")

    end_goal = date.today() - timedelta(days=1)  # last complete UTC day
    if start > end_goal:
        print("Already up to date.")
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
            print(f"  {cursor} → {window_end}: {len(rows)} rows")
        except Exception as e:
            print(f"  {cursor} → {window_end}: FAILED ({e}) — continuing")
        cursor = window_end + timedelta(days=1)
        time.sleep(REQUEST_PAUSE)

    # Write full per-model file, sorted
    with open(BY_MODEL_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "model", "tokens"])
        for (d, model), tokens in sorted(existing.items()):
            w.writerow([d, model, tokens])

    # Aggregate to daily platform totals
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

    print(f"Done. {fetched} rows fetched this run; "
          f"{len(daily)} days stored ({min(daily)} → {max(daily)}).")


if __name__ == "__main__":
    main()
