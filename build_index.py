"""
Builds availability indices AND supply-depth charts.

Outputs per GPU (when data exists):
  data/daily_index_{gpu}.csv        availability % + 30d avg
  data/index_chart_{gpu}.png        availability chart
  data/daily_supply_{gpu}.csv       daily avg visible GPUs, prices
  data/supply_chart_{gpu}.png       supply depth + median price chart
Combined:
  data/index_chart_combined.png     all availability indices
  data/supply_chart_combined.png    all visible-GPU counts
"""

import glob
import os

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

GPU_COLORS = {
    "h100": "#2ab5ac",
    "h200": "#7f77dd",
    "b200": "#d85a30",
    "b300": "#e24b4a",
}


# ------------------------ availability index -------------------------

def build_availability(log_path):
    gpu = os.path.basename(log_path).replace("availability_log_", "").replace(".csv", "")
    try:
        df = pd.read_csv(log_path, parse_dates=["timestamp_utc"])
    except Exception as e:
        print(f"[{gpu}] could not read {log_path}: {e}")
        return None
    df["overall_available"] = pd.to_numeric(df["overall_available"], errors="coerce")
    df["cheapest_price"] = pd.to_numeric(df.get("cheapest_price"), errors="coerce")
    df = df.dropna(subset=["overall_available", "timestamp_utc"])
    if df.empty:
        print(f"[{gpu}] no successful availability checks yet — skipping")
        return None

    span_days = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).days
    freq, freq_label = ("h", "hourly") if span_days < 3 else ("D", "daily")
    daily = (
        df.set_index("timestamp_utc")["overall_available"]
        .resample(freq).mean().mul(100)
        .rename("availability_pct").to_frame()
    )
    window = 30 if freq == "D" else 24
    daily["availability_30d_avg"] = daily["availability_pct"].rolling(window, min_periods=2).mean()

    # Lowest offer price per period (min of cheapest_price across all
    # providers within each day/hour) — the continuous signal that keeps
    # moving after the binary index saturates at 100%.
    daily["lowest_price"] = (
        df.set_index("timestamp_utc")["cheapest_price"].resample(freq).min()
    )
    # Median Vast per-GPU price, if the supply log exists.
    supply_path = f"data/supply_log_{gpu}.csv"
    if os.path.exists(supply_path):
        try:
            sup = pd.read_csv(supply_path, parse_dates=["timestamp_utc"])
            sup["vast_median_price"] = pd.to_numeric(
                sup["vast_median_price"], errors="coerce")
            daily["median_price"] = (
                sup.set_index("timestamp_utc")["vast_median_price"]
                .resample(freq).mean()
            )
        except Exception as e:
            print(f"[{gpu}] supply prices unavailable: {e}")

    daily.to_csv(f"data/daily_index_{gpu}.csv")

    fig, ax = plt.subplots(figsize=(12, 6))
    color = GPU_COLORS.get(gpu, "#333333")
    ax.plot(daily.index, daily["availability_pct"], color="lightgray", lw=1,
            marker="o", markersize=3, label=f"{gpu.upper()} {freq_label} %")
    ax.plot(daily.index, daily["availability_30d_avg"], color=color, lw=2.5,
            marker="o", markersize=2,
            label=f"{gpu.upper()} smoothed" if freq == "h" else f"{gpu.upper()} 30-day average")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(-2, 102)
    ax.grid(alpha=0.3)

    # Right axis: prices (the 3Fourteen chart-1 layout)
    ax2 = ax.twinx()
    if daily["lowest_price"].notna().any():
        ax2.plot(daily.index, daily["lowest_price"], color="#555555", lw=1.8,
                 marker="o", markersize=2, label="Lowest offer $/GPU-hr (right)")
    if "median_price" in daily.columns and daily["median_price"].notna().any():
        ax2.plot(daily.index, daily["median_price"], color="#AAAAAA", lw=1.5,
                 linestyle="--", marker="o", markersize=2,
                 label="Vast median $/GPU-hr (right)")
    ax2.set_ylabel("Price per GPU-hour ($)")
    ax2.set_ylim(bottom=0)

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    ax.set_title(f"{gpu.upper()} On-Demand Availability & Price")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"data/index_chart_{gpu}.png", dpi=150)
    plt.close(fig)

    print(f"[{gpu}] availability OK — {len(df)} checks, "
          f"latest daily {daily['availability_pct'].iloc[-1]:.1f}%")
    return gpu, daily


# -------------------------- supply depth -----------------------------

def build_supply(log_path):
    gpu = os.path.basename(log_path).replace("supply_log_", "").replace(".csv", "")
    try:
        df = pd.read_csv(log_path, parse_dates=["timestamp_utc"])
    except Exception as e:
        print(f"[{gpu}] could not read {log_path}: {e}")
        return None

    for col in ["vast_machines", "vast_gpus", "vast_gpus_under_cap",
                "vast_min_price", "vast_median_price", "lambda_regions"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp_utc"])
    if df.empty or df["vast_gpus"].dropna().empty:
        print(f"[{gpu}] no supply data yet — skipping")
        return None

    span_days = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).days
    freq = "h" if span_days < 3 else "D"
    daily = df.set_index("timestamp_utc").resample(freq).mean(numeric_only=True)
    daily.to_csv(f"data/daily_supply_{gpu}.csv")

    color = GPU_COLORS.get(gpu, "#333333")
    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax1.plot(daily.index, daily["vast_gpus"], color=color, lw=2.5,
             marker="o", markersize=3, label="Visible GPUs on Vast (any price)")
    ax1.plot(daily.index, daily["vast_gpus_under_cap"], color=color, lw=1.5,
             linestyle="--", marker="o", markersize=3, label="Visible GPUs under price cap")
    ax1.set_ylabel("GPUs listed (daily avg)")
    ax1.set_ylim(bottom=0)
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(daily.index, daily["vast_median_price"], color="#888888", lw=1.5,
             marker="o", markersize=3, label="Median $/GPU-hr (right)")
    ax2.set_ylabel("Median price per GPU-hour ($)")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    ax1.set_title(f"{gpu.upper()} Visible Supply Depth (Vast.ai order book)")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"data/supply_chart_{gpu}.png", dpi=150)
    plt.close(fig)

    latest = daily["vast_gpus"].dropna().iloc[-1]
    print(f"[{gpu}] supply OK — latest visible GPUs (daily avg): {latest:.0f}")
    return gpu, daily


# --------------------------- combined --------------------------------

def combined_availability(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    for gpu, daily in results:
        ax.plot(daily.index, daily["availability_30d_avg"],
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5,
                marker="o", markersize=3, label=gpu.upper())
    ax.set_title("GPU Availability Index — All Vintages (smoothed)")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(-2, 102)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/index_chart_combined.png", dpi=150)
    plt.close(fig)


def build_tokens():
    """OpenRouter token growth index: daily platform totals, 7d average,
    and 30d growth rate. Two stacked panels in one PNG."""
    path = "data/tokens_daily.csv"
    if not os.path.exists(path):
        return
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except Exception as e:
        print(f"[tokens] could not read {path}: {e}")
        return
    df["total_tokens"] = pd.to_numeric(df["total_tokens"], errors="coerce")
    df = df.dropna(subset=["date", "total_tokens"]).set_index("date").sort_index()
    if df.empty:
        print("[tokens] no data yet")
        return

    t = df["total_tokens"] / 1e12  # trillions/day
    ma7 = t.rolling(7, min_periods=3).mean()
    growth30 = ma7.pct_change(30) * 100  # 30-day % change of the 7d avg

    out = pd.DataFrame({"tokens_T": t, "tokens_T_7d": ma7,
                        "growth_30d_pct": growth30})
    out.to_csv("data/tokens_index.csv")

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]})
    ax1.plot(t.index, t, color="lightgray", lw=1, label="Daily total")
    ax1.plot(ma7.index, ma7, color="#B8860B", lw=2.5, label="7-day average")
    ax1.set_ylabel("Tokens per day (trillions)")
    ax1.set_ylim(bottom=0)
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)
    ax1.set_title("OpenRouter Platform Token Volume")

    ax2.plot(growth30.index, growth30, color="#555555", lw=1.8)
    ax2.axhline(0, color="#999999", lw=1)
    ax2.set_ylabel("30-day growth (%)")
    ax2.grid(alpha=0.3)

    fig.text(0.99, 0.01,
             "Source: OpenRouter (openrouter.ai/rankings)",
             ha="right", fontsize=8, color="#888888")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/tokens_chart.png", dpi=150)
    plt.close(fig)

    latest = ma7.dropna()
    g = growth30.dropna()
    print(f"[tokens] OK — {len(df)} days; latest 7d avg "
          f"{latest.iloc[-1]:.2f}T/day"
          + (f", 30d growth {g.iloc[-1]:+.1f}%" if len(g) else ""))


# Kimi is published under Moonshot AI; both slug spellings included
# so whichever OpenRouter uses will match (missing ones are skipped).
PROVIDER_DRILLDOWNS = ["anthropic", "openai", "google", "deepseek",
                       "moonshotai", "moonshot"]


def _load_by_model():
    path = "data/tokens_by_model.csv"
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"])
    df["tokens"] = pd.to_numeric(df["tokens"], errors="coerce")
    df = df.dropna(subset=["date", "tokens"])
    df = df[df["model"] != "other"]
    df["provider"] = df["model"].str.split("/").str[0]
    return df


def build_providers():
    """Token volume by provider (top 8), 7d smoothed."""
    df = _load_by_model()
    if df is None or df.empty:
        return
    prov = (df.groupby(["date", "provider"])["tokens"].sum()
              .unstack(fill_value=0).sort_index())
    prov.to_csv("data/tokens_by_provider.csv")

    top = prov.iloc[-30:].sum().nlargest(8).index
    fig, ax = plt.subplots(figsize=(12, 6))
    for p in top:
        ma = (prov[p] / 1e12).rolling(7, min_periods=3).mean()
        ax.plot(ma.index, ma, lw=2, label=p)
    ax.set_title("OpenRouter Token Volume by Provider (7-day avg)")
    ax.set_ylabel("Tokens per day (trillions)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    fig.text(0.99, 0.01, "Source: OpenRouter (openrouter.ai/rankings)",
             ha="right", fontsize=8, color="#888888")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/providers_chart.png", dpi=150)
    plt.close(fig)
    print(f"[providers] OK — top: {', '.join(top[:4])}...")

    # Drill-down: top models within selected providers
    for pname in PROVIDER_DRILLDOWNS:
        sub = df[df["provider"] == pname]
        if sub.empty:
            continue
        models = (sub.groupby(["date", "model"])["tokens"].sum()
                    .unstack(fill_value=0).sort_index())
        top_m = models.iloc[-30:].sum().nlargest(6).index
        fig, ax = plt.subplots(figsize=(12, 6))
        for m in top_m:
            ma = (models[m] / 1e12).rolling(7, min_periods=3).mean()
            ax.plot(ma.index, ma, lw=2, label=m.split("/", 1)[-1])
        ax.set_title(f"{pname.capitalize()} — Token Volume by Model (7-day avg)")
        ax.set_ylabel("Tokens per day (trillions)")
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(alpha=0.3)
        fig.text(0.99, 0.01, "Source: OpenRouter (openrouter.ai/rankings)",
                 ha="right", fontsize=8, color="#888888")
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(f"data/provider_{pname}_models_chart.png", dpi=150)
        plt.close(fig)
        print(f"[providers] {pname}: charted {len(top_m)} models")


def build_pricing():
    """Token-weighted average price per million tokens across the
    platform: joins daily prices with daily token volumes."""
    if not os.path.exists("data/model_prices.csv"):
        return
    prices = pd.read_csv("data/model_prices.csv", parse_dates=["date"])
    for c in ["prompt_usd_per_m", "completion_usd_per_m"]:
        prices[c] = pd.to_numeric(prices[c], errors="coerce")
    tok = _load_by_model()
    if tok is None or prices.empty:
        return

    merged = prices.merge(tok[["date", "model", "tokens"]],
                          on=["date", "model"], how="inner")
    merged = merged.dropna(subset=["tokens"])
    merged = merged[merged["tokens"] > 0]
    if merged.empty:
        print("[pricing] no overlapping price/token days yet "
              "(token data lags one day; overlap starts tomorrow)")
        return

    def weighted(g):
        w = g["tokens"]
        return pd.Series({
            "avg_prompt_usd_per_m": (g["prompt_usd_per_m"] * w).sum() / w.sum(),
            "avg_completion_usd_per_m": (g["completion_usd_per_m"] * w).sum() / w.sum(),
        })

    daily = merged.groupby("date").apply(weighted, include_groups=False)
    daily.to_csv("data/pricing_index.csv")

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(daily.index, daily["avg_completion_usd_per_m"], color="#8B3A62",
            lw=2.5, marker="o", markersize=3, label="Output (completion) $/M tokens")
    ax.plot(daily.index, daily["avg_prompt_usd_per_m"], color="#B58BA5",
            lw=2, linestyle="--", marker="o", markersize=3,
            label="Input (prompt) $/M tokens")
    ax.set_title("Token-Weighted Average Price on OpenRouter ($/million tokens)")
    ax.set_ylabel("$ per million tokens")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.text(0.99, 0.01, "Source: OpenRouter (openrouter.ai/rankings)",
             ha="right", fontsize=8, color="#888888")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/pricing_chart.png", dpi=150)
    plt.close(fig)
    print(f"[pricing] OK — {len(daily)} days; latest weighted output price "
          f"${daily['avg_completion_usd_per_m'].iloc[-1]:.2f}/M")


def combined_price(results):
    """All vintages' lowest offer price on one chart."""
    have = [(g, d) for g, d in results if "lowest_price" in d.columns
            and d["lowest_price"].notna().any()]
    if not have:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    for gpu, daily in have:
        ax.plot(daily.index, daily["lowest_price"],
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5,
                marker="o", markersize=3, label=gpu.upper())
    ax.set_title("Lowest On-Demand Offer — All Vintages ($/GPU-hour)")
    ax.set_ylabel("Lowest offer per GPU-hour ($)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/price_chart_combined.png", dpi=150)
    plt.close(fig)


def combined_supply(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    for gpu, daily in results:
        ax.plot(daily.index, daily["vast_gpus"],
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5,
                marker="o", markersize=3, label=gpu.upper())
    ax.set_title("Visible GPU Supply on Vast.ai — All Vintages (daily avg)")
    ax.set_ylabel("GPUs listed at any price")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/supply_chart_combined.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)

    avail_results, supply_results = [], []
    for path in sorted(glob.glob("data/availability_log_*.csv")):
        out = build_availability(path)
        if out:
            avail_results.append(out)
    for path in sorted(glob.glob("data/supply_log_*.csv")):
        out = build_supply(path)
        if out:
            supply_results.append(out)

    combined_availability(avail_results)
    combined_price(avail_results)
    combined_supply(supply_results)
    build_tokens()
    build_providers()
    build_pricing()
    print(f"\nDone. {len(avail_results)} availability indices, "
          f"{len(supply_results)} supply charts.")
