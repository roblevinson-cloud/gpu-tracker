"""
Builds one daily-index CSV and one chart per GPU type, plus a combined
chart of all four indices. Robust to small datasets and mixed column
types. Prints a one-line diagnostic per GPU so you can see exactly
what got processed.
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

    try:
        df = pd.read_csv(log_path, parse_dates=["timestamp_utc"])
    except Exception as e:
        print(f"[{gpu}] could not read {log_path}: {e}")
        return None

    if df.empty:
        print(f"[{gpu}] file has no rows yet — skipping")
        return None

    # Force numeric even if the column got read as object (happens when
    # every early row had all-provider failures and left empty strings).
    df["overall_available"] = pd.to_numeric(df["overall_available"], errors="coerce")
    df = df.dropna(subset=["overall_available", "timestamp_utc"])

    if df.empty:
        print(f"[{gpu}] no successful checks yet — skipping")
        return None

    # Daily availability % = share of checks that day where a GPU was rentable.
    daily = (
        df.set_index("timestamp_utc")["overall_available"]
        .resample("D").mean().mul(100)
        .rename("availability_pct").to_frame()
    )
    # 30-day smoothed line. min_periods=2 so we see *something* early on;
    # values before day 30 are just partial averages, not a true 30d MA yet.
    daily["availability_30d_avg"] = (
        daily["availability_pct"].rolling(30, min_periods=2).mean()
    )

    daily.to_csv(f"data/daily_index_{gpu}.csv")

    fig, ax = plt.subplots(figsize=(12, 6))
    color = GPU_COLORS.get(gpu, "#333333")
    ax.plot(daily.index, daily["availability_pct"],
            color="lightgray", lw=1, marker="o", markersize=3,
            label=f"{gpu.upper()} daily %")
    ax.plot(daily.index, daily["availability_30d_avg"],
            color=color, lw=2.5,
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

    n_days = len(daily)
    date_range = f"{daily.index.min().date()} → {daily.index.max().date()}"
    latest = daily["availability_pct"].iloc[-1]
    print(f"[{gpu}] OK — {len(df)} checks over {n_days} day(s) "
          f"({date_range}). Latest daily: {latest:.1f}%")
    return gpu, daily


def build_combined(results):
    if not results:
        print("nothing to combine")
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
    print("combined chart written")


def cleanup_old_single_gpu_files():
    """Remove leftovers from the single-GPU version so you don't confuse
    them with the new multi-GPU outputs."""
    for stale in ["data/daily_index.csv", "data/index_chart.png"]:
        if os.path.exists(stale):
            os.remove(stale)
            print(f"removed stale file: {stale}")


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    cleanup_old_single_gpu_files()

    log_files = sorted(glob.glob("data/availability_log_*.csv"))
    if not log_files:
        print("no availability_log_*.csv files found in data/. "
              "Has the poll workflow run at least once?")
    results = []
    for path in log_files:
        out = build_one(path)
        if out is not None:
            results.append(out)
    build_combined(results)
    print(f"\nDone. Built {len(results)} indices.")
