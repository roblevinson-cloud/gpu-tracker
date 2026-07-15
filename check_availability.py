"""
GPU Availability Checker
------------------------
Checks whether an 80GB H100 GPU can be rented on-demand right now,
for less than a price cap, across several cloud providers.
Appends one row to data/availability_log.csv each time it runs.

You do NOT need to run this by hand. GitHub Actions runs it
automatically on a schedule (see .github/workflows/poll.yml).
"""

import csv
import os
from datetime import datetime, timezone

import requests

# ----------------------------- SETTINGS -----------------------------

PRICE_CAP = 4.00          # dollars per hour (3Fourteen used < $4/hr for H100)
GPU_KEYWORD = "H100"      # which GPU generation to track
MIN_VRAM_GB = 80          # 80GB cards only

LOG_FILE = os.path.join("data", "availability_log.csv")

# API keys are read from the environment. On GitHub these come from
# "repository secrets" (explained in the README). Leaving one blank
# simply skips that provider.
LAMBDA_API_KEY = os.environ.get("LAMBDA_API_KEY", "")
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")

TIMEOUT = 30  # seconds before giving up on a provider


# ------------------------- PROVIDER CHECKS --------------------------
# Each function returns a tuple: (available, cheapest_price)
#   available      -> True / False, or None if the check failed
#   cheapest_price -> lowest qualifying $/hr found, or None


def check_vast_ai():
    """Vast.ai has a public marketplace search API (no key needed)."""
    try:
        query = (
            '{"gpu_name":{"in":["H100 SXM","H100 NVL","H100 PCIE","H100"]},'
            '"rentable":{"eq":true},"num_gpus":{"eq":1},'
            '"order":[["dph_total","asc"]],"type":"on-demand"}'
        )
        r = requests.get(
            "https://console.vast.ai/api/v0/bundles",
            params={"q": query},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        offers = r.json().get("offers", [])
        prices = [
            o.get("dph_total")
            for o in offers
            if o.get("dph_total") is not None
            and o.get("gpu_ram", 0) >= MIN_VRAM_GB * 1000 * 0.9  # MB, with slack
            and o.get("dph_total") < PRICE_CAP
        ]
        if prices:
            return True, round(min(prices), 3)
        return False, None
    except Exception as e:
        print(f"Vast.ai check failed: {e}")
        return None, None


def check_lambda_labs():
    """Lambda Labs: needs a free API key. Reports per-region capacity."""
    if not LAMBDA_API_KEY:
        return None, None
    try:
        r = requests.get(
            "https://cloud.lambda.ai/api/v1/instance-types",
            headers={"Authorization": f"Bearer {LAMBDA_API_KEY}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        best = None
        for name, info in data.items():
            if GPU_KEYWORD.lower() not in name.lower():
                continue
            itype = info.get("instance_type", {})
            price = itype.get("price_cents_per_hour", 0) / 100.0
            gpus = itype.get("specs", {}).get("gpus", 1) or 1
            price_per_gpu = price / gpus
            has_capacity = len(info.get("regions_with_capacity_available", [])) > 0
            if has_capacity and price_per_gpu < PRICE_CAP:
                if best is None or price_per_gpu < best:
                    best = price_per_gpu
        return (best is not None), (round(best, 3) if best else None)
    except Exception as e:
        print(f"Lambda Labs check failed: {e}")
        return None, None


def check_runpod():
    """RunPod: needs a free API key. Uses their GraphQL API."""
    if not RUNPOD_API_KEY:
        return None, None
    try:
        gql = {
            "query": """
            query {
              gpuTypes {
                id
                displayName
                memoryInGb
                securePrice
                communityPrice
                lowestPrice(input: {gpuCount: 1}) {
                  uninterruptablePrice
                  stockStatus
                }
              }
            }"""
        }
        r = requests.post(
            f"https://api.runpod.io/graphql?api_key={RUNPOD_API_KEY}",
            json=gql,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        types = r.json().get("data", {}).get("gpuTypes", []) or []
        best = None
        for t in types:
            name = t.get("displayName", "") or ""
            if GPU_KEYWORD.lower() not in name.lower():
                continue
            if (t.get("memoryInGb") or 0) < MIN_VRAM_GB:
                continue
            lp = t.get("lowestPrice") or {}
            price = lp.get("uninterruptablePrice")
            stock = (lp.get("stockStatus") or "").lower()
            in_stock = stock not in ("", "out of stock", "unavailable")
            if price is not None and price < PRICE_CAP and in_stock:
                if best is None or price < best:
                    best = price
        return (best is not None), (round(best, 3) if best else None)
    except Exception as e:
        print(f"RunPod check failed: {e}")
        return None, None


# ------------------------------ MAIN --------------------------------

def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    providers = {
        "vast": check_vast_ai(),
        "lambda": check_lambda_labs(),
        "runpod": check_runpod(),
    }

    # Overall availability: True if ANY provider has a qualifying instance.
    results = [avail for avail, _ in providers.values() if avail is not None]
    if results:
        overall = 1 if any(results) else 0
    else:
        overall = ""  # every provider errored; leave blank rather than guess

    prices = [p for _, p in providers.values() if p is not None]
    cheapest = min(prices) if prices else ""

    row = {
        "timestamp_utc": timestamp,
        "overall_available": overall,
        "cheapest_price": cheapest,
    }
    for name, (avail, price) in providers.items():
        row[f"{name}_available"] = "" if avail is None else int(avail)
        row[f"{name}_price"] = "" if price is None else price

    os.makedirs("data", exist_ok=True)
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"{timestamp} | overall={overall} | cheapest=${cheapest} | {providers}")


if __name__ == "__main__":
    main()
