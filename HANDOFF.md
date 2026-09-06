# GPU Tracker — System Handoff

One-page state of the system. Update when architecture changes.
Last updated: 2026-09-06.

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
| Build Index Chart | every 6h + manual | check_tokens.py -> fetch_cloud_prices.py -> fetch_market_stats.py sfcompute -> fetch_aa.py -> build_index.py -> build_economics.py (+ build_growth_table.py only if that file exists) |
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
- **fetch_aa.py** — daily (Build Index workflow, added 2026-09-06).
  Artificial Analysis: the only ACTIVE-probe instrument in the tracker
  (everything else on the token side is OpenRouter's passive
  telemetry). AA fires a fixed request (1 parallel query, 1,000-token
  prompt) at each host directly and publishes P50 over 72h. Two
  collectors, isolated, `models` / `providers` / no arg = both:
  * models -> data/aa_models.csv from the free API
    (api/v2/data/llms/models, x-api-key = AA_API_KEY secret, 1,000
    req/day, we use 1). One row per scored+priced model per day
    (~400): intelligence/coding index, list price, median tps/TTFT.
    AA returns 0 for "not yet measured" -> stored blank. Per-model
    ONLY -- there is no host breakdown in the API (checked 2026-09-06;
    query params are ignored).
  * providers -> data/aa_providers.csv, scraped from each mapped
    model's /models/<slug>/providers page: the `hostModels` array in
    the Next.js flight payload (same trick as sfcompute). Per host:
    price in/out, cache-hit price + observed cache-hit RATE, tps and
    TTFT median/p05/p95, e2e time. ~110 rows/day. Structure-dependent;
    if it prints "no hostModels found" AA redesigned the page.
  * aa_models.yml maps OpenRouter slug -> AA slug (keyed by slug,
    never brand; AA lists each reasoning-effort level as its own
    model, use the bare/max slug) and AA host name -> OpenRouter
    provider name for the join. AA appends quantisation/tier tags to
    host names -- "(FP4)", "(NVFP4)", "(FAST)", " BF16" -- which the
    collector strips into a `variant` column BEFORE aliasing.
  * ATTRIBUTION REQUIRED (free-API terms): link is on the charts'
    source note and the dashboard section. Keep it.

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
- **Compute axis = dense FP16/BF16** (switched from FP8 on 2026-08-11
  when A100 was added). Only tensor format present AND full-rate on all
  five vintages, and monotonic: 312/989/989/2250/2250. Charts also draw
  a "best deployable" dashed line (FP4 Blackwell, FP8 Hopper, FP16
  Ampere). INT8/INT4 were checked and REJECTED — see the long comment
  block at the top of gpu_specs.yml; short version, INT4 is A100-only
  and INT8 is non-monotonic because NVIDIA gutted B300's INT8 (3 POPS
  vs B200's 72), putting a 2025 flagship below a 2020 A100. Do not
  re-add them.
- **Memory axis = hbm_gb_usable**, not nameplate (NVIDIA's DGX B200
  page: 1,440 GB / 8 = 180, matching Vast's reported 180).
- **VALUE_CHART_EXCLUDE = {"a100"}** (build_index.py, 2026-08-15).
  A100 is collected and charted everywhere EXCEPT the four
  cross-vintage value charts. It is a trailing-edge part with no
  FP8/FP4 priced off bandwidth, so it widened every lens spread
  (bandwidth 1.41x -> 1.60x, memory 1.16x -> 2.53x) and dropped the
  age fit to R2 0.18 while changing no conclusion. Delete the entry to
  put it back. Note the FP16 axis was kept: for the four remaining
  vintages FP8 is EXACTLY 2x FP16, so the choice only rescales the
  y-axis — rankings, spreads and the age slope are identical.
- **Hardware value module** (in build_index.py, added 2026-07-31) —
  driven by gpu_specs.yml (dense-FP8 TFLOPS + volume-launch dates):
  pflop_price_chart.png ($/PFLOP-hr by vintage — draws BOTH dense FP8
  (solid, all four) and dense FP4 (dashed, Blackwell only; Hopper has
  no FP4 units). Added 2026-08-05 because the ranking INVERTS between
  them: FP8 makes H100 cheapest at $1.10 and B300 near-dearest at
  $1.85, FP4 makes B300 cheapest at $0.62. The depreciation slope
  flips sign on the same choice, -11%/yr vs +29%/yr, R2 ~0.35 either
  way — which is the real argument for keeping that headline gated)
  and
  depreciation_chart.png ($/PFLOP-hr vs hardware age). The fit is
  GATED (added 2026-08-04): it only prints the confident
  "-X%/yr, half-life" headline when R2 >= 0.70 AND no single vintage
  carries the slope (leave-one-vintage-out keeps b < 0). Otherwise it
  draws the line faint and states what R2 actually is. As of Aug 2026
  R2 = 0.35 and dropping H100 INVERTS the slope to +12%/yr, so the
  earlier "-11%/yr, half-life 72 months" headline was not supportable
  — vintage height is set by scarcity, not age. Expect the headline to
  appear on its own once each vintage has traced months of its own
  curve. Uses median Vast
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
- **build_within_vintage()** (added 2026-08-06) -> within_vintage_chart.png
  + within_vintage.csv. 2x2 small multiples: each vintage's own median
  $/GPU-hr over calendar time with an OLS log-trend. This is the
  IDENTIFIED depreciation estimate — across vintages, age and
  architecture are perfectly collinear (every H100 is both oldest and
  Hopper), so no cross-section can separate ageing from generational
  deflation at any precision. Tracking one vintage holds generation
  constant.
  Gate is PRECISION, not significance (RATE_CI_TARGET = 0.15 log
  units ~ +/-15pp on the annual rate). This matters: a 21-day drift is
  often significant vs zero yet annualises to garbage — the first
  version printed "h100 -1142%/yr". Panels that don't qualify show a
  countdown of days still needed, derived from SE(slope) ~ n^-1.5.
  As of 2026-08-06 none qualify; H200 is nearest (~28 more days), then
  B200 ~60, H100 ~81, B300 ~99. Slope is invariant to any per-vintage
  constant, so $/hr and $/PFLOP-hr give the SAME rate — the FP8-vs-FP4
  question does not arise here, which is the point.
- **build_aa()** (build_index.py, added 2026-09-06) -> two charts,
  skips quietly if fetch_aa.py hasn't run.
  * aa_crosscheck_chart.png + data/aa_crosscheck.csv: same (model,
    host) pair measured by AA's probe and by OpenRouter's telemetry,
    ratio per host (dots = models, diamond = host median), output
    speed and TTFT panels. The TTFT panel DROPS pairs where AA's TTFT
    > AA_TTFT_MAX_S (10 s): on reasoning-mode models AA waits through
    hidden reasoning (60-150 s on Opus 5 / Luna max; its inputTime/
    reasoningTime split does not separate it) while OpenRouter's
    latency is first-chunk, so the ratio there is not a measurement.
    Joins on (or_model, provider) against the
    perf_log daily median for the latest OpenRouter day <= the AA
    day, and says so in the subtitle when they differ by >1 day. First
    reading 2026-09-06 (vs 08-15 OpenRouter data): AA reads ~1.8x
    FASTER than OpenRouter at the median, 3x+ on Fireworks/Together/
    Crusoe/SiliconFlow, and the sign is consistent across nearly every
    host -- so it is structural (single-query probe vs real concurrent
    traffic with longer prompts), not noise. Hosts that break from the
    pack (Parasail, DeepInfra on MiMo: probe SLOWER than live) are the
    ones worth a look. Needs >= 5 joinable pairs.
  * aa_hedonic_chart.png + data/aa_frontier.csv: price of a unit of
    intelligence. Top panel = today's cross-section (blended $/M vs
    AA intelligence index, models released in the last 12 months) with
    the frontier = cheapest model at least this smart. Bottom = that
    frontier's cheapest price per intelligence band (AA_BANDS: 50+,
    45-50, 40-45, 35-40) over time -- the hedonic deflator, the
    token-side analogue of $/PFLOP-hr. History starts 2026-09-06; AA
    has no backfill, so this panel is a dot until it isn't.
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
prices watchlist -> Provider economics -> Inference performance ->
Inference benchmarks (Artificial Analysis, with required attribution).
Charts are pre-rendered PNGs with cache-busting; missing images
auto-hide. APLD link in header + nav.
Added 2026-07-31: 7-day delta chips on GPU + token cards (▲/▼ vs the
row ≥7 days back in the daily CSVs; price deltas neutral-colored),
and a data-freshness badge in the header (latest data/ commit time
via the public GitHub API; green ≤45m, amber ≤3h, red beyond).

## Secrets (repo Settings -> Actions)
LAMBDA_API_KEY, RUNPOD_API_KEY (read-only), OPENROUTER_API_KEY,
AA_API_KEY (Artificial Analysis free tier). Plus the PAT living only
in cron-job.org.

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
