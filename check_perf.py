"""
Inference Performance Collector
-------------------------------
Every run, for the leading models by recent token volume, fetches
OpenRouter's per-provider endpoint stats (throughput in tokens/sec and
latency) and appends them to data/perf_log.csv.

Runs inside the 10-minute poll workflow, so you get intraday resolution
on provider slowdowns (e.g. a new model launch crushing capacity).
"""

import csv
import os
import time
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
PERF_FILE = os.path.join("data", "perf_log.csv")
TOKENS_FILE = os.path.join("data", "tokens_by_model.csv")

TOP_N = 8            # how many leading models to track
ALWAYS_TRACK = []    # add model slugs here to force-track regardless of rank
PAUSE = 1.0          # seconds between endpoint calls
TIMEOUT = 30


def pick_models():
    """Top models by tokens over the last 7 recorded days, plus forced ones."""
    ranked = []
    if os.path.exists(TOKENS_FILE):
        totals, dates = {}, set()
        with open(TOKENS_FILE, newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            all_dates = sorted({r["date"] for r in rows})
            recent = set(all_dates[-7:])
            for r in rows:
                if r["date"] in recent and r["model"] != "other":
                    try:
                        totals[r["model"]] = totals.get(r["model"], 0) + int(r["tokens"])
                    except ValueError:
                        pass
            ranked = [m for m, _ in sorted(totals.items(),
                                           key=lambda kv: -kv[1])][:TOP_N]
    for m in ALWAYS_TRACK:
        if m not in ranked:
            ranked.append(m)
    return ranked


def _find_metric(d, needle):
    """Search a dict (one level of nesting) for a numeric value whose key
    contains `needle`. Defensive against schema differences."""
    flat = dict(d)
    for v in list(d.values()):
        if isinstance(v, dict):
            flat.update(v)
    for k, v in flat.items():
        if needle in k.lower() and isinstance(v, (int, float)):
            return float(v)
    return None


def fetch_endpoints(model):
    url = f"https://openrouter.ai/api/v1/models/{model}/endpoints"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json().get("data", {})
    endpoints = payload.get("endpoints", []) or []
    out = []
    for ep in endpoints:
        provider = (ep.get("provider_name") or ep.get("name") or "unknown")
        tps = _find_metric(ep, "throughput")
        lat = _find_metric(ep, "latency")
        if lat is not None and lat > 100:   # ms → seconds
            lat = lat / 1000.0
        if tps is not None or lat is not None:
            out.append((provider, tps, lat))
    return out


def main():
    models = pick_models()
    if not models:
        print("[perf] no token data yet to rank models — skipping")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for m in models:
        try:
            for provider, tps, lat in fetch_endpoints(m):
                rows.append({
                    "timestamp_utc": timestamp,
                    "model": m,
                    "provider": provider,
                    "throughput_tps": "" if tps is None else round(tps, 2),
                    "latency_s": "" if lat is None else round(lat, 3),
                })
        except Exception as e:
            print(f"[perf] {m}: failed ({e})")
        time.sleep(PAUSE)

    if not rows:
        print("[perf] no stats captured this run")
        return

    os.makedirs("data", exist_ok=True)
    file_exists = os.path.exists(PERF_FILE)
    with open(PERF_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if not file_exists:
            w.writeheader()
        w.writerows(rows)
    print(f"[perf] {timestamp}: logged {len(rows)} provider stats "
          f"across {len(models)} models")


if __name__ == "__main__":
    main()
