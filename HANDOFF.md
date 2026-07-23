# GPU Tracker — System Handoff

One-page state of the system. Update when architecture changes.
Last updated: 2026-07-22.

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
| GPU Availability Poll | every 10 min (triggered externally, see below) | check_availability.py + check_perf.py, commits data/ |
| Build Index Chart | daily 06:00 UTC + manual | check_tokens.py -> build_index.py -> build_economics.py (+ build_growth_table.py only if that file exists) |
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

## Dashboard (index.html)
Single page, self-contained. Cards (GPU + token summary), jump nav,
sections: All vintages -> per-GPU -> Token demand (Lines/Stacked +
Linear/Log toggles; log forces lines) -> Token prices watchlist ->
Provider economics -> Inference performance. Charts are pre-rendered
PNGs with cache-busting; missing images auto-hide. APLD link in
header + nav.

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
