"""
GPU Availability Checker (multi-GPU version)
--------------------------------------------
Checks availability for H100, H200, B200, and B300 GPUs across
several cloud providers, writing one CSV per GPU generation:
    data/availability_log_h100.csv
    data/availability_log_h200.csv
    data/availability_log_b200.csv
    data/availability_log_b300.csv
"""

import csv
import os
from datetime import datetime, timezone

import requests

# ----------------------------- SETTINGS -----------------------------

# One entry per GPU generation to track.
# name_variants = strings we look for in each provider's GPU labels.
# Price caps are tuned to 3Fourteen's philosophy: cap at the boundary
# of "reasonable" on-demand pricing for each vintage. Tune later if
# your log shows caps that are always too high or too low.
GPU_CONFIGS = [
    {
        "name": "h100",
        "min_vram_gb": 80,
        "price_cap": 4.00,
        "name_variants": ["H100 SXM", "H100 NVL", "H100 PCIE", "H100"],
    },
    {
        "name": "h200",
        "min_vram_gb": 141,   # H200 has 141GB HBM3e
        "price_cap": 5.50,
        "name_variants": ["H200 SXM", "H200 NVL", "H200"],
    },
    {
        "name": "b200",
        "min_vram_gb": 180,   # B200 has 180-192GB HBM3e
        "price_cap": 8.00,
        "name_variants": ["B200 SXM", "B200"],
    },
    {
        "name": "b300",
        "min_vram_gb": 288,   # B300 (Blackwell Ultra) has 288GB HBM3e
        "price_cap": 12.00,
        "name_variants": ["B300 SXM", "B300", "GB300"],
    },
]

LAMBDA_API_KEY = os.environ.get("LAMBDA_API_KEY", "")
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
TIMEOUT = 30


# ------------------------- PROVIDER CHECKS --------------------------
# Each function takes a gpu_config dict and returns (available, cheapest_price).


def _matches_variant(candidate: str, variants) -> bool:
    """Case-insensitive substring match against any of the name variants."""
    c = (candidate or "").lower()
    return any(v.lower() in c for v in variants)


def check_vast_ai(cfg):
    try:
        # Vast.ai gpu_name uses exact matches for the specific variants
        variants_json = ",".join(f'"{v}"' for v in cfg["name_variants"])
        query = (
            '{"gpu_name":{"in":[' + variants_json + ']},'
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
            and o.get("gpu_ram", 0) >= cfg["min_vram_gb"] * 1000 * 0.9
            and o.get("dph_total") < cfg["price_cap"]
        ]
        if prices:
            return True, round(min(prices), 3)
        return False, None
    except Exception as e:
        print(f"[{cfg['name']}] Vast.ai failed: {e}")
        return None, None


def check_lambda_labs(cfg):
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
            if not _matches_variant(name, cfg["name_variants"]):
                continue
            itype = info.get("instance_type", {})
            price = itype.get("price_cents_per_hour", 0) / 100.0
            gpus = itype.get("specs", {}).get("gpus", 1) or 1
            price_per_gpu = price / gpus
            has_capacity = len(info.get("regions_with_capacity_available", [])) > 0
            if has_capacity and price_per_gpu < cfg["price_cap"]:
                if best is None or price_per_gpu < best:
                    best = price_per_gpu
        return (best is not None), (round(best, 3) if best else None)
    except Exception as e:
        print(f"[{cfg['name']}] Lambda failed: {e}")
        return None, None


def check_runpod(cfg):
    if not RUNPOD_API_KEY:
        return None, None
    try:
        gql = {
            "query": """
            query {
              gpuTypes {
                id displayName memoryInGb
                lowestPrice(input: {gpuCount: 1}) {
                  uninterruptablePrice stockStatus
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
            if not _matches_variant(name, cfg["name_variants"]):
                continue
            if (t.get("memoryInGb") or 0) < cfg["min_vram_gb"]:
                continue
            lp = t.get("lowestPrice") or {}
            price = lp.get("uninterruptablePrice")
            stock = (lp.get("stockStatus") or "").lower()
            in_stock = stock not in ("", "out of stock", "unavailable")
            if price is not None and price < cfg["price_cap"] and in_stock:
                if best is None or price < best:
                    best = price
        return (best is not None), (round(best, 3) if best else None)
    except Exception as e:
        print(f"[{cfg['name']}] RunPod failed: {e}")
        return None, None


# ------------------------------ MAIN --------------------------------

def log_one_gpu(cfg, timestamp):
    providers = {
        "vast": check_vast_ai(cfg),
        "lambda": check_lambda_labs(cfg),
        "runpod": check_runpod(cfg),
    }
    results = [avail for avail, _ in providers.values() if avail is not None]
    overall = (1 if any(results) else 0) if results else ""
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
    log_path = os.path.join("data", f"availability_log_{cfg['name']}.csv")
    file_exists = os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"{timestamp} | {cfg['name'].upper()} | overall={overall} | "
          f"cheapest=${cheapest} | {providers}")


def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for cfg in GPU_CONFIGS:
        log_one_gpu(cfg, timestamp)


if __name__ == "__main__":
    main()
