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


# ============================ DESIGN SYSTEM ============================
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

INK     = "#1C2B29"
MUTED   = "#66756F"
FAINT   = "#9AA6A1"
PAPER   = "#FBFCFB"
GRID    = "#E7ECE9"
PALETTE = ["#14B8A9", "#7C6FDE", "#E2703A", "#3B82C4",
           "#C9962E", "#3F9E6E", "#C75D9C", "#E24B4A", "#7A8894"]

plt.rcParams.update({
    "figure.facecolor": PAPER,
    "axes.facecolor": PAPER,
    "savefig.facecolor": PAPER,
    "font.family": "DejaVu Sans",
    "font.size": 13,
    "axes.edgecolor": GRID,
    "axes.linewidth": 1.0,
    "axes.titlesize": 21,
    "axes.titleweight": "bold",
    "axes.titlepad": 30,
    "axes.labelsize": 13,
    "axes.labelcolor": MUTED,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "text.color": INK,
})

PCT_FMT = FuncFormatter(lambda v, _: f"{v:.0f}%")
USD_FMT = FuncFormatter(lambda v, _: (f"${v:,.2f}" if abs(v) < 20 else f"${v:,.0f}"))
NUM_FMT = FuncFormatter(lambda v, _: f"{v:,.0f}")


def style_axis(ax, ylabel="", yfmt=None, pct=False):
    """House style: open frame, horizontal grid only, tidy dates."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.grid(axis="x", visible=False)
    ax.tick_params(length=0)
    if ylabel:
        ax.set_ylabel(ylabel)
    if pct:
        ax.yaxis.set_major_formatter(PCT_FMT)
    elif yfmt is not None:
        ax.yaxis.set_major_formatter(yfmt)
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))


def title_block(ax, title, subtitle=""):
    ax.set_title(title, loc="left", color=INK)
    if subtitle:
        ax.annotate(subtitle, xy=(0, 1), xycoords="axes fraction",
                    xytext=(0, 7), textcoords="offset points",
                    fontsize=12.5, color=MUTED, va="bottom", ha="left",
                    annotation_clip=False)


def source_note(fig, text="Source: OpenRouter (openrouter.ai/rankings)"):
    fig.text(0.006, 0.006, text, ha="left", fontsize=9.5, color=FAINT)


def direct_labels(ax, entries, room=0.22):
    """Label lines at their right endpoints instead of using a legend.
    entries: list of (label, x_end, y_end, color). Nudges overlaps apart."""
    entries = [e for e in entries if e[1] is not None and e[2] is not None]
    if not entries:
        return
    x0, x1 = ax.get_xlim()
    ax.set_xlim(x0, x1 + (x1 - x0) * room)
    y0, y1 = ax.get_ylim()
    gap = (y1 - y0) * 0.065
    entries.sort(key=lambda e: e[2])
    placed = []
    for label, x, y, color in entries:
        yy = y
        if placed and yy - placed[-1] < gap:
            yy = placed[-1] + gap
        placed.append(yy)
        ax.annotate("  " + label, xy=(mdates.date2num(x), y),
                    xytext=(mdates.date2num(x), yy),
                    fontsize=13, fontweight=600, color=color, va="center")
        ax.plot([x], [y], "o", ms=6, color=color, zorder=5)


def multiline(ax, frame_or_series, colors=None, lw=2.8):
    """Plot columns of a DataFrame with house palette and direct labels."""
    cols = list(frame_or_series.columns)
    if len(cols) > 9:
        cols = cols[:9]
    ends = []
    for i, c in enumerate(cols):
        s = frame_or_series[c].dropna()
        if s.empty:
            continue
        color = (colors or PALETTE)[i % len(PALETTE)]
        ax.plot(s.index, s, lw=lw, color=color, solid_capstyle="round")
        ends.append((str(c), s.index[-1], float(s.iloc[-1]), color))
    return ends
# ======================================================================


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

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    color = GPU_COLORS.get(gpu, INK)
    avail = daily["availability_pct"].dropna()
    sm = daily["availability_30d_avg"].dropna()
    ax.plot(avail.index, avail, color=GRID, lw=1.4)
    ax.plot(sm.index, sm, color=color, lw=3, solid_capstyle="round")
    ax.set_ylim(-2, 108)
    style_axis(ax, "Available under price cap", pct=True)
    ends = [("availability", sm.index[-1], float(sm.iloc[-1]), color)] if len(sm) else []

    ax2 = ax.twinx()
    ax2.set_ylim(bottom=0)
    if daily["lowest_price"].notna().any():
        lp = daily["lowest_price"].dropna()
        ax2.plot(lp.index, lp, color=INK, lw=2)
        ends.append((f"low ${lp.iloc[-1]:.2f}", lp.index[-1], float(lp.iloc[-1]), INK))
    if "median_price" in daily.columns and daily["median_price"].notna().any():
        mp = daily["median_price"].dropna()
        ax2.plot(mp.index, mp, color=FAINT, lw=2, linestyle=(0, (4, 3)))
        ends.append((f"median ${mp.iloc[-1]:.2f}", mp.index[-1], float(mp.iloc[-1]), FAINT))
    for side in ("top", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.spines["right"].set_visible(False)
    ax2.grid(visible=False)
    ax2.tick_params(length=0)
    ax2.set_ylabel("$ per GPU-hour", color=MUTED)
    ax2.yaxis.set_major_formatter(USD_FMT)

    if ends:
        direct_labels(ax, ends[:1])
        direct_labels(ax2, ends[1:], room=0)
    title_block(ax, f"{gpu.upper()} availability & price",
                f"Share of checks rentable under cap (left, {freq_label}) · lowest offer and Vast median $/GPU-hr (right)")
    source_note(fig, "3FR-style index · data: Vast.ai, Lambda, RunPod")
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

    color = GPU_COLORS.get(gpu, INK)
    fig, ax1 = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    g_all = daily["vast_gpus"].dropna()
    g_cap = daily["vast_gpus_under_cap"].dropna()
    ax1.plot(g_all.index, g_all, color=color, lw=3, solid_capstyle="round")
    ax1.plot(g_cap.index, g_cap, color=color, lw=2, linestyle=(0, (4, 3)))
    ax1.set_ylim(bottom=0)
    style_axis(ax1, "GPUs listed", yfmt=NUM_FMT)
    ends1 = []
    if len(g_all):
        ends1.append((f"listed {g_all.iloc[-1]:,.0f}", g_all.index[-1], float(g_all.iloc[-1]), color))
    if len(g_cap):
        ends1.append((f"under cap {g_cap.iloc[-1]:,.0f}", g_cap.index[-1], float(g_cap.iloc[-1]), color))

    ax2 = ax1.twinx()
    mpx = daily["vast_median_price"].dropna()
    ends2 = []
    if len(mpx):
        ax2.plot(mpx.index, mpx, color=FAINT, lw=2)
        ends2.append((f"median ${mpx.iloc[-1]:.2f}", mpx.index[-1], float(mpx.iloc[-1]), FAINT))
    for side in ("top", "left", "bottom", "right"):
        ax2.spines[side].set_visible(False)
    ax2.grid(visible=False)
    ax2.tick_params(length=0)
    ax2.set_ylabel("$ per GPU-hour", color=MUTED)
    ax2.yaxis.set_major_formatter(USD_FMT)
    ax2.set_ylim(bottom=0)

    direct_labels(ax1, ends1)
    direct_labels(ax2, ends2, room=0)
    title_block(ax1, f"{gpu.upper()} visible supply",
                "Deduped GPUs listed on Vast.ai at any price vs under the cap · median $/GPU-hr (right)")
    source_note(fig, "data: Vast.ai order book")
    fig.savefig(f"data/supply_chart_{gpu}.png", dpi=150)
    plt.close(fig)

    latest = daily["vast_gpus"].dropna().iloc[-1]
    print(f"[{gpu}] supply OK — latest visible GPUs (daily avg): {latest:.0f}")
    return gpu, daily


# --------------------------- combined --------------------------------

def combined_availability(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ends = []
    for gpu, daily in results:
        s = daily["availability_30d_avg"].dropna()
        if s.empty:
            continue
        c = GPU_COLORS.get(gpu, INK)
        ax.plot(s.index, s, color=c, lw=3, solid_capstyle="round")
        ends.append((gpu.upper(), s.index[-1], float(s.iloc[-1]), c))
    ax.set_ylim(-2, 108)
    style_axis(ax, "Available under price cap", pct=True)
    direct_labels(ax, ends, room=0.14)
    title_block(ax, "GPU availability index — all vintages",
                "Smoothed share of checks with a GPU rentable under each cap")
    source_note(fig, "data: Vast.ai, Lambda, RunPod")
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
        2, 1, figsize=(10.5, 9), sharex=True, constrained_layout=True,
        gridspec_kw={"height_ratios": [2.2, 1]})
    ax1.plot(t.index, t, color=GRID, lw=1.4)
    ma = ma7.dropna()
    ax1.plot(ma.index, ma, color=PALETTE[4], lw=3, solid_capstyle="round")
    ax1.set_ylim(bottom=0)
    style_axis(ax1, "Tokens per day (trillions)")
    if len(ma):
        direct_labels(ax1, [(f"{ma.iloc[-1]:.2f}T/day", ma.index[-1],
                             float(ma.iloc[-1]), PALETTE[4])], room=0.12)
    title_block(ax1, "OpenRouter platform token volume",
                "Daily total (faint) and 7-day average")

    g = growth30.dropna()
    ax2.axhline(0, color=FAINT, lw=1)
    ax2.plot(g.index, g, color=INK, lw=2)
    style_axis(ax2, "30-day growth", pct=True)
    if len(g):
        direct_labels(ax2, [(f"{g.iloc[-1]:+.0f}%", g.index[-1],
                             float(g.iloc[-1]), INK)], room=0.12)
    source_note(fig)
    fig.savefig("data/tokens_chart.png", dpi=150)
    plt.close(fig)

    # Log-scale variant: exponential growth reads as a straight line
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10.5, 9), sharex=True, constrained_layout=True,
        gridspec_kw={"height_ratios": [2.2, 1]})
    pos = t[t > 0]
    ax1.plot(pos.index, pos, color=GRID, lw=1.4)
    map_ = ma[ma > 0]
    ax1.plot(map_.index, map_, color=PALETTE[4], lw=3, solid_capstyle="round")
    ax1.set_yscale("log")
    style_axis(ax1, "Tokens per day (trillions, log scale)")
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    if len(map_):
        direct_labels(ax1, [(f"{map_.iloc[-1]:.2f}T/day", map_.index[-1],
                             float(map_.iloc[-1]), PALETTE[4])], room=0.12)
    title_block(ax1, "OpenRouter platform token volume",
                "Log scale — constant growth rate appears as a straight line")
    ax2.axhline(0, color=FAINT, lw=1)
    ax2.plot(g.index, g, color=INK, lw=2)
    style_axis(ax2, "30-day growth", pct=True)
    source_note(fig)
    fig.savefig("data/tokens_chart_log.png", dpi=150)
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

    top = prov.iloc[-7:].sum().nlargest(8).index

    smoothed = (prov / 1e12).rolling(7, min_periods=3).mean()

    # --- line version ---
    fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
    ends = multiline(ax, smoothed[list(top)])
    ax.set_ylim(bottom=0)
    style_axis(ax, "Tokens per day (trillions)")
    direct_labels(ax, ends, room=0.18)
    title_block(ax, "Token volume by provider",
                "7-day average, top providers by recent volume")
    source_note(fig)
    fig.savefig("data/providers_chart.png", dpi=150)
    plt.close(fig)

    # log-scale line variant
    fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
    logframe = smoothed[list(top)].where(smoothed[list(top)] > 0)
    ends = multiline(ax, logframe)
    ax.set_yscale("log")
    style_axis(ax, "Tokens per day (trillions, log scale)")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    direct_labels(ax, ends, room=0.18)
    title_block(ax, "Token volume by provider",
                "Log scale — parallel lines mean equal growth rates")
    source_note(fig)
    fig.savefig("data/providers_chart_log.png", dpi=150)
    plt.close(fig)

    # --- stacked version (top 8 + everything else, so height = total) ---
    rest = smoothed.drop(columns=top).sum(axis=1)
    stack_df = smoothed[list(top)].copy()
    stack_df["all others"] = rest
    stack_df = stack_df.fillna(0)
    fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
    stack_colors = (PALETTE * 2)[:len(stack_df.columns) - 1] + [GRID]
    ax.stackplot(stack_df.index, [stack_df[c] for c in stack_df.columns],
                 labels=list(stack_df.columns), colors=stack_colors,
                 alpha=0.92, linewidth=0)
    ax.set_ylim(bottom=0)
    style_axis(ax, "Tokens per day (trillions)")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1], loc="upper left", fontsize=12,
              frameon=False, ncol=2)
    title_block(ax, "Token volume by provider — stacked",
                "Total height = whole platform · 7-day average")
    source_note(fig)
    fig.savefig("data/providers_chart_stacked.png", dpi=150)
    plt.close(fig)
    print(f"[providers] OK — top: {', '.join(top[:4])}...")

    # Drill-down: top models within selected providers
    for pname in PROVIDER_DRILLDOWNS:
        sub = df[df["provider"] == pname]
        if sub.empty:
            continue
        models = (sub.groupby(["date", "model"])["tokens"].sum()
                    .unstack(fill_value=0).sort_index())
        top_m = models.iloc[-7:].sum().nlargest(8).index
        sm = (models / 1e12).rolling(7, min_periods=3).mean()

        # line version
        fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
        named = sm[list(top_m)].copy()
        named.columns = [c.split("/", 1)[-1] for c in named.columns]
        ends = multiline(ax, named)
        ax.set_ylim(bottom=0)
        style_axis(ax, "Tokens per day (trillions)")
        direct_labels(ax, ends, room=0.30)
        title_block(ax, f"{pname.capitalize()} — token volume by model",
                    "7-day average, top models by recent volume")
        source_note(fig)
        fig.savefig(f"data/provider_{pname}_models_chart.png", dpi=150)
        plt.close(fig)

        # log-scale line variant
        fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
        ends = multiline(ax, named.where(named > 0))
        ax.set_yscale("log")
        style_axis(ax, "Tokens per day (trillions, log scale)")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        direct_labels(ax, ends, room=0.30)
        title_block(ax, f"{pname.capitalize()} — token volume by model",
                    "Log scale — parallel lines mean equal growth rates")
        source_note(fig)
        fig.savefig(f"data/provider_{pname}_models_chart_log.png", dpi=150)
        plt.close(fig)

        # stacked version (top models + rest, height = provider total)
        rest_m = sm.drop(columns=top_m).sum(axis=1)
        sdf = sm[list(top_m)].copy()
        sdf.columns = [c.split("/", 1)[-1] for c in sdf.columns]
        if rest_m.abs().sum() > 0:
            sdf["all others"] = rest_m
        sdf = sdf.fillna(0)
        fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
        n_named = len(sdf.columns) - (1 if "all others" in sdf.columns else 0)
        s_colors = (PALETTE * 2)[:n_named] + ([GRID] if "all others" in sdf.columns else [])
        ax.stackplot(sdf.index, [sdf[c] for c in sdf.columns],
                     labels=list(sdf.columns), colors=s_colors,
                     alpha=0.92, linewidth=0)
        ax.set_ylim(bottom=0)
        style_axis(ax, "Tokens per day (trillions)")
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles[::-1], labels[::-1], loc="upper left", fontsize=12,
                  frameon=False, ncol=2)
        title_block(ax, f"{pname.capitalize()} — token volume by model, stacked",
                    "Total height = provider total · 7-day average")
        source_note(fig)
        fig.savefig(f"data/provider_{pname}_models_chart_stacked.png", dpi=150)
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

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    out_s = daily["avg_completion_usd_per_m"].dropna()
    in_s = daily["avg_prompt_usd_per_m"].dropna()
    ax.plot(out_s.index, out_s, color=PALETTE[6], lw=3, solid_capstyle="round")
    ax.plot(in_s.index, in_s, color=PALETTE[6], lw=2, alpha=0.55,
            linestyle=(0, (4, 3)))
    ax.set_ylim(bottom=0)
    style_axis(ax, "$ per million tokens", yfmt=USD_FMT)
    ends = []
    if len(out_s):
        ends.append((f"output ${out_s.iloc[-1]:.2f}", out_s.index[-1],
                     float(out_s.iloc[-1]), PALETTE[6]))
    if len(in_s):
        ends.append((f"input ${in_s.iloc[-1]:.2f}", in_s.index[-1],
                     float(in_s.iloc[-1]), FAINT))
    direct_labels(ax, ends, room=0.16)
    title_block(ax, "Token-weighted average price",
                "What the market actually pays per million tokens, weighted by usage")
    source_note(fig)
    fig.savefig("data/pricing_chart.png", dpi=150)
    plt.close(fig)
    print(f"[pricing] OK — {len(daily)} days; latest weighted output price "
          f"${daily['avg_completion_usd_per_m'].iloc[-1]:.2f}/M")


def build_price_history():
    """Per-model price over time for the highest-volume models, plus a
    repricing table: each model's first vs latest observed price."""
    if not os.path.exists("data/model_prices.csv"):
        return
    prices = pd.read_csv("data/model_prices.csv", parse_dates=["date"])
    for c in ["prompt_usd_per_m", "completion_usd_per_m"]:
        prices[c] = pd.to_numeric(prices[c], errors="coerce")
    prices = prices.dropna(subset=["date", "completion_usd_per_m"])
    if prices.empty:
        return
    # ignore free-tier zero-price rows for trend purposes
    priced = prices[prices["completion_usd_per_m"] > 0]

    # Which models to chart: top by recent token volume, that have prices
    tok = _load_by_model()
    if tok is not None and not tok.empty:
        recent = tok[tok["date"] >= tok["date"].max() - pd.Timedelta(days=7)]
        vol_rank = recent.groupby("model")["tokens"].sum().sort_values(ascending=False)
        watch = [m for m in vol_rank.index if m in set(priced["model"])][:10]
    else:
        latest = priced[priced["date"] == priced["date"].max()]
        watch = latest.nlargest(10, "completion_usd_per_m")["model"].tolist()
    if not watch:
        return

    piv = (priced[priced["model"].isin(watch)]
           .pivot_table(index="date", columns="model",
                        values="completion_usd_per_m", aggfunc="last")
           .sort_index())

    fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
    ends = []
    for i, m in enumerate(watch):
        if m not in piv.columns:
            continue
        s = piv[m].dropna()
        if s.empty:
            continue
        c = PALETTE[i % len(PALETTE)]
        ax.plot(s.index, s, lw=2.4, color=c, drawstyle="steps-post",
                solid_capstyle="round")
        ends.append((m.split("/", 1)[-1], s.index[-1], float(s.iloc[-1]), c))
    ax.set_ylim(bottom=0)
    style_axis(ax, "$ per million output tokens", yfmt=USD_FMT)
    direct_labels(ax, ends, room=0.30)
    title_block(ax, "Output price history — highest-volume models",
                "List price over time; steps mark repricings")
    source_note(fig)
    fig.savefig("data/price_history_chart.png", dpi=150)
    plt.close(fig)

    # Repricing table: first vs last observed price per model
    changes = []
    for m, g in priced.groupby("model"):
        g = g.sort_values("date")
        first, last = g.iloc[0], g.iloc[-1]
        if first["date"] == last["date"]:
            continue
        pct = (last["completion_usd_per_m"] / first["completion_usd_per_m"] - 1) * 100
        changes.append([m, first["date"].date(), first["completion_usd_per_m"],
                        last["date"].date(), last["completion_usd_per_m"],
                        round(pct, 1)])
    if changes:
        ch = pd.DataFrame(changes, columns=[
            "model", "first_date", "first_price", "last_date",
            "last_price", "pct_change"]).sort_values("pct_change")
        ch.to_csv("data/price_changes.csv", index=False)
        cut = (ch["pct_change"] < -1).sum()
        raised = (ch["pct_change"] > 1).sum()
        flat = len(ch) - cut - raised
        print(f"[price-history] {len(ch)} models with 2+ observations: "
              f"{cut} cut, {raised} raised, {flat} unchanged")


def build_perf():
    """Per-model charts of provider throughput and latency over time.
    Emits one two-panel PNG per leading model plus a manifest the
    dashboard reads to know which charts exist."""
    import json
    path = "data/perf_log.csv"
    if not os.path.exists(path):
        return
    df = pd.read_csv(path, parse_dates=["timestamp_utc"])
    df["throughput_tps"] = pd.to_numeric(df["throughput_tps"], errors="coerce")
    df["latency_s"] = pd.to_numeric(df["latency_s"], errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "model", "provider"])
    if df.empty:
        return

    span_days = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).days
    freq = "h" if span_days < 3 else "D"

    manifest = []
    # chart the models with the most observations
    for model in df["model"].value_counts().index[:8]:
        sub = df[df["model"] == model]
        # top 6 providers by observation count for readability
        provs = sub["provider"].value_counts().index[:6]
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 9.5), sharex=True,
                                       constrained_layout=True)
        drew = False
        ends1, ends2 = [], []
        for i, p in enumerate(provs):
            c = PALETTE[i % len(PALETTE)]
            ps = sub[sub["provider"] == p].set_index("timestamp_utc")
            tps = ps["throughput_tps"].resample(freq).mean().dropna()
            lat = ps["latency_s"].resample(freq).mean().dropna()
            if len(tps):
                ax1.plot(tps.index, tps, lw=2.4, color=c, solid_capstyle="round")
                ends1.append((p, tps.index[-1], float(tps.iloc[-1]), c))
                drew = True
            if len(lat):
                ax2.plot(lat.index, lat, lw=2.4, color=c, solid_capstyle="round")
                ends2.append((f"{lat.iloc[-1]:.1f}s", lat.index[-1],
                              float(lat.iloc[-1]), c))
        if not drew:
            plt.close(fig)
            continue
        ax1.set_ylim(bottom=0)
        style_axis(ax1, "Throughput (tokens/sec)", yfmt=NUM_FMT)
        direct_labels(ax1, ends1, room=0.20)
        title_block(ax1, f"{model} — provider performance",
                    "Tokens/sec by provider (top) · latency in seconds (bottom)")
        ax2.set_ylim(bottom=0)
        style_axis(ax2, "Latency (seconds)")
        direct_labels(ax2, ends2, room=0.20)
        source_note(fig)
        safe = model.replace("/", "_").replace(":", "_")
        fname = f"data/perf_{safe}_chart.png"
        fig.savefig(fname, dpi=150)
        plt.close(fig)
        manifest.append({"file": fname, "model": model})

    with open("data/perf_manifest.json", "w") as f:
        json.dump(manifest, f)
    print(f"[perf] charted {len(manifest)} models "
          f"({len(df)} observations)")


def combined_price(results):
    """All vintages' lowest offer price on one chart."""
    have = [(g, d) for g, d in results if "lowest_price" in d.columns
            and d["lowest_price"].notna().any()]
    if not have:
        return
    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ends = []
    for gpu, daily in have:
        s = daily["lowest_price"].dropna()
        if s.empty:
            continue
        c = GPU_COLORS.get(gpu, INK)
        ax.plot(s.index, s, color=c, lw=3, solid_capstyle="round")
        ends.append((f"{gpu.upper()} ${s.iloc[-1]:.2f}", s.index[-1], float(s.iloc[-1]), c))
    ax.set_ylim(bottom=0)
    style_axis(ax, "$ per GPU-hour", yfmt=USD_FMT)
    direct_labels(ax, ends, room=0.16)
    title_block(ax, "Lowest on-demand offer — all vintages",
                "Cheapest qualifying rental seen across providers each period")
    source_note(fig, "data: Vast.ai, Lambda, RunPod")
    fig.savefig("data/price_chart_combined.png", dpi=150)
    plt.close(fig)


def combined_supply(results):
    if not results:
        return
    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ends = []
    for gpu, daily in results:
        s = daily["vast_gpus"].dropna()
        if s.empty:
            continue
        c = GPU_COLORS.get(gpu, INK)
        ax.plot(s.index, s, color=c, lw=3, solid_capstyle="round")
        ends.append((f"{gpu.upper()} {s.iloc[-1]:,.0f}", s.index[-1], float(s.iloc[-1]), c))
    ax.set_ylim(bottom=0)
    style_axis(ax, "GPUs listed at any price", yfmt=NUM_FMT)
    direct_labels(ax, ends, room=0.16)
    title_block(ax, "Visible GPU supply — all vintages",
                "Deduped machines listed on Vast.ai, daily average")
    source_note(fig, "data: Vast.ai order book")
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
    build_price_history()
    build_perf()
    print(f"\nDone. {len(avail_results)} availability indices, "
          f"{len(supply_results)} supply charts.")
