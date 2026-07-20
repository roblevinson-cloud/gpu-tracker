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


def _flatten(d, prefix="", depth=0, out=None):
    """Flatten nested dicts/lists (3 levels) to dotted-path -> numeric value."""
    if out is None:
        out = {}
    if depth > 3:
        return out
    if isinstance(d, dict):
        items = d.items()
    elif isinstance(d, list):
        items = enumerate(d)
    else:
        return out
    for k, v in items:
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[path.lower()] = float(v)
        elif isinstance(v, str):
            try:
                out[path.lower()] = float(v)
            except ValueError:
                pass
        elif isinstance(v, (dict, list)):
            _flatten(v, path, depth + 1, out)
    return out


THROUGHPUT_NEEDLES = ["throughput", "tokens_per_second", "tokens_per_sec",
                      "tok_per_sec", "tps"]
LATENCY_NEEDLES = ["ttft", "time_to_first", "latency"]


def _pick(flat, needles):
    """Prefer p50/median variants, then any match."""
    candidates = [(k, v) for k, v in flat.items()
                  if any(n in k for n in needles)]
    if not candidates:
        return None
    for pref in ("p50", "median"):
        for k, v in candidates:
            if pref in k:
                return v
    return candidates[0][1]


def fetch_endpoints(model, debug=False):
    url = f"https://openrouter.ai/api/v1/models/{model}/endpoints"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json().get("data", {})
    endpoints = payload.get("endpoints", []) or []
    out = []
    for ep in endpoints:
        provider = (ep.get("provider_name") or ep.get("name") or "unknown")
        flat = _flatten(ep)
        tps = _pick(flat, THROUGHPUT_NEEDLES)
        lat = _pick(flat, LATENCY_NEEDLES)
        if lat is not None and lat > 100:   # ms → seconds
            lat = lat / 1000.0
        if tps is not None or lat is not None:
            out.append((provider, tps, lat))
    if debug and endpoints and not out:
        # No metrics matched: print the field names we actually received
        sample = _flatten(endpoints[0])
        keys = sorted(sample.keys())
        print(f"[perf][debug] {model}: no metric fields matched. "
              f"Numeric fields present on first endpoint:")
        for k in keys[:60]:
            print(f"[perf][debug]   {k}")
        if not keys:
            print("[perf][debug]   (no numeric fields at all — stats may "
                  "not be exposed on this endpoint)")
    return out


def main():
    models = pick_models()
    if not models:
        print("[perf] no token data yet to rank models — skipping")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for i, m in enumerate(models):
        try:
            for provider, tps, lat in fetch_endpoints(m, debug=(i == 0)):
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
