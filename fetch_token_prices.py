#!/usr/bin/env python3
"""
Fetch per-provider token prices from OpenRouter for a watchlist of models
and append them to data/token_prices.csv.

Data source: OpenRouter public API (no key required for these endpoints):
  GET https://openrouter.ai/api/v1/models/{author}/{slug}/endpoints

Each row in the CSV = one (timestamp, model, provider) observation:
  timestamp_utc, model, model_class, provider, region, quantization,
  input_usd_per_m, output_usd_per_m, blended_usd_per_m,
  context_length, uptime_30m

Run it as often as you like — it only appends, never overwrites.
"""

import csv
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

import yaml

BASE = "https://openrouter.ai/api/v1/models"
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "data", "token_prices.csv")
WATCHLIST_PATH = os.path.join(HERE, "token_watchlist.yml")
REGIONS_PATH = os.path.join(HERE, "provider_regions.yml")

HEADERS = {
    "User-Agent": "gpu-tracker-token-prices/1.0 (personal research)",
    "Accept": "application/json",
}

CSV_COLUMNS = [
    "timestamp_utc", "model", "model_class", "provider", "region",
    "quantization", "input_usd_per_m", "output_usd_per_m",
    "blended_usd_per_m", "context_length", "uptime_30m",
]


def load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def region_for(provider_name, region_map):
    p = (provider_name or "").lower()
    for region, needles in region_map.items():
        for needle in needles or []:
            if str(needle).lower() in p:
                return region
    return "UNKNOWN"


def per_million(price_str):
    """OpenRouter prices are USD per single token, as strings. -> USD per 1M."""
    try:
        return round(float(price_str) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def fetch_model_endpoints(slug):
    """Return the endpoint list for a model slug, or None if not found."""
    url = f"{BASE}/{slug}/endpoints"
    try:
        payload = get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  [skip] {slug}: not listed on OpenRouter (404)")
            return None
        print(f"  [error] {slug}: HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001 - keep the poller alive
        print(f"  [error] {slug}: {e}")
        return None
    data = payload.get("data") or {}
    return data.get("endpoints") or []


def rows_for_model(slug, model_class, ratio, region_map, now_iso):
    endpoints = fetch_model_endpoints(slug)
    if endpoints is None:
        return []
    rows = []
    w_in, w_out = ratio
    denom = w_in + w_out
    for ep in endpoints:
        provider = ep.get("provider_name") or ep.get("name") or "unknown"
        pricing = ep.get("pricing") or {}
        inp = per_million(pricing.get("prompt"))
        out = per_million(pricing.get("completion"))
        if inp is None or out is None:
            continue
        # Skip free/promo endpoints — they distort the cost signal
        if inp == 0 and out == 0:
            continue
        blended = round((inp * w_in + out * w_out) / denom, 4)
        rows.append({
            "timestamp_utc": now_iso,
            "model": slug,
            "model_class": model_class,
            "provider": provider,
            "region": region_for(provider, region_map),
            "quantization": ep.get("quantization") or "unspecified",
            "input_usd_per_m": inp,
            "output_usd_per_m": out,
            "blended_usd_per_m": blended,
            "context_length": ep.get("context_length") or "",
            "uptime_30m": ep.get("uptime_last_30m") or "",
        })
    print(f"  [ok] {slug}: {len(rows)} provider endpoints")
    return rows


def main():
    watchlist = load_yaml(WATCHLIST_PATH)
    region_map = load_yaml(REGIONS_PATH) or {}
    ratio = watchlist.get("input_output_ratio", [3, 1])
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    all_rows = []
    for slug in watchlist.get("open_models", []) or []:
        all_rows += rows_for_model(slug, "open", ratio, region_map, now_iso)
    for slug in watchlist.get("closed_benchmarks", []) or []:
        all_rows += rows_for_model(slug, "closed", ratio, region_map, now_iso)

    if not all_rows:
        print("No rows collected — check network or watchlist slugs.")
        sys.exit(1)

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(all_rows)

    print(f"Appended {len(all_rows)} rows to {CSV_PATH}")


if __name__ == "__main__":
    main()

