# GPU Tracker — System Handoff

One-page state of the system. Update when architecture changes.
Last updated: 2026-07-31.

## What this is
Self-updating compute-market monitor. Tracks GPU rental availability/
supply/prices, OpenRouter token volumes/prices/performance, a curated
token-price watchlist, and assumption-driven provider economics.
Runs entirely on GitHub Actions (free). No servers.

- Dashboard: https://roblevinson-cloud.github.io/gpu-tracker/
- Companion page: apld-valuation.html (Applied Digital NAV model,
  cross-linked with dashboard)
- Built collaboratively with Claude across July 2026. Owner is
  non-programmer; all changes go through web-editor paste-and-commit.

## Workflows (Actions tab)
| Workflow | Schedule | What it does |
|---|---|---|
| GPU Availability Poll | every 10 min (triggered externally, see below) | check_availability.py + check_perf.py + fetch_market_stats.py utilization, commits data/ |
| Build Index Chart | every 6h + manual | check_tokens.py -> fetch_cloud_prices.py -> fetch_market_stats.py sfcompute -> build_index.py -> build_economics.py (+ build_growth_table.py only if that file exists) |
| Track token prices | every 6 h | fetch_token_prices.py -> build_token_index.py (watchlist system) |
| Backfill Price History | manual only | backfill_prices.py (Wayback archive of OpenRouter prices) — if present |

**Scheduler quirk:** GitHub's cron never fired reliably for the 10-min
poll. Fix: cron-job.org account POSTs to the GitHub API every 10 min
to trigger poll.yml via workflow_dispatch. Uses a fine-grained PAT
(Actions read/write, this repo only) stored in the cron-job.org job's
Authorization header. **PAT expires 2027-06-30** — renew in GitHub
Developer settings, paste new token into cron-job.org. If polls stop
and cron-job.org shows 401s, that's why.

All workflows use a collision-proof commit pattern (commit, then
pull --rebase + push with 5 retries) because concurrent workflows
race on pushes.

## Collectors
- **check_availability.py** — every 10 min. Per GPU (h100/h200/b200/
  b300): binary availability under price cap across Vast.ai (no key),
  Lambda, RunPod; plus Vast order-book depth (deduped by machine_id,
  hosts <0.90 reliability excluded, VRAM filter has 10% slack).
  Price caps: H100 $4, H200 $5.50, **B200 $5.75** (lowered from $8 on
  Jul 17 — small series discontinuity), B300 $12.
- **check_perf.py** — same cadence. Top-8 models by 7d tokens: per-
  provider throughput (tok/s) and latency from OpenRouter endpoints
  API. Deep/defensive field matching; prints schema debug if parsing
  fails. ALWAYS_TRACK list at top for forced models.
- **check_tokens.py** — daily. OpenRouter rankings-daily dataset
  (backfilled to 2025-01-01) -> tokens_by_model.csv, tokens_daily.csv.
  Also snapshots all model list prices daily -> model_prices.csv.
- **fetch_cloud_prices.py** — daily (in Build Index workflow, added
  2026-08-01). Azure Retail Prices API (public, keyless): ND H100/
  H200 v5 + ND GB200 v6 list prices across US regions -> cheapest
  region per term (spot/ondemand/1yr/3yr/5yr; reservation lump sums
  converted to effective hourly). SKU->GPU map in cloud_skus.yml.
  Appends data/cloud_prices.csv (idempotent per day). 429-aware
  (15s+ backoff, 2s between pages). Extend later: AWS price list,
  GCP catalog, AWS Capacity Blocks forward curve, neocloud scrapes.
- **fetch_market_stats.py** — two free/no-auth sources on DIFFERENT
  cadences; pass `utilization` or `sfcompute` to pick one (no arg =
  both). Each is isolated so one failing never blocks the other:
  * utilization runs in the **10-minute poll**. It was daily until
    2026-08-02, but the daily build fires at 06:00 UTC (~11pm Pacific),
    so every reading was pegged to one quiet hour — biased, not just
    coarse. Observed intraday swings are real (B200 64% -> 55% inside
    a day), so cadence matters. Appends rather than rewriting, and
    skips the write if 500.farm's exporter hasn't refreshed since the
    last poll (its timestamp is the dedupe key, read from the file's
    tail so it stays cheap as the log grows). ALSO rewrites
    data/utilization_latest.csv (newest snapshot, 4 rows) on every
    poll — that is what the dashboard cards read, so they stay live
    between chart builds. Written even when the duplicate guard skips
    the append, so the cards never look staler than they are.
  * sfcompute stays **daily** — the page publishes one value per day
    and revises recent ones, so it needs the merge-rewrite path.
  * **500.farm** (community Vast exporter) -> data/vast_utilization.csv:
    rented/available/total per vintage + utilization % + implied
    revenue run-rate (sum of per-board count x median price). Vast's
    own API can't give global rented state; 500.farm reconstructs it
    by diffing snapshots — good estimate, no SLA. Its CDN
    intermittently serves an empty body, hence fetch() retries.
    Board-name -> vintage map is VAST_MODELS at the top of the file.
  * **sfcompute.com/prices** -> data/sfcompute_prices.csv: daily
    avg/top/bottom $/GPU-hr. No public unauth API; the series is
    embedded in the Next.js flight payload (self.__next_f pushes ->
    "pricesByHardwareType"), so the parser is structure-dependent and
    will need a look if SF Compute redesigns. Ships ~31 days of
    history, so day one backfills. Only H100 has liquidity; H200/B200
    return all-zero rows and are filtered out.
- **fetch_token_prices.py / build_token_index.py** — watchlist system
  (built in a separate chat): per-host prices for models in
  token_watchlist.yml, regions via provider_regions.yml. Outputs
  data/token_prices.csv, charts/, TOKEN_PRICE_SUMMARY.md.
  K3 pre-listed; capturing since Jul 21.

## Builders
- **build_index.py** — all core charts (house style: direct line
  labels, no legends where possible, typography sized for half-width
  display). Availability+price per GPU, supply depth, combined charts,
  token volume (linear+log), providers (lines/stacked/log), model
  drill-downs (PROVIDER_DRILLDOWNS list incl. moonshotai), weighted
  pricing, price history, perf charts (writes perf_manifest.json).
- **build_economics.py** — driven by **economics_assumptions.yml**
  (all editable guesses documented inline): implied revenue (all vs
  paid), revenue/token, serving-cost band vs market price, tokens/kWh,
  growth decomposition (log-rate version, 14d smoothed). Skips
  gracefully if inputs missing.
- **build_growth_table.py** — OPTIONAL; only if file exists (writes
  growth_table.json for the dashboard table). May not be installed.
- **Chart annotations** — events.yml (repo root): dated markers drawn
  as dashed vlines on chart families via show_on tags (gpu/h100/…/
  tokens/prices/all). $ in labels auto-escaped for mathtext.
- **Hardware value module** (in build_index.py, added 2026-07-31) —
  driven by gpu_specs.yml (dense-FP8 TFLOPS + volume-launch dates):
  pflop_price_chart.png ($/PFLOP-hr by vintage) and
  depreciation_chart.png ($/PFLOP-hr vs hardware age with cross-
  vintage exponential fit -> %/yr + half-life). Uses median Vast
  price, falls back to lowest offer. hardware_value.csv exported.
  Caveat printed on chart: FP8 metric doesn't credit B300's FP4/mem.
  Also value_lenses_chart.png (added later 2026-07-31): same rentals
  normalized per best-precision FLOP (FP4 where supported), per GB
  HBM, per TB/s bandwidth — with a max/min spread stat per panel.
  Flattest lens = what the market prices (HBM, at first fit: 1.25x
  vs compute 3.62x). Needs fp4_dense_tflops/hbm_gb/bandwidth_tbps
  in gpu_specs.yml; lines skip silently if fields absent.
- **Cloud term module** (build_cloud_term in build_index.py, added
  2026-08-01) — cloud_term_chart.png: latest-day Azure $/GPU-hr from
  spot to 5yr commitments per vintage, dotted Vast medians as the
  merchant reference. Dashboard section "Market prices".
- **Market-structure charts** (added 2026-08-01, all in build_index.py)
  * build_utilization() -> utilization_chart.png. Two panels:
    rented share by vintage, and implied revenue run-rate (price x
    quantity). This is the price-vs-quantity read: price up WITH
    revenue up = demand growth; price up with flat revenue and high
    utilization = scarcity. Pins a +/-1 day x-window while history is
    under 3 days, else the date locator sprawls over years. Also
    writes data/daily_utilization.csv (daily means, one row per
    vintage per day) — used for the cards' 7-day delta, never the raw
    10-minute log, which grows ~17MB/yr. Current card values come from
    utilization_latest.csv instead, because this file is only rewritten
    when the chart build runs.

**Staleness note (fixed 2026-08-04):** GitHub's scheduled runs for this
repo drift 5-10h, so a once-daily chart build left charts and card
values up to a day behind data that was arriving every 10 minutes.
Two mitigations: cards now read the poll-written snapshot, and the
chart build moved to every 6h. If charts ever look stale again, check
Actions for whether the *schedule* event actually fired — the poll
(cron-job.org) and the build (GitHub cron) fail independently.
  * build_venue_prices() -> venue_price_chart.png. One H100-hour
    across SF Compute (order book, with daily high/low band), Vast
    median, and Azure spot/on-demand. H100 only — the sole vintage
    liquid on all three.
  * lens spread history -> lens_spread_chart.png + lens_spread.csv
    (inside build_hardware_value): the per-lens max/min ratio
    recomputed daily. Only days where every vintage priced are used.
- **Cards**: each GPU card now shows a "Rented" row (utilization %
  with a 7d delta in percentage points), read from the single shared
  vast_utilization.csv fetch in init().
- **Bugfix 2026-07-31**: per-GPU availability charts' right-axis
  price lines were invisible since launch (ax2.set_ylim called
  before plotting froze autoscale at 0-1). Now set after plotting.

## Dashboard (index.html)
Single page, self-contained. Cards (GPU + token summary), jump nav,
sections: All vintages -> per-GPU -> Hardware value -> Token demand
(Lines/Stacked + Linear/Log toggles; log forces lines) -> Token
prices watchlist -> Provider economics -> Inference performance.
Charts are pre-rendered PNGs with cache-busting; missing images
auto-hide. APLD link in header + nav.
Added 2026-07-31: 7-day delta chips on GPU + token cards (▲/▼ vs the
row ≥7 days back in the daily CSVs; price deltas neutral-colored),
and a data-freshness badge in the header (latest data/ commit time
via the public GitHub API; green ≤45m, amber ≤3h, red beyond).

## Secrets (repo Settings -> Actions)
LAMBDA_API_KEY, RUNPOD_API_KEY (read-only), OPENROUTER_API_KEY.
Plus the PAT living only in cron-job.org.

## Known caveats
- Coverage = observable merchant/spot market only (~2-5% of capacity,
  ~20-40% of discount spot). Anthropic/OpenAI direct API invisible.
- OpenRouter token growth conflates market growth with OpenRouter
  share gains — affects growth decomposition especially.
- Economics = estimates, not measurements; assumptions file is the
  instrument panel. Revenue history uses today's prices projected
  back until price snapshots/backfill accumulate.
- Public repos idle 60 days get schedules disabled (email warning
  first); any commit resets the clock.
- Perf stats are short-window medians; low-traffic endpoints noisy.

## Watch items
- **2026-07-27: Kimi K3 open weights** — expect third-party hosts in
  watchlist prices, perf charts, moonshot drill-down. Baseline
  captured pre-launch.
- RunPod migrating GraphQL -> v2 REST; if runpod columns go blank,
  port that one function.
- PAT renewal June 2027.
