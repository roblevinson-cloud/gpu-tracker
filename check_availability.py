"""
GPU Availability + Supply Depth Checker (v3)
--------------------------------------------
Logs two things per GPU generation, every run:

1. AVAILABILITY (same as before, same files):
   data/availability_log_{gpu}.csv  — binary "can I rent one under the cap?"

2. SUPPLY DEPTH (new files):
   data/supply_log_{gpu}.csv — how MUCH supply is visible and at what prices:
     - vast_machines / vast_gpus: deduped count of machines and total GPUs
       listed on Vast.ai at ANY price (the visible order book)
     - vast_gpus_under_cap: total GPUs priced under the cap
     - vast_min_price / vast_median_price: per-GPU price distribution
     - lambda_regions: number of Lambda regions with capacity (coarse proxy)
     - runpod_stock: RunPod's stock label (High/Medium/Low; coarse proxy)
"""

import csv
import os
import statistics
from datetime import datetime, timezone

import requests

# ----------------------------- SETTINGS -----------------------------

GPU_CONFIGS = [
    {"name": "h100", "min_vram_gb": 80,  "price_cap": 4.00,
     "name_variants": ["H100 SXM", "H100 NVL", "H100 PCIE", "H100"]},
    {"name": "h200", "min_vram_gb": 141, "price_cap": 5.50,
     "name_variants": ["H200 SXM", "H200 NVL", "H200"]},
    {"name": "b200", "min_vram_gb": 180, "price_cap": 8.00,
     "name_variants": ["B200 SXM", "B200"]},
    {"name": "b300", "min_vram_gb": 288, "price_cap": 12.00,
     "name_variants": ["B300 SXM", "B300", "GB300"]},
]

LAMBDA_API_KEY = os.environ.get("LAMBDA_API_KEY", "")
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
TIMEOUT = 30


def _matches_variant(candidate: str, variants) -> bool:
    c = (candidate or "").lower()
    return any(v.lower() in c for v in variants)


# ------------------------- VAST.AI (order book) ----------------------

def fetch_vast_offers(cfg):
    """Fetch ALL rentable offers for this GPU (no price cap, no GPU-count
    filter) so we can measure depth, then dedupe by physical machine."""
    variants_json = ",".join(f'"{v}"' for v in cfg["name_variants"])
    query = (
        '{"gpu_name":{"in":[' + variants_json + ']},'
        '"rentable":{"eq":true},'
        '"order":[["dph_total","asc"]],"type":"on-demand"}'
    )
    r = requests.get(
        "https://console.vast.ai/api/v0/bundles",
        params={"q": query},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    offers = r.json().get("offers", [])

    # Dedupe: Vast lists one physical machine as many bundle sizes
    # (1x, 2x, 4x, 8x). Keep the largest configuration per machine_id
    # so gpu counts reflect real hardware, not listing permutations.
    by_machine = {}
    for o in offers:
        if o.get("gpu_ram", 0) < cfg["min_vram_gb"] * 1000 * 0.9:  # MB
            continue
        mid = o.get("machine_id")
        if mid is None:
            continue
        prev = by_machine.get(mid)
        if prev is None or (o.get("num_gpus") or 0) > (prev.get("num_gpus") or 0):
            by_machine[mid] = o
    return list(by_machine.values())


def vast_depth_metrics(cfg):
    try:
        machines = fetch_vast_offers(cfg)
        total_gpus = sum(int(m.get("num_gpus") or 0) for m in machines)

        # Per-GPU prices for the distribution
        per_gpu_prices = []
        gpus_under_cap = 0
        for m in machines:
            n = int(m.get("num_gpus") or 0)
            dph = m.get("dph_total")
            if not n or dph is None:
                continue
            p = dph / n
            per_gpu_prices.append(p)
            if p < cfg["price_cap"]:
                gpus_under_cap += n

        return {
            "vast_machines": len(machines),
            "vast_gpus": total_gpus,
            "vast_gpus_under_cap": gpus_under_cap,
            "vast_min_price": round(min(per_gpu_prices), 3) if per_gpu_prices else "",
            "vast_median_price": round(statistics.median(per_gpu_prices), 3) if per_gpu_prices else "",
        }, per_gpu_prices
    except Exception as e:
        print(f"[{cfg['name']}] Vast depth failed: {e}")
        return {
            "vast_machines": "", "vast_gpus": "", "vast_gpus_under_cap": "",
            "vast_min_price": "", "vast_median_price": "",
        }, []


# ----------------------- LAMBDA (region proxy) -----------------------

def lambda_metrics(cfg):
    """Returns (available_under_cap, cheapest, regions_with_capacity)."""
    if not LAMBDA_API_KEY:
        return None, None, ""
    try:
        r = requests.get(
            "https://cloud.lambda.ai/api/v1/instance-types",
            headers={"Authorization": f"Bearer {LAMBDA_API_KEY}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        best = None
        regions = set()
        for name, info in data.items():
            if not _matches_variant(name, cfg["name_variants"]):
                continue
            itype = info.get("instance_type", {})
            price = itype.get("price_cents_per_hour", 0) / 100.0
            gpus = itype.get("specs", {}).get("gpus", 1) or 1
            price_per_gpu = price / gpus
            caps = info.get("regions_with_capacity_available", []) or []
            for reg in caps:
                rname = reg.get("name") if isinstance(reg, dict) else reg
                if rname:
                    regions.add(rname)
            if caps and price_per_gpu < cfg["price_cap"]:
                if best is None or price_per_gpu < best:
                    best = price_per_gpu
        return (best is not None), (round(best, 3) if best else None), len(regions)
    except Exception as e:
        print(f"[{cfg['name']}] Lambda failed: {e}")
        return None, None, ""


# ----------------------- RUNPOD (stock proxy) ------------------------

def runpod_metrics(cfg):
    """Returns (available_under_cap, cheapest, stock_label)."""
    if not RUNPOD_API_KEY:
        return None, None, ""
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
        best_stock = ""
        for t in types:
            name = t.get("displayName", "") or ""
            if not _matches_variant(name, cfg["name_variants"]):
                continue
            if (t.get("memoryInGb") or 0) < cfg["min_vram_gb"] * 0.9:
                continue
            lp = t.get("lowestPrice") or {}
            price = lp.get("uninterruptablePrice")
            stock = (lp.get("stockStatus") or "")
            in_stock = stock.lower() not in ("", "out of stock", "unavailable")
            if stock and not best_stock:
                best_stock = stock
            if price is not None and price < cfg["price_cap"] and in_stock:
                if best is None or price < best:
                    best = price
                    best_stock = stock
        return (best is not None), (round(best, 3) if best else None), best_stock
    except Exception as e:
        print(f"[{cfg['name']}] RunPod failed: {e}")
        return None, None, ""


# ------------------------------ MAIN --------------------------------

def append_row(path, row):
    os.makedirs("data", exist_ok=True)
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def process_gpu(cfg, timestamp):
    # --- depth metrics (Vast full order book) ---
    depth, per_gpu_prices = vast_depth_metrics(cfg)

    # Vast availability under cap falls straight out of the depth data
    vast_avail = None
    vast_cheapest = None
    if depth["vast_machines"] != "":
        under_cap = [p for p in per_gpu_prices if p < cfg["price_cap"]]
        vast_avail = bool(under_cap)
        vast_cheapest = round(min(under_cap), 3) if under_cap else None

    lam_avail, lam_price, lam_regions = lambda_metrics(cfg)
    rp_avail, rp_price, rp_stock = runpod_metrics(cfg)

    # --- availability log (identical schema to before) ---
    providers = {
        "vast": (vast_avail, vast_cheapest),
        "lambda": (lam_avail, lam_price),
        "runpod": (rp_avail, rp_price),
    }
    results = [a for a, _ in providers.values() if a is not None]
    overall = (1 if any(results) else 0) if results else ""
    prices = [p for _, p in providers.values() if p is not None]
    cheapest = min(prices) if prices else ""

    avail_row = {
        "timestamp_utc": timestamp,
        "overall_available": overall,
        "cheapest_price": cheapest,
    }
    for name, (a, p) in providers.items():
        avail_row[f"{name}_available"] = "" if a is None else int(a)
        avail_row[f"{name}_price"] = "" if p is None else p
    append_row(os.path.join("data", f"availability_log_{cfg['name']}.csv"), avail_row)

    # --- supply depth log (new file) ---
    supply_row = {
        "timestamp_utc": timestamp,
        **depth,
        "lambda_regions": lam_regions,
        "runpod_stock": rp_stock,
    }
    append_row(os.path.join("data", f"supply_log_{cfg['name']}.csv"), supply_row)

    print(f"{timestamp} | {cfg['name'].upper()} | avail={overall} "
          f"| vast_gpus={depth['vast_gpus']} (under cap: {depth['vast_gpus_under_cap']}) "
          f"| median=${depth['vast_median_price']} | lambda_regions={lam_regions} "
          f"| runpod_stock={rp_stock}")


def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for cfg in GPU_CONFIGS:
        process_gpu(cfg, timestamp)


if __name__ == "__main__":
    main()
