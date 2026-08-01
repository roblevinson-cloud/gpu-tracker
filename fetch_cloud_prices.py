"""
Collects hyperscaler GPU list prices (Azure for now) into
data/cloud_prices.csv — one row per SKU x term per day, using the
cheapest qualifying US region.

Source: Azure Retail Prices API (public, keyless):
  https://prices.azure.com/api/retail/prices
Terms captured: spot, ondemand, 1yr, 3yr, 5yr (reservation lump sums
converted to effective $/hr). Linux prices only; "Low Priority"
(Batch) meters ignored.

Idempotent per day: re-running replaces today's azure rows.
Driven by cloud_skus.yml. No dependencies beyond pyyaml.
"""

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

BASE = "https://prices.azure.com/api/retail/prices"
OUT = "data/cloud_prices.csv"
FIELDS = ["date", "cloud", "gpu", "sku", "term", "region",
          "instance_usd_hr", "usd_per_gpu_hr", "gpus"]
HOURS_PER_YEAR = 8760.0


def query(filt):
    """All pages for one $filter, with 429 backoff."""
    url = BASE + "?$filter=" + urllib.parse.quote(filt)
    rows = []
    while url:
        data = None
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"  429 rate-limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
        if data is None:
            raise RuntimeError("rate-limited after retries")
        rows.extend(data.get("Items", []))
        url = data.get("NextPageLink")
        time.sleep(2)   # stay well under the API's rate limit
    return rows


def collect_azure(cfg):
    regions = set(cfg.get("regions", []))
    out = []
    for sku, meta in cfg.get("skus", {}).items():
        gpu = meta["gpu"]
        gpus = float(meta["gpus"])

        # --- consumption: on-demand + spot ---
        try:
            rows = query(f"armSkuName eq '{sku}' and priceType eq 'Consumption'")
        except Exception as e:
            print(f"[cloud] {sku} consumption failed: {e}")
            rows = []
        best = {}   # term -> (price, region)
        for r in rows:
            if r.get("armRegionName") not in regions:
                continue
            if "Windows" in r.get("productName", ""):
                continue
            name = r.get("skuName", "")
            if name.endswith(" Low Priority"):
                continue
            term = "spot" if name.endswith(" Spot") else "ondemand"
            price = float(r.get("retailPrice") or 0)
            if price <= 0:
                continue
            if term not in best or price < best[term][0]:
                best[term] = (price, r["armRegionName"])
        for term, (price, region) in best.items():
            out.append((gpu, sku, term, region, price, price / gpus, gpus))

        # --- reservations: lump sums -> effective hourly ---
        try:
            rows = query(f"armSkuName eq '{sku}' and priceType eq 'Reservation'")
        except Exception as e:
            print(f"[cloud] {sku} reservation failed: {e}")
            rows = []
        best = {}
        for r in rows:
            if r.get("armRegionName") not in regions:
                continue
            term_label = (r.get("reservationTerm") or "").strip()
            years = {"1 Year": 1, "3 Years": 3, "5 Years": 5}.get(term_label)
            if not years:
                continue
            lump = float(r.get("retailPrice") or 0)
            if lump <= 0:
                continue
            hourly = lump / (years * HOURS_PER_YEAR)
            key = f"{years}yr"
            if key not in best or hourly < best[key][0]:
                best[key] = (hourly, r["armRegionName"])
        for term, (price, region) in best.items():
            out.append((gpu, sku, term, region, price, price / gpus, gpus))

        print(f"[cloud] azure {sku} ({gpu}): "
              f"{sorted(o[2] for o in out if o[1] == sku)}")
    return out


def main():
    os.makedirs("data", exist_ok=True)
    with open("cloud_skus.yml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    if "azure" in cfg:
        for gpu, sku, term, region, price, per_gpu, gpus in collect_azure(cfg["azure"]):
            rows.append({
                "date": today, "cloud": "azure", "gpu": gpu, "sku": sku,
                "term": term, "region": region,
                "instance_usd_hr": round(price, 4),
                "usd_per_gpu_hr": round(per_gpu, 4),
                "gpus": int(gpus),
            })

    if not rows:
        print("[cloud] nothing collected — keeping existing CSV untouched")
        return

    # merge: drop any existing rows for (today, azure), keep the rest
    existing = []
    if os.path.exists(OUT):
        with open(OUT, newline="", encoding="utf-8") as f:
            existing = [r for r in csv.DictReader(f)
                        if not (r.get("date") == today and r.get("cloud") == "azure")]

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in FIELDS})
        for r in rows:
            w.writerow(r)
    print(f"[cloud] wrote {len(rows)} fresh rows ({len(existing)} kept) -> {OUT}")


if __name__ == "__main__":
    main()
