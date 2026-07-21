#!/usr/bin/env python3
"""
Build charts + a markdown summary from data/token_prices.csv.

Outputs (committed by the workflow so they render on GitHub):
  charts/price_history.png      - blended $/M over time, one line per model
                                  (cheapest-3-provider average = "market cost")
  charts/open_vs_closed.png     - open-market index vs closed sticker prices
  charts/region_comparison.png  - median blended $/M by region (same models only)
  TOKEN_PRICE_SUMMARY.md        - latest snapshot table + regional analysis

The "market cost" per model = mean of the 3 cheapest providers at each
timestamp. Using the cheapest tail approximates marginal cost under the
commodity assumption (competition drives the low end toward cost).
"""

import os
from datetime import datetime, timezone

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "data", "token_prices.csv")
CHART_DIR = os.path.join(HERE, "charts")
SUMMARY_PATH = os.path.join(HERE, "TOKEN_PRICE_SUMMARY.md")


def market_cost(group, n=3):
    """Mean blended price of the n cheapest providers in a snapshot."""
    return group.nsmallest(n, "blended_usd_per_m")["blended_usd_per_m"].mean()


def main():
    if not os.path.exists(CSV_PATH):
        raise SystemExit("No data yet — run fetch_token_prices.py first.")

    df = pd.read_csv(CSV_PATH)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"])
    os.makedirs(CHART_DIR, exist_ok=True)

    # ---- 1. Per-model market-cost history --------------------------------
    hist = (
        df.groupby(["timestamp_utc", "model", "model_class"])
        .apply(market_cost, include_groups=False)
        .rename("market_blended")
        .reset_index()
    )

    fig, ax = plt.subplots(figsize=(11, 6))
    for model, g in hist.groupby("model"):
        style = "-" if g["model_class"].iloc[0] == "open" else "--"
        ax.plot(g["timestamp_utc"], g["market_blended"], style,
                marker=".", markersize=3, label=model)
    ax.set_yscale("log")
    ax.set_ylabel("Blended $/M tokens (3:1 in:out), cheapest-3 avg — log scale")
    ax.set_title("Token price history by model (solid=open, dashed=closed)")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(os.path.join(CHART_DIR, "price_history.png"), dpi=140)
    plt.close(fig)

    # ---- 2. Open-market index vs closed ----------------------------------
    idx = (
        hist.groupby(["timestamp_utc", "model_class"])["market_blended"]
        .median()
        .unstack("model_class")
    )
    fig, ax = plt.subplots(figsize=(11, 5))
    if "open" in idx:
        ax.plot(idx.index, idx["open"], "-o", markersize=3,
                label="Open-weights market index (median)")
    if "closed" in idx:
        ax.plot(idx.index, idx["closed"], "--s", markersize=3,
                label="Closed frontier (median sticker)")
    ax.set_ylabel("Blended $/M tokens")
    ax.set_title("Inferred market cost of frontier compute: open vs closed")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(os.path.join(CHART_DIR, "open_vs_closed.png"), dpi=140)
    plt.close(fig)

    # ---- 3. Regional comparison (fair: same model + quantization) --------
    latest_ts = df["timestamp_utc"].max()
    snap = df[df["timestamp_utc"] == latest_ts].copy()
    open_snap = snap[snap["model_class"] == "open"]

    # Only compare regions on model+quant combos served in >=2 regions
    combo_regions = open_snap.groupby(["model", "quantization"])["region"].nunique()
    fair = combo_regions[combo_regions >= 2].index
    fair_rows = open_snap.set_index(["model", "quantization"])
    fair_rows = fair_rows[fair_rows.index.isin(fair)].reset_index()

    region_stats = pd.DataFrame()
    if not fair_rows.empty:
        # Normalize each observation by its model+quant median, then compare
        med = fair_rows.groupby(["model", "quantization"])["blended_usd_per_m"] \
                       .transform("median")
        fair_rows["rel_price"] = fair_rows["blended_usd_per_m"] / med
        region_stats = (
            fair_rows.groupby("region")
            .agg(n_offers=("rel_price", "size"),
                 rel_price_median=("rel_price", "median"),
                 abs_blended_median=("blended_usd_per_m", "median"))
            .sort_values("rel_price_median")
        )
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.bar(region_stats.index, region_stats["rel_price_median"])
        ax.axhline(1.0, color="gray", ls="--", lw=1)
        ax.set_ylabel("Median price vs same-model/quant median (1.0 = parity)")
        ax.set_title("Regional price level, controlled for model + quantization")
        fig.tight_layout()
        fig.savefig(os.path.join(CHART_DIR, "region_comparison.png"), dpi=140)
        plt.close(fig)

    # ---- 4. Markdown summary ---------------------------------------------
    lines = [
        "# Token Price Tracker — Summary",
        f"_Last updated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
        f"latest snapshot: {latest_ts} · "
        f"{df['timestamp_utc'].nunique()} snapshots since "
        f"{df['timestamp_utc'].min():%Y-%m-%d}_",
        "",
        "## Latest market cost per model (cheapest-3 avg, blended 3:1)",
        "",
        "| Model | Class | $/M blended | Cheapest provider | Providers |",
        "|---|---|---|---|---|",
    ]
    for (model, mclass), g in snap.groupby(["model", "model_class"]):
        cheapest = g.nsmallest(1, "blended_usd_per_m").iloc[0]
        mc = market_cost(g)
        lines.append(
            f"| {model} | {mclass} | ${mc:,.2f} | "
            f"{cheapest['provider']} (${cheapest['blended_usd_per_m']:,.2f}) | "
            f"{len(g)} |"
        )

    open_med = snap[snap.model_class == "open"]["blended_usd_per_m"].median()
    closed_med = snap[snap.model_class == "closed"]["blended_usd_per_m"].median()
    if pd.notna(open_med) and pd.notna(closed_med) and open_med > 0:
        lines += ["",
                  f"**Closed/open price multiple right now: "
                  f"{closed_med / open_med:.1f}x** "
                  f"(closed median ${closed_med:,.2f} vs open ${open_med:,.2f})"]

    lines += ["", "## Regional comparison (open models, same model+quant only)", ""]
    if region_stats.empty:
        lines.append("_Not enough multi-region offers yet for a fair comparison. "
                     "This fills in as history accumulates._")
    else:
        lines += ["| Region | Offers | Rel. price (1.0=parity) | Median $/M |",
                  "|---|---|---|---|"]
        for region, r in region_stats.iterrows():
            lines.append(f"| {region} | {int(r.n_offers)} | "
                         f"{r.rel_price_median:.2f} | "
                         f"${r.abs_blended_median:,.2f} |")
        lines += ["",
                  "_Caveat: this measures offer **price**, not underlying cost; "
                  "regional gaps can reflect margin strategy, subsidies, or "
                  "capacity, not just electricity and hardware costs._"]

    lines += ["", "## Charts", "",
              "![Price history](charts/price_history.png)",
              "![Open vs closed](charts/open_vs_closed.png)"]
    if not region_stats.empty:
        lines.append("![Regions](charts/region_comparison.png)")

    with open(SUMMARY_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Wrote {SUMMARY_PATH} and charts to {CHART_DIR}/")


if __name__ == "__main__":
    main()

