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
    df = df.dropna(subset=["overall_available", "timestamp_utc"])
    if df.empty:
        print(f"[{gpu}] no successful availability checks yet — skipping")
        return None

    daily = (
        df.set_index("timestamp_utc")["overall_available"]
        .resample("D").mean().mul(100)
        .rename("availability_pct").to_frame()
    )
    daily["availability_30d_avg"] = daily["availability_pct"].rolling(30, min_periods=2).mean()
    daily.to_csv(f"data/daily_index_{gpu}.csv")

    fig, ax = plt.subplots(figsize=(12, 6))
    color = GPU_COLORS.get(gpu, "#333333")
    ax.plot(daily.index, daily["availability_pct"], color="lightgray", lw=1,
            marker="o", markersize=3, label=f"{gpu.upper()} daily %")
    ax.plot(daily.index, daily["availability_30d_avg"], color=color, lw=2.5,
            label=f"{gpu.upper()} 30-day average")
    ax.set_title(f"{gpu.upper()} On-Demand Availability Index")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(-2, 102)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
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

    daily = df.set_index("timestamp_utc").resample("D").mean(numeric_only=True)
    daily.to_csv(f"data/daily_supply_{gpu}.csv")

    color = GPU_COLORS.get(gpu, "#333333")
    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax1.plot(daily.index, daily["vast_gpus"], color=color, lw=2.5,
             label="Visible GPUs on Vast (any price)")
    ax1.plot(daily.index, daily["vast_gpus_under_cap"], color=color, lw=1.5,
             linestyle="--", label="Visible GPUs under price cap")
    ax1.set_ylabel("GPUs listed (daily avg)")
    ax1.set_ylim(bottom=0)
    ax1.grid(alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(daily.index, daily["vast_median_price"], color="#888888", lw=1.5,
             label="Median $/GPU-hr (right)")
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
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5, label=gpu.upper())
    ax.set_title("GPU Availability Index — All Vintages (30-day average)")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(-2, 102)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("data/index_chart_combined.png", dpi=150)
    plt.close(fig)


def combined_supply(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    for gpu, daily in results:
        ax.plot(daily.index, daily["vast_gpus"],
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5, label=gpu.upper())
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
    combined_supply(supply_results)
    print(f"\nDone. {len(avail_results)} availability indices, "
          f"{len(supply_results)} supply charts.")
