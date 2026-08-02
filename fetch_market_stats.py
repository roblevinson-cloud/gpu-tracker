"""
Collects two market-structure signals the price logs can't see:

  data/vast_utilization.csv   how much of Vast's visible fleet is
                              actually RENTED (not just listed), plus
                              an implied revenue run-rate = rented x price
  data/sfcompute_prices.csv   SF Compute cluster lease rates (a real
                              order-book venue), daily avg/top/bottom

Why these matter: the rest of the tracker measures PRICE. A high price
can mean scarcity (tight supply) or a booming market (demand growing).
Utilization and price x quantity separate those two stories.

Sources (both free, no auth):
  500.farm  - community Vast.ai exporter. Vast's own API does not
              expose rented state globally; 500.farm reconstructs it
              by diffing offer/machine snapshots. Treat as a good
              estimate, not ground truth. No SLA - failures are
              tolerated and simply skip the day.
  sfcompute.com/prices - Next.js page; the daily series is embedded
              in the streamed flight payload (no public unauth API).
              Ships ~31 days of history, so the first run backfills.

Run one collector or both:
    python fetch_market_stats.py utilization   # every 10 min (poll.yml)
    python fetch_market_stats.py sfcompute     # daily (build-index.yml)
    python fetch_market_stats.py               # both

They run at different cadences on purpose. Utilization has a strong
intraday cycle, so a once-a-day sample would peg every reading to the
same hour and bias the series; SF Compute publishes one value per day,
so polling it more often would just re-download the same numbers.

If one source fails the other still runs, and neither failure is fatal
to the workflow that calls it.
"""

import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 500.farm GPU model names -> tracker vintage. Vast lists board
# variants separately; we aggregate them per vintage because the
# question is "is this generation busy", not "is this SKU busy".
# Update if Vast renames a board (check /vastai-exporter/gpu-stats).
VAST_MODELS = {
    "h100": ["H100 SXM", "H100 PCIE", "H100 NVL"],
    "h200": ["H200", "H200 NVL"],
    "b200": ["B200"],
    "b300": ["B300"],
}

UTIL_OUT = "data/vast_utilization.csv"
UTIL_FIELDS = ["timestamp_utc", "gpu", "rented", "available", "total",
               "utilization_pct", "avg_price_rented", "revenue_usd_hr"]

SF_OUT = "data/sfcompute_prices.csv"
SF_FIELDS = ["date", "gpu", "avg_usd_gpu_hr", "top_usd_gpu_hr",
             "bottom_usd_gpu_hr"]


def fetch(url, timeout=60, tries=4):
    """GET with retries and transparent gunzip.

    500.farm's cache serves a gzipped variant even when the request
    doesn't ask for one (and does so only from some edges — which is
    why this looked intermittent), so decompression is decided by the
    body's magic bytes rather than by the response header."""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", "replace")
            if body.strip():
                return body
            last = ValueError("empty response body")
        except (urllib.error.URLError, OSError, EOFError, gzip.BadGzipFile) as e:
            last = e
        if attempt < tries - 1:
            time.sleep(5 * (attempt + 1))
    raise last if last else RuntimeError(f"failed to fetch {url}")


def fetch_json(url, **kw):
    """fetch() + parse, retrying once more if the body isn't valid JSON."""
    for attempt in range(2):
        body = fetch(url, **kw)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            if attempt:
                raise
            print(f"  non-JSON body from {url} ({body[:60]!r}) — retrying")
            time.sleep(5)


def append_rows(path, fields, rows):
    """Append-only writer for the 10-minute log — rewriting a file that
    grows all year on every poll would be pointless work."""
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def last_logged_timestamp(path):
    """Timestamp of the final row, read from the file's tail so this
    stays cheap as the log grows. Assumes timestamp is column 0."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode("utf-8", "replace")
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines or lines[-1].startswith("timestamp_utc"):
        return None
    return lines[-1].split(",")[0]


def merge_write(path, fields, fresh, key_fn):
    """Rewrite `path` keeping old rows whose key isn't in `fresh`.
    For low-cadence sources that revise past values (SF Compute)."""
    existing = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            keys = {key_fn(r) for r in fresh}
            existing = [r for r in csv.DictReader(f) if key_fn(r) not in keys]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in fields})
        for r in fresh:
            w.writerow(r)
    return len(existing)


# ------------------------- Vast utilization --------------------------

def _bucket(model, state):
    """stats.<state>.all[0] -> (count, price_median); zeros if absent."""
    entries = ((model.get("stats") or {}).get(state) or {}).get("all") or []
    if not entries:
        return 0, None
    e = entries[0]
    return int(e.get("count") or 0), e.get("price_median")


def collect_vast_utilization():
    data = fetch_json("https://500.farm/vastai-exporter/gpu-stats")
    by_name = {m.get("name"): m for m in data.get("models", [])}
    stamp = (data.get("timestamp") or "")[:19].replace("T", " ") or \
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for gpu, names in VAST_MODELS.items():
        rented = available = 0
        revenue = 0.0
        for name in names:
            model = by_name.get(name)
            if not model:
                continue
            r_count, r_price = _bucket(model, "rented")
            a_count, _ = _bucket(model, "available")
            rented += r_count
            available += a_count
            if r_price:
                # per-variant price x count is more faithful than
                # blending medians across boards of different value
                revenue += r_count * float(r_price)
        total = rented + available
        if total == 0:
            print(f"[util] {gpu}: no listings found — skipping")
            continue
        rows.append({
            "timestamp_utc": stamp,
            "gpu": gpu,
            "rented": rented,
            "available": available,
            "total": total,
            "utilization_pct": round(100.0 * rented / total, 2),
            "avg_price_rented": round(revenue / rented, 4) if rented else "",
            "revenue_usd_hr": round(revenue, 2),
        })
        print(f"[util] {gpu}: {rented}/{total} rented "
              f"({100.0 * rented / total:.1f}%), "
              f"${revenue:,.0f}/hr implied")
    return rows


# ------------------------ SF Compute prices --------------------------

def _extract_json_object(text, start):
    """Balanced-brace slice starting at the '{' at `start`."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise ValueError("unbalanced JSON object")


def collect_sfcompute():
    html = fetch("https://sfcompute.com/prices")
    # The page streams its data as escaped strings in __next_f pushes.
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', html)
    if not chunks:
        raise ValueError("no flight payload found (page structure changed?)")
    flight = "".join(json.loads(c) for c in chunks)

    key = '"pricesByHardwareType":'
    i = flight.find(key)
    if i < 0:
        raise ValueError("pricesByHardwareType missing (page changed?)")
    obj = json.loads(_extract_json_object(flight, flight.index("{", i + len(key))))

    rows = []
    for hw, series in obj.items():
        gpu = hw.strip().lower()
        kept = 0
        for rec in series or []:
            # dates arrive as Next.js date markers: "$D2026-08-01T23:59:59Z"
            raw = str(rec.get("date") or "")
            date = raw[2:12] if raw.startswith("$D") else raw[:10]
            avg = rec.get("avg")
            if not date or not avg:
                continue          # zero rows = no liquidity that day
            rows.append({
                "date": date,
                "gpu": gpu,
                "avg_usd_gpu_hr": round(float(avg), 4),
                "top_usd_gpu_hr": round(float(rec.get("top") or 0), 4),
                "bottom_usd_gpu_hr": round(float(rec.get("bottom") or 0), 4),
            })
            kept += 1
        print(f"[sfc] {gpu}: {kept} priced days "
              f"({len(series or []) - kept} zero/illiquid)")
    return rows


def run_utilization():
    """High cadence: appended by the 10-minute poll. Sampling once a
    day would peg every reading to the same hour, and utilization has
    a strong intraday cycle — so the daily figure would be biased,
    not merely coarse."""
    try:
        prev = last_logged_timestamp(UTIL_OUT)
        rows = collect_vast_utilization()
        if not rows:
            return
        if rows[0]["timestamp_utc"] == prev:
            print("[util] exporter hasn't refreshed since last poll — "
                  "skipping duplicate snapshot")
            return
        append_rows(UTIL_OUT, UTIL_FIELDS, rows)
        print(f"[util] appended {len(rows)} rows -> {UTIL_OUT}")
    except Exception as e:
        print(f"[util] FAILED ({type(e).__name__}: {e}) — leaving CSV as-is")


def run_sfcompute():
    """Low cadence: the page publishes one value per day and revises
    recent days, so a daily merge-rewrite is the right shape."""
    try:
        rows = collect_sfcompute()
        if rows:
            kept = merge_write(SF_OUT, SF_FIELDS, rows,
                               lambda r: (r.get("date"), r.get("gpu")))
            print(f"[sfc] wrote {len(rows)} rows ({kept} kept) -> {SF_OUT}")
    except Exception as e:
        print(f"[sfc] FAILED ({type(e).__name__}: {e}) — leaving CSV as-is")


def main():
    os.makedirs("data", exist_ok=True)
    which = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    if which not in ("all", "utilization", "sfcompute"):
        raise SystemExit(f"usage: {sys.argv[0]} [utilization|sfcompute]")
    if which in ("all", "utilization"):
        run_utilization()
    if which in ("all", "sfcompute"):
        run_sfcompute()


if __name__ == "__main__":
    main()
