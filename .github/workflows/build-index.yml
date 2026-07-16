"""
Builds one daily-index CSV and one chart per GPU type, plus a combined
chart showing all four availability indices on the same axes.
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


def build_one(log_path: str):
    gpu = os.path.basename(log_path).replace("availability_log_", "").replace(".csv", "")
    df = pd.read_csv(log_path, parse_dates=["timestamp_utc"])
    df = df.dropna(subset=["overall_available"])
    if df.empty:
        print(f"[{gpu}] no data yet, skipping")
        return None
    df["overall_available"] = df["overall_available"].astype(float)

    daily = (
        df.set_index("timestamp_utc")["overall_available"]
        .resample("D").mean().mul(100)
        .rename("availability_pct").to_frame()
    )
    daily["availability_30d_avg"] = (
        daily["availability_pct"].rolling(30, min_periods=5).mean()
    )
    daily.to_csv(f"data/daily_index_{gpu}.csv")

    fig, ax = plt.subplots(figsize=(12, 6))
    color = GPU_COLORS.get(gpu, "#333333")
    ax.plot(daily.index, daily["availability_pct"], color="lightgray", lw=1,
            label=f"{gpu.upper()} daily %")
    ax.plot(daily.index, daily["availability_30d_avg"], color=color, lw=2.5,
            label=f"{gpu.upper()} 30-day average")
    ax.set_title(f"{gpu.upper()} On-Demand Availability Index")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(0, 100)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"data/index_chart_{gpu}.png", dpi=150)
    plt.close(fig)
    return gpu, daily


def build_combined(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    for gpu, daily in results:
        ax.plot(daily.index, daily["availability_30d_avg"],
                color=GPU_COLORS.get(gpu, "#333"), lw=2.5, label=gpu.upper())
    ax.set_title("GPU Availability Index — All Vintages (30-day average)")
    ax.set_ylabel("% of checks with a GPU available")
    ax.set_ylim(0, 100)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig("data/index_chart_combined.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    results = []
    for path in sorted(glob.glob("data/availability_log_*.csv")):
        out = build_one(path)
        if out is not None:
            results.append(out)
    build_combined(results)
    print(f"\nBuilt {len(results)} indices. See data/ folder.")
