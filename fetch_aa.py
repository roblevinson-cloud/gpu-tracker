"""
Collects Artificial Analysis benchmark data into two daily CSVs:

  data/aa_models.csv     one row per model per day: AA intelligence /
                         coding index, list price, median output speed
                         and time-to-first-token. From the free API.
  data/aa_providers.csv  one row per (model x host) per day for the
                         models in aa_models.yml: per-host price, cache
                         pricing, and output-speed / TTFT distributions
                         (median, p05, p95). Scraped from the model's
                         /providers page -- the free API is per-model
                         only and has no host breakdown.

Why: everything else in the token module is price, speed and volume
from ONE instrument (OpenRouter's passive telemetry). AA is an active
probe -- it fires a fixed request (1 parallel query, 1000-token prompt)
at each host directly and reports P50 over 72h. Two uses:
  1. Cross-check: same (model, host) pair measured two ways. Persistent
     gaps are structural (routing tier, prompt mix); transient gaps are
     measurement error on one side. Neither is visible from one source.
  2. Quality axis: the intelligence index lets $/token be normalised by
     capability -- a hedonic price index for inference, the token-side
     analogue of the GPU value lenses.

Source: https://artificialanalysis.ai  -- ATTRIBUTION IS REQUIRED for
free-API use (link on the dashboard). API key in AA_API_KEY (repo
secret); 1,000 requests/day limit, this script uses ~1 + N models.

The /providers scrape reads the Next.js flight payload, like the SF
Compute parser: structure-dependent, will need a look if AA redesigns.
Each model is isolated so one bad page never blocks the others.

Run one collector or both:
    python fetch_aa.py models      # API only (no scrape)
    python fetch_aa.py providers   # page scrape only
    python fetch_aa.py             # both

Daily cadence (build-index.yml). AA's 72h window makes anything
faster pointless. Idempotent per day: re-running replaces today's rows.
"""

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

API = "https://artificialanalysis.ai/api/v2/data/llms/models"
PAGE = "https://artificialanalysis.ai/models/{slug}/providers"
CFG = "aa_models.yml"
MODELS_OUT = "data/aa_models.csv"
PROV_OUT = "data/aa_providers.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
PAUSE = 2.0   # seconds between page fetches

MODELS_FIELDS = ["date", "aa_slug", "creator", "name", "release_date",
                 "intelligence_index", "coding_index",
                 "price_1m_input", "price_1m_output", "price_1m_blended_3_to_1",
                 "output_tps", "ttft_s", "ttfat_s"]
PROV_FIELDS = ["date", "or_model", "aa_slug", "host", "variant", "provider",
               "price_1m_input", "price_1m_output",
               "cache_hit_price", "cache_hit_rate",
               "tps_median", "tps_p05", "tps_p95",
               "ttft_median", "ttft_p05", "ttft_p95",
               "ttfat_input_s", "reasoning_s", "ttfat_total_s",
               "e2e_s", "json_mode", "function_calling"]

# AA tags quantisation and speed tiers onto the host name. Strip them
# into `variant` so "DeepInfra (FP4)" joins to OpenRouter's "DeepInfra".
VARIANT_RE = re.compile(r"\s*\((FP\d+|NVFP\d+|MXFP\d+|BF16|FAST)\)\s*$|\s+(BF16)$",
                        re.IGNORECASE)


def fetch(url, headers=None, timeout=60, tries=4):
    """GET with retries; AA's CDN occasionally serves an empty body."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            if body.strip():
                return body
            last = ValueError("empty response body")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                print(f"  429 rate-limited, waiting {wait}s")
                time.sleep(wait)
                last = e
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            last = e
        if attempt < tries - 1:
            time.sleep(5 * (attempt + 1))
    raise last if last else RuntimeError(f"failed to fetch {url}")


def blank_if_zero(v, nd=2):
    """AA reports 0 for 'not yet measured'; store blank, not a fake 0."""
    if v is None:
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "" if v <= 0 else round(v, nd)


def num(v, nd=3):
    if v is None:
        return ""
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return ""


def merge_write(path, fields, today, rows, key=None):
    """Drop today's existing rows (optionally only those matching key),
    keep the rest, append the fresh ones. Same shape as cloud_prices."""
    existing = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("date") == today and (key is None or key(r)):
                    continue
                existing.append(r)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in fields})
        for r in rows:
            w.writerow(r)
    print(f"[aa] wrote {len(rows)} fresh rows ({len(existing)} kept) -> {path}")


# ----------------------------------------------------------------------
# 1. Models: free API, per-model medians
# ----------------------------------------------------------------------
def collect_models(today):
    key = os.environ.get("AA_API_KEY", "")
    if not key:
        print("[aa] AA_API_KEY not set - skipping models")
        return []
    body = fetch(API, headers={"x-api-key": key})
    js = json.loads(body)
    opts = js.get("prompt_options", {})
    data = js.get("data", []) or []
    print(f"[aa] API: {len(data)} models, probe config {opts}")
    rows = []
    for m in data:
        ev = m.get("evaluations") or {}
        pr = m.get("pricing") or {}
        intel = ev.get("artificial_analysis_intelligence_index")
        blended = pr.get("price_1m_blended_3_to_1")
        # Keep models that are both scored and priced: those are the
        # ones a hedonic index can use. Unscored or unpriced entries
        # (previews, deprecated) would only pad the file.
        if intel is None or not blended:
            continue
        rows.append({
            "date": today,
            "aa_slug": m.get("slug", ""),
            "creator": (m.get("model_creator") or {}).get("slug", ""),
            "name": m.get("name", ""),
            "release_date": m.get("release_date", "") or "",
            "intelligence_index": num(intel, 1),
            "coding_index": num(ev.get("artificial_analysis_coding_index"), 1),
            "price_1m_input": num(pr.get("price_1m_input_tokens"), 4),
            "price_1m_output": num(pr.get("price_1m_output_tokens"), 4),
            "price_1m_blended_3_to_1": num(blended, 4),
            "output_tps": blank_if_zero(m.get("median_output_tokens_per_second"), 1),
            "ttft_s": blank_if_zero(m.get("median_time_to_first_token_seconds"), 3),
            "ttfat_s": blank_if_zero(m.get("median_time_to_first_answer_token"), 3),
        })
    print(f"[aa] {len(rows)} models scored+priced "
          f"({sum(1 for r in rows if r['output_tps'] != '')} with speed)")
    return rows


# ----------------------------------------------------------------------
# 2. Providers: scrape hostModels out of the Next.js flight payload
# ----------------------------------------------------------------------
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def flight_payload(html):
    """Concatenate the streamed RSC chunks into one decoded string."""
    out = []
    for chunk in _PUSH_RE.findall(html):
        try:
            out.append(json.loads('"' + chunk + '"'))
        except json.JSONDecodeError:
            out.append(chunk.encode().decode("unicode_escape", "ignore"))
    return "".join(out)


def json_array_at(s, start):
    """Return the JSON array beginning at s[start] == '[', respecting
    strings, or None if unbalanced."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        i += 1
    return None


def extract_host_models(html, slug):
    """Every hostModels[] entry for this model slug, deduped by id."""
    flat = flight_payload(html)
    seen, out = set(), []
    for m in re.finditer(r'"hostModels":\[', flat):
        arr = json_array_at(flat, m.end() - 1)
        if not arr:
            continue
        try:
            items = json.loads(arr)
        except json.JSONDecodeError:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if (it.get("model") or {}).get("slug") not in (None, slug):
                continue
            hid = it.get("id") or it.get("slug")
            if hid in seen:
                continue
            seen.add(hid)
            out.append(it)
    return out


def split_host(name):
    """'DeepInfra (FP4)' -> ('DeepInfra', 'FP4'); 'Fireworks' -> ('Fireworks', '')."""
    m = VARIANT_RE.search(name or "")
    if not m:
        return (name or "").strip(), ""
    variant = (m.group(1) or m.group(2) or "").upper()
    return name[:m.start()].strip(), variant


def collect_providers(today, cfg):
    models = cfg.get("models") or {}
    aliases = cfg.get("hosts") or {}
    rows = []
    for or_model, aa_slug in models.items():
        url = PAGE.format(slug=aa_slug)
        try:
            html = fetch(url)
            items = extract_host_models(html, aa_slug)
        except Exception as e:
            print(f"[aa] {aa_slug}: page failed ({e})")
            time.sleep(PAUSE)
            continue
        if not items:
            print(f"[aa] {aa_slug}: no hostModels found - page structure "
                  f"may have changed")
            time.sleep(PAUSE)
            continue
        n_perf = 0
        for it in items:
            host_name = (it.get("host") or {}).get("name", "") or ""
            host, variant = split_host(host_name)
            pr = it.get("pricing") or {}
            perf = it.get("performance") or {}
            tps = perf.get("outputSpeed") or {}
            ttft = perf.get("timeToFirstToken") or {}
            e2e = perf.get("endToEndResponseTime") or {}
            # timeToFirstToken INCLUDES reasoning for reasoning-mode models
            # (100s+ on GPT-5.6 Luna max). timeToFirstAnswerToken splits
            # it: inputTime is the pre-reasoning TTFT, the figure that is
            # comparable to OpenRouter's latency.
            tfa = perf.get("timeToFirstAnswerToken") or {}
            if tps.get("median") is not None:
                n_perf += 1
            rows.append({
                "date": today,
                "or_model": or_model,
                "aa_slug": aa_slug,
                "host": host,
                "variant": variant,
                "provider": aliases.get(host, host),
                "price_1m_input": num(pr.get("price1mInputTokens"), 4),
                "price_1m_output": num(pr.get("price1mOutputTokens"), 4),
                "cache_hit_price": num(pr.get("cacheHitPrice"), 4),
                "cache_hit_rate": num(pr.get("cacheHitRate"), 4),
                "tps_median": num(tps.get("median"), 1),
                "tps_p05": num(tps.get("percentile05"), 1),
                "tps_p95": num(tps.get("percentile95"), 1),
                "ttft_median": num(ttft.get("median"), 3),
                "ttft_p05": num(ttft.get("percentile05"), 3),
                "ttft_p95": num(ttft.get("percentile95"), 3),
                "ttfat_input_s": num(tfa.get("inputTime"), 3),
                "reasoning_s": num(tfa.get("reasoningTime"), 3),
                "ttfat_total_s": num(tfa.get("totalTime"), 3),
                "e2e_s": num(e2e.get("totalTime"), 2),
                "json_mode": "" if it.get("jsonMode") is None else int(bool(it.get("jsonMode"))),
                "function_calling": "" if it.get("functionCalling") is None else int(bool(it.get("functionCalling"))),
            })
        print(f"[aa] {aa_slug:36} {len(items):2d} hosts, {n_perf:2d} with perf")
        time.sleep(PAUSE)
    return rows


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    os.makedirs("data", exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if which in ("models", "both"):
        try:
            rows = collect_models(today)
            if rows:
                merge_write(MODELS_OUT, MODELS_FIELDS, today, rows)
            else:
                print("[aa] nothing from API - keeping existing CSV untouched")
        except Exception as e:
            print(f"[aa] models collector failed: {e}")

    if which in ("providers", "both"):
        try:
            rows = collect_providers(today, cfg)
            if rows:
                merge_write(PROV_OUT, PROV_FIELDS, today, rows)
            else:
                print("[aa] nothing from provider pages - keeping existing CSV untouched")
        except Exception as e:
            print(f"[aa] providers collector failed: {e}")


if __name__ == "__main__":
    main()
