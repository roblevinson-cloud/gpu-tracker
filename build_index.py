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

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import yaml
except ImportError:      # annotations/specs simply skip if pyyaml missing
    yaml = None


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


def direct_labels(ax, entries, room=0.22, fontsize=13, ms=6):
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
                    fontsize=fontsize, fontweight=600, color=color, va="center")
        ax.plot([x], [y], "o", ms=ms, color=color, zorder=5)


def _load_yaml(path):
    if yaml is None or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[yaml] could not parse {path}: {e}")
        return None


def load_events():
    """events.yml -> [(timestamp, label, {tags})]. Empty list if absent."""
    raw = _load_yaml("events.yml") or []
    events = []
    for ev in raw:
        try:
            d = pd.to_datetime(str(ev.get("date")))
            label = str(ev.get("label", "")).strip()
            tags = {str(t).lower() for t in (ev.get("show_on") or ["all"])}
            events.append((d, label, tags))
        except Exception as e:
            print(f"[events] skipping bad entry {ev}: {e}")
    return events


EVENTS = load_events()


def draw_events(ax, *tags):
    """Dashed vertical markers from events.yml on charts whose family
    matches the event's show_on list ('all' matches everything)."""
    if not EVENTS:
        return
    want = {t.lower() for t in tags} | {"all"}
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    for d, label, evtags in EVENTS:
        if not (evtags & want):
            continue
        x = mdates.date2num(d)
        if not (x0 <= x <= x1):
            continue
        ax.axvline(d, color=FAINT, lw=1.2, linestyle=(0, (2, 3)), zorder=1)
        if label:
            # escape $ so matplotlib doesn't treat it as mathtext
            ax.annotate(label.replace("$", r"\$"), xy=(x, y1), xycoords="data",
                        xytext=(4, -4), textcoords="offset points",
                        rotation=90, va="top", ha="left",
                        fontsize=9.5, color=MUTED, zorder=1,
                        annotation_clip=True)


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
    "a100": "#3f9e6e",
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
    if daily["lowest_price"].notna().any():
        lp = daily["lowest_price"].dropna()
        ax2.plot(lp.index, lp, color=INK, lw=2)
        ends.append((f"low ${lp.iloc[-1]:.2f}", lp.index[-1], float(lp.iloc[-1]), INK))
    if "median_price" in daily.columns and daily["median_price"].notna().any():
        mp = daily["median_price"].dropna()
        ax2.plot(mp.index, mp, color=FAINT, lw=2, linestyle=(0, (4, 3)))
        ends.append((f"median ${mp.iloc[-1]:.2f}", mp.index[-1], float(mp.iloc[-1]), FAINT))
    ax2.set_ylim(bottom=0)   # after plotting, so autoscale still works
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
    draw_events(ax, gpu, "gpu")
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
    draw_events(ax1, gpu, "gpu")
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
    draw_events(ax, "gpu")
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
    draw_events(ax1, "tokens")
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
    draw_events(ax1, "tokens")
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
    draw_events(ax, "prices")
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
    draw_events(ax, "prices")
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
    draw_events(ax, "gpu")
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
    draw_events(ax, "gpu")
    title_block(ax, "Visible GPU supply — all vintages",
                "Deduped machines listed on Vast.ai, daily average")
    source_note(fig, "data: Vast.ai order book")
    fig.savefig("data/supply_chart_combined.png", dpi=150)
    plt.close(fig)


# ------------------------- hardware value ----------------------------

def style_axis_numeric(ax, ylabel="", xlabel="", yfmt=None):
    """House style for non-date x-axes (e.g. hardware age)."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.grid(axis="x", visible=False)
    ax.tick_params(length=0)
    if ylabel:
        ax.set_ylabel(ylabel)
    if xlabel:
        ax.set_xlabel(xlabel)
    if yfmt is not None:
        ax.yaxis.set_major_formatter(yfmt)


# Vintages held out of the CROSS-VINTAGE value charts (price of compute,
# rental value vs age, value lenses, lens spread). A100 was added
# 2026-08-11 and pulled from these on 2026-08-15: it trades as a
# different market — trailing edge, no FP8/FP4, priced off bandwidth
# rather than compute — so it widened every spread (bandwidth 1.41x ->
# 1.60x, memory 1.16x -> 2.53x) and cut the age fit from R2 0.29 to 0.18
# without changing any conclusion. It stays fully tracked everywhere
# else: cards, per-GPU charts, utilisation, revenue, within-vintage.
VALUE_CHART_EXCLUDE = {"a100"}


def build_hardware_value(avail_results):
    """Two charts driven by gpu_specs.yml:
      data/pflop_price_chart.png    $/dense-FP8-PFLOP-hour over time
      data/depreciation_chart.png   $/PFLOP-hr vs hardware age + decay fit
    Uses median Vast price (falls back to lowest offer). Skips quietly
    if specs or price data are missing."""
    specs = _load_yaml("gpu_specs.yml") or {}
    if not specs or not avail_results:
        print("[hardware] no gpu_specs.yml or no availability data — skipping")
        return

    series = {}
    for gpu, daily in avail_results:
        sp = specs.get(gpu)
        if not sp:
            continue
        if gpu in VALUE_CHART_EXCLUDE:
            print(f"[hardware] {gpu}: held out of cross-vintage value "
                  f"charts (see VALUE_CHART_EXCLUDE)")
            continue
        if "fp16_dense_tflops" not in sp:
            print(f"[hardware] {gpu}: no FP16 spec — skipped")
            continue
        try:
            # FP16/BF16 dense is the cross-vintage backbone: the only
            # tensor format present AND full-rate on every part here.
            pflops = float(sp["fp16_dense_tflops"]) / 1000.0
            launch = pd.to_datetime(str(sp["launch"]))
        except Exception as e:
            print(f"[hardware] bad spec for {gpu}: {e}")
            continue
        price = None
        if "median_price" in daily.columns and daily["median_price"].notna().any():
            price = daily["median_price"]
        elif "lowest_price" in daily.columns and daily["lowest_price"].notna().any():
            price = daily["lowest_price"]
        if price is None:
            continue
        s = price.dropna()
        if s.empty:
            continue
        # "Best deployable" = what a stack would actually serve at:
        # NVFP4 on Blackwell, FP8 on Hopper, FP16 on Ampere. Where that
        # equals the FP16 backbone (A100), there is no second line.
        best = sp.get("fp4_dense_tflops") or sp.get("fp8_dense_tflops")
        series[gpu] = {"price": s, "usd_per_pflop": s / pflops,
                       "usd_per_pflop_fp4":
                           (s / (float(best) / 1000.0)) if best else None,
                       "launch": launch, "sp": sp}
    if not series:
        print("[hardware] no priced vintages — skipping")
        return

    wide = pd.DataFrame({g: d["usd_per_pflop"] for g, d in series.items()})
    wide.to_csv("data/hardware_value.csv")

    # --- Chart 1: price of compute, both normalizations ---
    # The precision you can actually serve at decides who looks cheap:
    # on FP8 the oldest part wins, on FP4 the newest does. Drawing both
    # keeps that fork visible instead of buried in a caption.
    fig, ax = plt.subplots(figsize=(10.5, 6.4), constrained_layout=True)
    ends = []
    has_fp4 = False
    for gpu, d in series.items():
        s = d["usd_per_pflop"]
        c = GPU_COLORS.get(gpu, INK)
        ax.plot(s.index, s, color=c, lw=3, solid_capstyle="round")
        ends.append((f"{gpu.upper()} ${s.iloc[-1]:.2f}", s.index[-1],
                     float(s.iloc[-1]), c))
        b = d.get("usd_per_pflop_fp4")
        if b is not None and not b.dropna().empty:
            has_fp4 = True
            b = b.dropna()
            ax.plot(b.index, b, color=c, lw=2.2, linestyle=(0, (4, 3)),
                    solid_capstyle="round")
            ends.append((f"{gpu.upper()} at FP4 ${b.iloc[-1]:.2f}",
                         b.index[-1], float(b.iloc[-1]), c))
    ax.set_ylim(bottom=0)
    style_axis(ax, "$ per PFLOP-hour", yfmt=USD_FMT)
    direct_labels(ax, ends, room=0.30)
    draw_events(ax, "gpu")
    sub = ("Solid = dense FP16/BF16, the one format all five run at full rate"
           " · dashed = best deployable precision (FP4 / FP8)")
    if has_fp4:
        ax.annotate("A100 has no dashed line: FP16 IS its best precision.\n"
                    "The gap between the two lines is the quantisation dividend.",
                    xy=(0.015, 0.06), xycoords="axes fraction",
                    ha="left", va="bottom", fontsize=11, color=MUTED)
    title_block(ax, "Price of compute — $/PFLOP-hour", sub)
    source_note(fig, "specs: gpu_specs.yml (dense, no sparsity) · data: Vast.ai order book")
    fig.savefig("data/pflop_price_chart.png", dpi=150)
    plt.close(fig)

    # --- Chart 2: depreciation — $/PFLOP-hr vs hardware age ---
    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    xs_all, ys_all, vint_all = [], [], []
    x_max = 0.0
    for gpu, d in series.items():
        s = d["usd_per_pflop"]
        age = np.asarray((s.index - d["launch"]).days) / 365.25
        c = GPU_COLORS.get(gpu, INK)
        ax.plot(age, s.values, color=c, lw=3, solid_capstyle="round")
        ax.plot([age[-1]], [s.iloc[-1]], "o", ms=6, color=c, zorder=5)
        ax.annotate(f"  {gpu.upper()}", xy=(age[-1], float(s.iloc[-1])),
                    fontsize=13, fontweight=600, color=c, va="center")
        xs_all.extend(age.tolist())
        ys_all.extend(s.values.tolist())
        vint_all.extend([gpu] * len(age))
        x_max = max(x_max, float(age.max()))

    xs = np.array(xs_all)
    ys = np.array(ys_all)
    vints = np.array(vint_all)
    mask = np.isfinite(xs) & np.isfinite(ys) & (ys > 0)
    fitted = ""
    if mask.sum() >= 10 and len(series) >= 2:
        logy = np.log(ys[mask])
        b, a = np.polyfit(xs[mask], logy, 1)

        # How much of the spread is actually AGE? Each vintage currently
        # spans only days of age, so this is a cross-section through a
        # handful of clusters whose height is set mostly by scarcity.
        # Report that honestly instead of implying a measured decay.
        ss_res = ((logy - (a + b * xs[mask])) ** 2).sum()
        ss_tot = ((logy - logy.mean()) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        # Does the decay survive dropping any single vintage? If one
        # vintage carries the whole slope, the number is an artifact.
        carriers = []
        for v in sorted(set(vints[mask])):
            m2 = mask & (vints != v)
            if m2.sum() >= 4 and len(set(vints[m2])) >= 2:
                b2, _ = np.polyfit(xs[m2], np.log(ys[m2]), 1)
                if b2 >= 0:
                    carriers.append(v.upper())

        xf = np.linspace(0, xs[mask].max() * 1.02, 120)
        trustworthy = b < 0 and r2 >= 0.70 and not carriers
        ax.plot(xf, np.exp(a + b * xf), color=INK if trustworthy else FAINT,
                lw=1.6, linestyle=(0, (5, 4)), zorder=2)

        if trustworthy:
            annual_pct = (1 - np.exp(b)) * 100
            half_life_mo = np.log(0.5) / b * 12
            fitted = (f"fit: −{annual_pct:.0f}%/yr · "
                      f"half-life ≈ {half_life_mo:.0f} months · R²={r2:.2f}")
            note, color = fitted, INK
        else:
            fitted = f"weak age signal (R²={r2:.2f})"
            note = f"age explains only {r2 * 100:.0f}% of the spread"
            if carriers:
                note += f" · slope depends entirely on {', '.join(carriers)}"
            note += "\nvintages are priced by scarcity, not age — see utilization"
            color = MUTED
        ax.annotate(note, xy=(0.97, 0.08), xycoords="axes fraction",
                    ha="right", va="bottom", fontsize=12,
                    fontweight=600 if trustworthy else 400, color=color)

    ax.set_xlim(0, x_max * 1.14)
    ax.set_ylim(bottom=0)
    style_axis_numeric(ax, "$ per dense-FP16 PFLOP-hour", yfmt=USD_FMT)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}y"))
    ax.annotate("hardware age since volume launch", xy=(0.5, -0.09),
                xycoords="axes fraction", ha="center", va="top",
                fontsize=11, color=MUTED, annotation_clip=False)
    title_block(ax, "Rental value vs hardware age",
                "Cross-section, not a measured decay: each vintage spans only weeks of age so far")
    source_note(fig, "specs & launch dates: gpu_specs.yml · data: Vast.ai order book")
    fig.savefig("data/depreciation_chart.png", dpi=150)
    plt.close(fig)

    # --- Chart 3: value lenses — which resource is the market pricing? ---
    # Same rental prices normalized three ways. The lens with the
    # flattest lines (smallest spread) is what buyers actually pay for.
    lenses = [
        ("PER UNIT OF COMPUTE",
         "$ / PFLOP-hr, best deployable",
         lambda sp: float(sp.get("fp4_dense_tflops")
                          or sp.get("fp8_dense_tflops")
                          or sp["fp16_dense_tflops"]) / 1000.0,
         FuncFormatter(lambda v, _: f"${v:.2f}")),
        ("PER GB OF HBM",
         "cents / GB-hr, usable",
         lambda sp: float(sp.get("hbm_gb_usable") or sp["hbm_gb"]),
         FuncFormatter(lambda v, _: f"{v * 100:.1f}¢")),
        ("PER TB/S BANDWIDTH",
         "$ / (TB/s)-hr",
         lambda sp: float(sp["bandwidth_tbps"]),
         FuncFormatter(lambda v, _: f"${v:.2f}")),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 5.6))
    fig.subplots_adjust(top=0.76, bottom=0.15, left=0.075, right=0.985,
                        wspace=0.42)
    spreads = []
    for ax, (name, unit, denom_fn, yfmt) in zip(axes, lenses):
        latest = {}
        ends = []
        for gpu, d in series.items():
            try:
                denom = denom_fn(d["sp"])
            except (KeyError, TypeError, ValueError):
                continue        # spec missing this field — skip the line
            if not denom:
                continue
            s = d["price"] / denom
            c = GPU_COLORS.get(gpu, INK)
            ax.plot(s.index, s, color=c, lw=2.4, solid_capstyle="round")
            latest[gpu] = float(s.iloc[-1])
            ends.append((gpu.upper(), s.index[-1], float(s.iloc[-1]), c))
        ax.set_ylim(bottom=0)
        style_axis(ax, unit, yfmt=yfmt)
        direct_labels(ax, ends, room=0.42, fontsize=10, ms=4)
        ax.set_title(name, loc="left", fontsize=11, fontweight=600,
                     color=MUTED, pad=8)
        if len(latest) >= 2:
            mx, mn = max(latest.values()), min(latest.values())
            if mn > 0:
                spread = mx / mn
                spreads.append(f"{name.split()[-1].lower()} {spread:.2f}x")
                ax.annotate(f"spread {spread:.2f}×",
                            xy=(0.05, 0.04), xycoords="axes fraction",
                            ha="left", va="bottom", fontsize=11,
                            fontweight=600, color=INK)
    fig.text(0.0075, 0.965, "Value lenses — what is the market pricing?",
             fontsize=21, fontweight="bold", color=INK, va="top", ha="left")
    fig.text(0.0075, 0.875, "Same rentals, three normalizations · "
             "the flattest panel (smallest spread) = the resource buyers actually pay for",
             fontsize=12.5, color=MUTED, va="top", ha="left")
    source_note(fig, "specs: gpu_specs.yml · data: Vast.ai order book")
    fig.savefig("data/value_lenses_chart.png", dpi=150)
    plt.close(fig)

    # --- Chart 4: lens spread over time ---
    # Same max/min ratio as above, recomputed for every day. A lens
    # trending toward 1.0x is becoming the market's pricing basis.
    spread_hist = {}
    for name, unit, denom_fn, _ in lenses:
        frame = {}
        for gpu, d in series.items():
            try:
                denom = denom_fn(d["sp"])
            except (KeyError, TypeError, ValueError):
                continue
            if denom:
                frame[gpu] = d["price"] / denom
        if len(frame) < 2:
            continue
        wide_l = pd.DataFrame(frame).dropna(how="all")
        # only days where every vintage priced, so the ratio is comparable
        wide_l = wide_l.dropna()
        if wide_l.empty:
            continue
        spread_hist[name.replace("PER ", "").lower()] = (
            wide_l.max(axis=1) / wide_l.min(axis=1))

    if spread_hist:
        sh = pd.DataFrame(spread_hist).sort_index()
        sh.to_csv("data/lens_spread.csv")
        fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
        ax.axhline(1.0, color=FAINT, lw=1.2, linestyle=(0, (3, 3)))
        ax.annotate("1.00× — vintages priced identically on this basis",
                    xy=(0.01, 1.0), xycoords=("axes fraction", "data"),
                    xytext=(0, 6), textcoords="offset points",
                    fontsize=10.5, color=MUTED, va="bottom")
        ends = multiline(ax, sh, colors=[PALETTE[2], PALETTE[0], PALETTE[3]],
                         lw=2.8)
        ax.set_ylim(bottom=0.9)
        style_axis(ax, "Spread across vintages (max ÷ min)",
                   yfmt=FuncFormatter(lambda v, _: f"{v:.2f}×"))
        direct_labels(ax, ends, room=0.26)
        draw_events(ax, "gpu")
        title_block(ax, "Which lens is converging?",
                    "Cross-vintage price spread per normalization · the lens nearest 1.00× is what the market prices")
        source_note(fig, "specs: gpu_specs.yml · data: Vast.ai order book")
        fig.savefig("data/lens_spread_chart.png", dpi=150)
        plt.close(fig)

    msg = f"[hardware] OK — {len(series)} vintages"
    if fitted:
        msg += f" · {fitted}"
    if spreads:
        msg += " · lens spreads: " + ", ".join(spreads)
    print(msg)


# --------------------- within-vintage depreciation ---------------------

# A slope can be statistically distinguishable from zero over three
# weeks and still be a useless annual number: annualising a 21-day
# drift multiplies signal and noise alike by ~17x, which is how you get
# "-1142%/yr". So the gate is PRECISION, not significance — publish the
# rate only once its 2-sigma interval is narrower than this many log
# units per year (0.15 ~= +/-15 percentage points on the annual rate).
RATE_CI_TARGET = 0.15


def _trend_stats(dates, prices):
    """OLS of log(price) on years, with the precision of the ANNUAL rate.
    The slope is unchanged by any per-vintage constant, so $/hr and
    $/PFLOP-hr give identical rates — the FP8-vs-FP4 argument does not
    apply within a single vintage."""
    t = np.asarray((dates - dates[0]).days, dtype=float) / 365.25
    y = np.log(np.asarray(prices, dtype=float))
    n = len(t)
    if n < 5 or t.max() <= 0:
        return None
    b, a = np.polyfit(t, y, 1)
    resid = y - (a + b * t)
    sxx = ((t - t.mean()) ** 2).sum()
    if n - 2 <= 0 or sxx <= 0:
        return None
    sigma = np.sqrt((resid ** 2).sum() / (n - 2))
    se_b = sigma / np.sqrt(sxx)
    # SE(slope) shrinks as n^-1.5 with daily spacing; invert for the n
    # that would bring the 2-sigma interval inside RATE_CI_TARGET
    need = (2 * sigma * np.sqrt(12) * 365.25 / RATE_CI_TARGET) ** (2 / 3)
    return {"a": a, "b": b, "se": se_b, "sigma": sigma, "n": n,
            "usable": 2 * se_b <= RATE_CI_TARGET, "need_days": need}


def build_within_vintage():
    """Each vintage against ITSELF over calendar time. The cross-vintage
    chart cannot separate ageing from generational deflation — age and
    architecture are perfectly collinear there. Tracking one vintage
    holds generation constant, so this is the identified estimate. The
    rate prints only once it clears 2 sigma; until then the panel says
    how much longer it needs."""
    rows = []
    vintages = ["a100", "h100", "h200", "b200", "b300"]
    ncol = 3 if len(vintages) > 4 else 2
    nrow = int(np.ceil(len(vintages) / ncol))
    height = 3.5 * nrow + 1.7
    fig, axes = plt.subplots(nrow, ncol, figsize=(10.5, height))
    fig.subplots_adjust(top=1 - 1.5 / height, bottom=0.085, left=0.075,
                        right=0.985, hspace=0.52, wspace=0.30)
    flat = np.atleast_1d(axes).ravel()
    for spare in flat[len(vintages):]:      # unused cells in the grid
        spare.set_axis_off()
    drew = False
    for ax, gpu in zip(flat, vintages):
        path = f"data/daily_index_{gpu}.csv"
        c = GPU_COLORS.get(gpu, INK)
        s = None
        if os.path.exists(path):
            try:
                d = pd.read_csv(path, parse_dates=["timestamp_utc"])
                mp = pd.to_numeric(d.get("median_price"), errors="coerce")
                d = d.assign(mp=mp).dropna(subset=["mp"])
                s = d.set_index("timestamp_utc")["mp"]
            except Exception as e:
                print(f"[within] {gpu}: {e}")
        if s is None or s.empty:
            ax.set_axis_off()
            continue
        drew = True
        ax.plot(s.index, s.values, color=c, lw=2.4, marker="o" if len(s) < 4
                else None, ms=5, solid_capstyle="round")

        if len(s) < 3:
            # A vintage added mid-flight: pin a readable window or the
            # date locator spans years around a single point.
            ax.set_xlim(s.index.min() - pd.Timedelta(days=3),
                        s.index.max() + pd.Timedelta(days=3))
            lo, hi = ax.get_ylim()
            ax.set_ylim(lo - (hi - lo) * 0.22, hi)
            ax.annotate("just started collecting", xy=(0.03, 0.04),
                        xycoords="axes fraction", ha="left", va="bottom",
                        fontsize=10.5, color=MUTED)
            ax.set_title(gpu.upper(), loc="left", fontsize=12,
                         fontweight=600, color=c, pad=6)
            style_axis(ax, yfmt=USD_FMT)
            ax.tick_params(labelsize=10)
            continue

        st = _trend_stats(s.index, s.values)
        if st:
            rate = (1 - np.exp(st["b"])) * 100      # positive = losing value
            years = np.asarray((s.index - s.index[0]).days, dtype=float) / 365.25
            ax.plot(s.index, np.exp(st["a"] + st["b"] * years),
                    color=INK if st["usable"] else FAINT,
                    lw=1.8 if st["usable"] else 1.4,
                    linestyle="-" if st["usable"] else (0, (5, 4)),
                    zorder=3)
            if st["usable"]:
                half = 2 * st["se"] * 100      # ~percentage points
                note = (f"{'losing' if rate > 0 else 'gaining'} "
                        f"{abs(rate):.0f}%/yr ± {half:.0f}")
                ncolor, nweight = INK, 600
            else:
                more = max(0.0, st["need_days"] - st["n"])
                note = ("not measurable yet · "
                        + (f"~{more:.0f}d more" if np.isfinite(more)
                           and more < 2000 else "months more"))
                ncolor, nweight = MUTED, 400
            # reserve a clear band under the data for the caption
            lo, hi = ax.get_ylim()
            ax.set_ylim(lo - (hi - lo) * 0.22, hi)
            ax.annotate(note, xy=(0.03, 0.04), xycoords="axes fraction",
                        ha="left", va="bottom", fontsize=10.5,
                        color=ncolor, fontweight=nweight)
            rows.append({"gpu": gpu, "annual_pct": round(rate, 2),
                         "ci_halfwidth_pp": round(2 * st["se"] * 100, 1),
                         "usable": bool(st["usable"]),
                         "obs_days": st["n"],
                         "days_needed": (round(st["need_days"])
                                         if np.isfinite(st["need_days"]) else "")})
        ax.set_title(gpu.upper(), loc="left", fontsize=12, fontweight=600,
                     color=c, pad=6)
        style_axis(ax, yfmt=USD_FMT)
        ax.tick_params(labelsize=10)

    if not drew:
        plt.close(fig)
        return
    fig.text(0.007, 0.977, "Within-vintage price trend — the identified estimate",
             fontsize=19, fontweight="bold", color=INK, va="top", ha="left")
    fig.text(0.007, 0.900,
             "Each vintage tracked against itself, so generation is held constant"
             " · median $/GPU-hour\nRate appears once its 2σ interval fits inside "
             "±15pp — a 3-week drift annualises to noise, not depreciation",
             fontsize=11.5, color=MUTED, va="top", ha="left")
    # escape $ so matplotlib doesn't read it as mathtext
    source_note(fig, r"data: Vast.ai order book · slope is identical in \$/hr or \$/PFLOP-hr")
    fig.savefig("data/within_vintage_chart.png", dpi=150)
    plt.close(fig)

    if rows:
        pd.DataFrame(rows).to_csv("data/within_vintage.csv", index=False)
        done = [r for r in rows if r["usable"]]
        soon = min((r for r in rows if not r["usable"]),
                   key=lambda r: r["days_needed"] or 1e9, default=None)
        msg = f"[within] OK — {len(rows)} vintages, {len(done)} measurable"
        if done:
            msg += ": " + ", ".join(f"{r['gpu']} {r['annual_pct']:+.0f}%/yr"
                                    for r in done)
        elif soon:
            msg += (f" · earliest is {soon['gpu']} in ~"
                    f"{max(0, soon['days_needed'] - soon['obs_days'])} days")
        print(msg)


# --------------------- utilization & market size ----------------------

def build_utilization():
    """Two panels answering 'tight, or growing?':
      top    — share of Vast's visible fleet actually rented
      bottom — implied revenue run-rate (rented x price) = price x quantity
    A price rise with rising revenue is demand; with flat/falling
    revenue and high utilization it's scarcity."""
    path = "data/vast_utilization.csv"
    if not os.path.exists(path):
        return
    try:
        df = pd.read_csv(path, parse_dates=["timestamp_utc"])
    except Exception as e:
        print(f"[util] could not read {path}: {e}")
        return
    for c in ["utilization_pct", "revenue_usd_hr", "rented", "total"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "gpu", "utilization_pct"])
    if df.empty:
        return

    span_days = (df["timestamp_utc"].max() - df["timestamp_utc"].min()).days
    freq = "h" if span_days < 3 else "D"

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10.5, 9), sharex=True, constrained_layout=True,
        gridspec_kw={"height_ratios": [1.35, 1]})
    ends1, ends2 = [], []
    daily_rows = []
    for gpu in ["a100", "h100", "h200", "b200", "b300"]:
        sub = df[df["gpu"] == gpu].set_index("timestamp_utc").sort_index()
        if sub.empty:
            continue
        c = GPU_COLORS.get(gpu, INK)
        u = sub["utilization_pct"].resample(freq).mean().dropna()
        # markers read well while points are sparse, but turn to mush
        # once the 10-minute log has real history behind it
        mk = dict(marker="o", ms=5) if len(u) <= 40 else {}
        if len(u):
            ax1.plot(u.index, u, color=c, lw=3, solid_capstyle="round", **mk)
            ends1.append((f"{gpu.upper()} {u.iloc[-1]:.0f}%", u.index[-1],
                          float(u.iloc[-1]), c))
        if "revenue_usd_hr" in sub.columns:
            rv = sub["revenue_usd_hr"].resample(freq).mean().dropna()
            if len(rv):
                ax2.plot(rv.index, rv, color=c, lw=3,
                         solid_capstyle="round", **mk)
                ends2.append((f"{gpu.upper()} ${rv.iloc[-1]:,.0f}",
                              rv.index[-1], float(rv.iloc[-1]), c))

        # Daily means for the dashboard cards. The page reads this small
        # file, never the raw 10-minute log, which grows all year.
        d = (sub.resample("D").mean(numeric_only=True)
             .dropna(subset=["utilization_pct"]))
        for ts, row in d.iterrows():
            rec = {"date": ts.strftime("%Y-%m-%d"), "gpu": gpu,
                   "utilization_pct": round(float(row["utilization_pct"]), 2)}
            for col, nd in (("revenue_usd_hr", 2), ("rented", 1), ("total", 1)):
                v = row.get(col)
                rec[col] = round(float(v), nd) if pd.notna(v) else ""
            daily_rows.append(rec)

    if daily_rows:
        (pd.DataFrame(daily_rows).sort_values(["date", "gpu"])
           .to_csv("data/daily_utilization.csv", index=False))
    if not ends1:
        plt.close(fig)
        return

    # With only a day or two logged, the date locator sprawls over
    # years — pin a readable window until real history accumulates.
    if span_days < 3:
        lo = df["timestamp_utc"].min() - pd.Timedelta(days=1)
        hi = df["timestamp_utc"].max() + pd.Timedelta(days=1)
        ax1.set_xlim(lo, hi)
        ax2.set_xlim(lo, hi)

    ax1.set_ylim(0, 105)
    style_axis(ax1, "Share of listed GPUs rented", pct=True)
    direct_labels(ax1, ends1, room=0.20)
    draw_events(ax1, "gpu")
    title_block(ax1, "Vast.ai fleet utilization",
                "Rented ÷ listed · high and rising = the market is tight")

    ax2.set_ylim(bottom=0)
    style_axis(ax2, "Implied revenue ($/hour)", yfmt=USD_FMT)
    direct_labels(ax2, ends2, room=0.20)
    title_block(ax2, "Implied revenue run-rate — price × quantity",
                "Rented GPUs × their price · separates scarcity from a growing market")
    source_note(fig, "utilization estimated by 500.farm from Vast.ai order-book snapshots")
    fig.savefig("data/utilization_chart.png", dpi=150)
    plt.close(fig)

    latest = df[df["timestamp_utc"] == df["timestamp_utc"].max()]
    print("[util] OK — " + ", ".join(
        f"{r.gpu} {r.utilization_pct:.0f}%" for r in latest.itertuples()))


def build_venue_prices():
    """One GPU-hour, three venues: SF Compute (order-book wholesale),
    Vast.ai (merchant spot), Azure (hyperscaler list). H100 only —
    it's the sole vintage with liquidity across all three."""
    sf_path = "data/sfcompute_prices.csv"
    if not os.path.exists(sf_path):
        return
    try:
        sf = pd.read_csv(sf_path, parse_dates=["date"])
    except Exception as e:
        print(f"[venues] could not read {sf_path}: {e}")
        return
    for c in ["avg_usd_gpu_hr", "top_usd_gpu_hr", "bottom_usd_gpu_hr"]:
        sf[c] = pd.to_numeric(sf[c], errors="coerce")
    sf = sf[sf["gpu"] == "h100"].dropna(subset=["date", "avg_usd_gpu_hr"])
    sf = sf.set_index("date").sort_index()
    if sf.empty:
        print("[venues] no SF Compute H100 rows")
        return

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ends = []

    # SF Compute: average with a high/low band
    band = sf[(sf["top_usd_gpu_hr"] > 0) & (sf["bottom_usd_gpu_hr"] > 0)]
    if not band.empty:
        ax.fill_between(band.index, band["bottom_usd_gpu_hr"],
                        band["top_usd_gpu_hr"], color=PALETTE[3],
                        alpha=0.13, linewidth=0)
    ax.plot(sf.index, sf["avg_usd_gpu_hr"], color=PALETTE[3], lw=3,
            solid_capstyle="round")
    ends.append((f"SF Compute ${sf['avg_usd_gpu_hr'].iloc[-1]:.2f}",
                 sf.index[-1], float(sf["avg_usd_gpu_hr"].iloc[-1]), PALETTE[3]))

    # Vast.ai merchant median
    if os.path.exists("data/daily_index_h100.csv"):
        try:
            v = pd.read_csv("data/daily_index_h100.csv",
                            parse_dates=["timestamp_utc"])
            v["median_price"] = pd.to_numeric(v.get("median_price"),
                                              errors="coerce")
            v = v.dropna(subset=["median_price"]).set_index("timestamp_utc")
            if not v.empty:
                s = v["median_price"]
                ax.plot(s.index, s, color=GPU_COLORS["h100"], lw=3,
                        solid_capstyle="round")
                ends.append((f"Vast median ${s.iloc[-1]:.2f}", s.index[-1],
                             float(s.iloc[-1]), GPU_COLORS["h100"]))
        except Exception as e:
            print(f"[venues] Vast series unavailable: {e}")

    # Azure spot + on-demand list
    if os.path.exists("data/cloud_prices.csv"):
        try:
            cp = pd.read_csv("data/cloud_prices.csv", parse_dates=["date"])
            cp["usd_per_gpu_hr"] = pd.to_numeric(cp["usd_per_gpu_hr"],
                                                 errors="coerce")
            cp = cp[(cp["gpu"] == "h100") & (cp["cloud"] == "azure")]
            for term, color, label in [("spot", PALETTE[6], "Azure spot"),
                                       ("ondemand", MUTED, "Azure on-demand")]:
                t = (cp[cp["term"] == term].dropna(subset=["usd_per_gpu_hr"])
                     .groupby("date")["usd_per_gpu_hr"].min().sort_index())
                if t.empty:
                    continue
                style = dict(color=color, lw=2.2, linestyle=(0, (4, 3)))
                if len(t) == 1:
                    ax.plot(t.index, t, marker="o", ms=7, **style)
                else:
                    ax.plot(t.index, t, **style)
                ends.append((f"{label} ${t.iloc[-1]:.2f}", t.index[-1],
                             float(t.iloc[-1]), color))
        except Exception as e:
            print(f"[venues] Azure series unavailable: {e}")

    ax.set_ylim(bottom=0)
    style_axis(ax, "$ per GPU-hour", yfmt=USD_FMT)
    direct_labels(ax, ends, room=0.34)
    draw_events(ax, "gpu", "h100")
    title_block(ax, "One H100-hour, three markets",
                "Order-book wholesale vs merchant spot vs hyperscaler list · shaded = SF Compute daily high/low")
    source_note(fig, "data: sfcompute.com/prices · Vast.ai · Azure Retail Prices API")
    fig.savefig("data/venue_price_chart.png", dpi=150)
    plt.close(fig)
    print(f"[venues] OK — SF Compute {len(sf)} days, "
          f"latest ${sf['avg_usd_gpu_hr'].iloc[-1]:.2f}/GPU-hr")


# ------------------------ cloud term structure ------------------------

TERM_ORDER = ["spot", "ondemand", "1yr", "3yr", "5yr"]
TERM_LABELS = ["spot", "on-demand", "1 yr", "3 yr", "5 yr"]


def build_cloud_term(avail_results):
    """Hyperscaler term structure: list $/GPU-hr from spot to 5-year
    commitments (data/cloud_prices.csv, latest day), with the Vast.ai
    median as a dotted merchant-market reference per vintage."""
    path = "data/cloud_prices.csv"
    if not os.path.exists(path):
        return
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except Exception as e:
        print(f"[cloud-term] could not read {path}: {e}")
        return
    df["usd_per_gpu_hr"] = pd.to_numeric(df["usd_per_gpu_hr"], errors="coerce")
    df = df.dropna(subset=["date", "usd_per_gpu_hr"])
    if df.empty:
        return
    latest = df[df["date"] == df["date"].max()]

    # latest Vast median per vintage, for the merchant reference lines
    vast = {}
    for gpu, daily in avail_results or []:
        if "median_price" in daily.columns and daily["median_price"].notna().any():
            vast[gpu] = float(daily["median_price"].dropna().iloc[-1])

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    xs = range(len(TERM_ORDER))
    drew = False
    ends = []
    for gpu in ["a100", "h100", "h200", "b200", "b300"]:
        sub = latest[latest["gpu"] == gpu]
        if sub.empty:
            continue
        pts = [(i, float(sub[sub["term"] == t]["usd_per_gpu_hr"].min()))
               for i, t in enumerate(TERM_ORDER)
               if not sub[sub["term"] == t].empty]
        if not pts:
            continue
        c = GPU_COLORS.get(gpu, INK)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=c, lw=3,
                marker="o", ms=7, solid_capstyle="round", zorder=3)
        last_x, last_y = pts[-1]
        ax.annotate(f"  {gpu.upper()} ${last_y:.2f}", xy=(last_x, last_y),
                    fontsize=13, fontweight=600, color=c, va="center")
        drew = True
    if not drew:
        plt.close(fig)
        print("[cloud-term] no chartable rows")
        return

    # merchant reference: dotted line at the Vast median
    for gpu, med in vast.items():
        c = GPU_COLORS.get(gpu, INK)
        ax.axhline(med, color=c, lw=1.6, linestyle=(0, (1, 3)), alpha=0.75,
                   zorder=1)
        ax.annotate(f"Vast {gpu.upper()} ${med:.2f}", xy=(0.02, med),
                    xycoords=("axes fraction", "data"),
                    xytext=(0, 5), textcoords="offset points",
                    fontsize=10, color=c, alpha=0.95, va="bottom")

    ax.set_xlim(-0.35, len(TERM_ORDER) - 1 + 1.15)
    ax.set_ylim(bottom=0)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(TERM_LABELS, fontsize=12)
    ax.tick_params(axis="x", pad=10)
    style_axis_numeric(ax, "$ per GPU-hour (list)", yfmt=USD_FMT)
    title_block(ax, "Azure GPU term structure",
                "Cheapest US region, list prices · spot at list price = no discount (tight capacity) · dotted = Vast.ai median")
    source_note(fig, "data: Azure Retail Prices API · Vast.ai order book · SKUs: cloud_skus.yml")
    fig.savefig("data/cloud_term_chart.png", dpi=150)
    plt.close(fig)
    print(f"[cloud-term] OK — {latest['gpu'].nunique()} vintages, "
          f"{len(latest)} price points")




# ------------------------- Artificial Analysis -------------------------
# Two charts from fetch_aa.py's CSVs. Both exist because everything else
# in the token module comes from ONE instrument (OpenRouter's passive
# telemetry). AA is an active probe -- a fixed request fired at each
# host directly -- so it can (1) cross-check the same host measured two
# ways and (2) supply a QUALITY axis (intelligence index) that lets
# $/token be normalised by capability, the token-side analogue of the
# GPU value lenses. Attribution to artificialanalysis.ai is a condition
# of the free API; it is on the chart and on the dashboard section.

AA_SOURCE = ("Source: Artificial Analysis (artificialanalysis.ai) "
             "· OpenRouter endpoint stats")
# Intelligence-index bands for the frontier time series: cheapest
# blended price per day among models scoring at least the lower bound
# and below the upper. Adjust as the index scale drifts.
AA_BANDS = [(50, None), (45, 50), (40, 45), (35, 40)]
# AA's time to first token waits through hidden reasoning on reasoning-
# mode models (60-150s on Opus 5 / GPT-5.6 Luna max; its inputTime /
# reasoningTime split does NOT separate it -- checked 2026-09-06), while
# OpenRouter's latency is a first-chunk figure. Above this many seconds
# the two are not the same quantity, so the pair leaves the latency
# panel instead of plotting a 100x that means nothing.
AA_TTFT_MAX_S = 10.0


def _write_daily(path, day, frame):
    """Merge-rewrite: replace rows for `day`, keep the rest."""
    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
    frame = frame.copy()
    frame.insert(0, "date", day_s)
    cols = list(frame.columns)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype=str)
        old = old[old["date"] != day_s]
        frame = pd.concat([old, frame.astype(str)], ignore_index=True)
    frame.reindex(columns=cols).to_csv(path, index=False)


def _or_daily_medians(on_or_before):
    """OpenRouter perf_log medians per (model, provider) for the latest
    logged day on or before `on_or_before`. Returns (day, frame)."""
    path = "data/perf_log.csv"
    if not os.path.exists(path):
        return None, None
    df = pd.read_csv(path, usecols=["timestamp_utc", "model", "provider",
                                    "throughput_tps", "latency_s"])
    df["day"] = pd.to_datetime(df["timestamp_utc"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["day"])
    df = df[df["day"] <= pd.Timestamp(on_or_before)]
    if df.empty:
        return None, None
    day = df["day"].max()
    sub = df[df["day"] == day].copy()
    for c in ("throughput_tps", "latency_s"):
        sub[c] = pd.to_numeric(sub[c], errors="coerce")
    med = (sub.groupby(["model", "provider"])[["throughput_tps", "latency_s"]]
              .median().reset_index())
    return day, med


def _ratio_panel(ax, d, col, color, xlabel):
    """One row per host: grey dots = models, diamond = host median of
    the AA ÷ OpenRouter ratio, log x. Returns the row order."""
    from matplotlib.ticker import NullFormatter
    order = d.groupby("provider")[col].median().sort_values()
    rowmax = d.groupby("provider")[col].max()
    ypos = {p: i for i, p in enumerate(order.index)}
    ax.set_xscale("log")
    ax.set_xlim(min(0.5, d[col].min()) * 0.8, d[col].max() * 3.2)
    ax.axvline(1.0, color=FAINT, lw=1.2, linestyle=(0, (3, 3)))
    ax.plot(d[col], [ypos[p] for p in d["provider"]], "o", ms=6,
            color=PALETTE[8], alpha=0.5, zorder=3, linestyle="none")
    for p, m in order.items():
        ax.plot(m, ypos[p], "D", ms=8.5, color=color, zorder=5)
        ax.annotate(f"{m:.2f}×", xy=(rowmax[p], ypos[p]), xytext=(8, 0),
                    textcoords="offset points", fontsize=11.5, fontweight=600,
                    color=color, va="center", ha="left")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(list(order.index))
    ax.set_ylim(-0.7, len(order) - 0.3)
    style_axis_numeric(ax, xlabel=xlabel)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}×"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.grid(axis="x", color=GRID, lw=1)
    ax.grid(axis="y", visible=False)
    return order


def _aa_crosscheck(day, latest):
    """Same (model, host) pair measured by AA's probe and by OpenRouter's
    telemetry. Ratio per host: a consistent sign across hosts is
    structural (single-query probe vs real concurrent traffic); a host
    that stands apart from the pack is the interesting one."""
    or_day, med = _or_daily_medians(day)
    if med is None:
        print("[aa] no OpenRouter perf data to cross-check against")
        return
    j = latest.merge(med, left_on=["or_model", "provider"],
                     right_on=["model", "provider"], how="inner")
    j = j[(j["tps_median"] > 0) & (j["throughput_tps"] > 0)].copy()
    if len(j) < 5:
        print(f"[aa] only {len(j)} joinable pairs — skipping cross-check")
        return
    j["tps_ratio"] = j["tps_median"] / j["throughput_tps"]
    aa_lat = j["ttft_median"]
    j["aa_latency_s"] = aa_lat
    lat_ok = (aa_lat > 0) & (aa_lat <= AA_TTFT_MAX_S) & (j["latency_s"] > 0)
    j["lat_ratio"] = np.where(lat_ok, aa_lat / j["latency_s"], np.nan)
    j["or_date"] = or_day.strftime("%Y-%m-%d")
    _write_daily("data/aa_crosscheck.csv", day,
                 j[["or_model", "aa_slug", "provider", "variant", "or_date",
                    "tps_median", "throughput_tps", "tps_ratio",
                    "aa_latency_s", "latency_s", "lat_ratio"]].round(3))

    gap = (pd.Timestamp(day) - or_day).days
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 12))
    fig.subplots_adjust(left=0.19, right=0.96, top=0.91, bottom=0.075, hspace=0.55)

    d1 = j[j["tps_ratio"] > 0]
    order = _ratio_panel(ax1, d1, "tps_ratio", PALETTE[0],
                         f"Output speed: AA ÷ OpenRouter (log) · {len(d1)} model×host "
                         f"pairs · {d1['or_model'].nunique()} models")
    ax1.annotate("1.00× — both instruments agree", xy=(1.0, len(order) - 0.3),
                 xytext=(5, -2), textcoords="offset points", fontsize=10.5,
                 color=MUTED, va="top", ha="left")
    title_block(ax1, "Same host, two instruments",
                "AA single-query probe ÷ OpenRouter live-traffic median · "
                "dots = models · diamond = host median")

    d2 = j.dropna(subset=["lat_ratio"])
    d2 = d2[d2["lat_ratio"] > 0]
    if d2.empty:
        ax2.set_visible(False)
    else:
        dropped = int((j["ttft_median"] > AA_TTFT_MAX_S).sum())
        _ratio_panel(ax2, d2, "lat_ratio", PALETTE[1],
                     f"Time to first token: AA ÷ OpenRouter (log) · {len(d2)} pairs · "
                     f"{dropped} reasoning-mode pairs dropped")
        title_block(ax2, "Time to first token",
                    f"AA TTFT ÷ OpenRouter latency · reasoning-mode pairs "
                    f"(AA TTFT > {AA_TTFT_MAX_S:g} s) excluded")
    note = AA_SOURCE
    if gap > 1:
        note += f" · OpenRouter data from {or_day:%d %b %Y} ({gap}d before the AA reading)"
    source_note(fig, note)
    fig.savefig("data/aa_crosscheck_chart.png", dpi=150)
    plt.close(fig)
    print(f"[aa] cross-check: {len(j)} pairs, median AA/OR speed ratio "
          f"{j['tps_ratio'].median():.2f}x (OpenRouter day {or_day:%Y-%m-%d})")


def _short_name(name):
    """'Claude Opus 5 (Adaptive Reasoning, Max Effort)' -> 'Claude Opus 5'."""
    return str(name).split(" (")[0].strip()


def _aa_hedonic(m):
    """Price of a unit of intelligence. Top: today's cross-section with
    the frontier (cheapest model at least this smart). Bottom: the
    frontier's cheapest price per intelligence band over time — the
    hedonic deflator. History accumulates from the first collection
    day; AA has no backfill."""
    price, intel = "price_1m_blended_3_to_1", "intelligence_index"
    day = m["date"].max()
    cs = m[m["date"] == day].copy()
    rel = pd.to_datetime(cs["release_date"], errors="coerce")
    cs = cs[rel >= day - pd.Timedelta(days=365)]
    cs = cs.sort_values([intel, price], ascending=[False, True])
    front, best = [], np.inf
    for _, r in cs.iterrows():
        if r[price] < best:
            front.append(r)
            best = r[price]
    front = pd.DataFrame(front).sort_values(intel) if front else pd.DataFrame()

    # frontier per band, every day
    hist = []
    for d, g in m.groupby("date"):
        for lo, hi in AA_BANDS:
            sel = g[(g[intel] >= lo) & (g[intel] < (hi if hi is not None else np.inf))]
            if sel.empty:
                continue
            r = sel.loc[sel[price].idxmin()]
            hist.append({"date": d, "band": f"{lo}+" if hi is None else f"{lo}-{hi}",
                         "min_price": r[price], "model": r["aa_slug"],
                         "intelligence_index": r[intel]})
    if not hist:
        return
    hist = pd.DataFrame(hist)
    hist.to_csv("data/aa_frontier.csv", index=False)
    wide = hist.pivot(index="date", columns="band", values="min_price").sort_index()
    band_order = [f"{lo}+" if hi is None else f"{lo}-{hi}" for lo, hi in AA_BANDS]
    wide = wide[[b for b in band_order if b in wide.columns]]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 12))
    fig.subplots_adjust(left=0.11, right=0.97, top=0.91, bottom=0.075, hspace=0.55)

    # --- top: cross-section ---
    ymin, ymax = cs[price].min() * 0.6, cs[price].max() * 2.5
    ax1.set_yscale("log")
    ax1.set_ylim(ymin, ymax)
    ax1.set_xlim(-1, cs[intel].max() + 16)
    ax1.plot(cs[intel], cs[price], "o", ms=5.5, color=PALETTE[8], alpha=0.45,
             linestyle="none", zorder=3)
    if not front.empty:
        ax1.step(front[intel], front[price], where="post", color=PALETTE[0],
                 lw=2.6, zorder=4)
        ax1.plot(front[intel], front[price], "o", ms=7, color=PALETTE[0], zorder=5)
        # labels climb the frontier; push apart in log space so none overlap
        gap = 0.07 * (np.log10(ymax) - np.log10(ymin))
        placed = []
        for _, r in front.iterrows():
            y = np.log10(r[price])
            if placed and y - placed[-1] < gap:
                y = placed[-1] + gap
            placed.append(y)
            ax1.annotate(_short_name(r["name"]), xy=(r[intel], r[price]),
                         xytext=(r[intel] + 0.9, 10 ** y), textcoords="data",
                         fontsize=10, color=PALETTE[0], fontweight=600,
                         va="center", ha="left", zorder=6,
                         bbox=dict(boxstyle="round,pad=0.15", fc=PAPER,
                                   ec="none", alpha=0.85))
    style_axis_numeric(ax1, "Blended $ per M tokens (3:1 in:out, log)",
                       "Artificial Analysis Intelligence Index",
                       yfmt=FuncFormatter(lambda v, _: f"${v:g}"))
    title_block(ax1, "What a unit of intelligence costs",
                f"Models released in the last 12 months · frontier = cheapest "
                f"at least this smart · {day:%d %b %Y}")

    # --- bottom: frontier over time ---
    colors = [PALETTE[2], PALETTE[0], PALETTE[3], PALETTE[1]]
    ax2.set_yscale("log")
    style_axis(ax2, "Cheapest blended $ per M tokens (log)",
               yfmt=FuncFormatter(lambda v, _: f"${v:g}"))
    if len(wide) < 3:
        ax2.set_xlim(wide.index.min() - pd.Timedelta(days=1),
                     wide.index.max() + pd.Timedelta(days=1))
        ax2.xaxis.set_major_locator(mdates.DayLocator())
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ends = []
    for i, band in enumerate(wide.columns):
        s = wide[band].dropna()
        if s.empty:
            continue
        c = colors[i % len(colors)]
        ax2.plot(s.index, s.values, "-o", lw=2.4, ms=4.5, color=c, solid_capstyle="round")
        ends.append((f"index {band}", s.index[-1], float(s.iloc[-1]), c))
    x0, x1 = ax2.get_xlim()
    ax2.set_xlim(x0, x1 + (x1 - x0) * 0.24)
    for label, x, y, c in ends:      # no nudging: direct_labels assumes a linear y
        ax2.annotate("  " + label, xy=(x, y), fontsize=13, fontweight=600,
                     color=c, va="center")
    draw_events(ax2, "tokens")
    title_block(ax2, "Hedonic deflator",
                f"Cheapest price per intelligence band, daily · history accumulates from "
                f"{wide.index.min():%d %b %Y}")
    source_note(fig, AA_SOURCE)
    fig.savefig("data/aa_hedonic_chart.png", dpi=150)
    plt.close(fig)
    print(f"[aa] hedonic: {len(cs)} models in cross-section, "
          f"{len(front)} on frontier, {len(wide)} days of history")


def build_aa():
    """Artificial Analysis charts; skips quietly if fetch_aa.py has not run."""
    prov_path, models_path = "data/aa_providers.csv", "data/aa_models.csv"
    if os.path.exists(prov_path):
        aa = pd.read_csv(prov_path)
        aa["date"] = pd.to_datetime(aa["date"], errors="coerce")
        aa = aa.dropna(subset=["date"])
        if not aa.empty:
            day = aa["date"].max()
            latest = aa[aa["date"] == day].copy()
            for c in ("tps_median", "ttft_median", "ttfat_input_s"):
                latest[c] = pd.to_numeric(latest[c], errors="coerce") if c in latest else np.nan
            latest["variant"] = latest["variant"].fillna("")
            _aa_crosscheck(day, latest)
    if os.path.exists(models_path):
        m = pd.read_csv(models_path)
        m["date"] = pd.to_datetime(m["date"], errors="coerce")
        for c in ("intelligence_index", "price_1m_blended_3_to_1"):
            m[c] = pd.to_numeric(m[c], errors="coerce")
        m = m.dropna(subset=["date", "intelligence_index", "price_1m_blended_3_to_1"])
        m = m[m["price_1m_blended_3_to_1"] > 0]
        if not m.empty:
            _aa_hedonic(m)


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
    build_hardware_value(avail_results)
    build_within_vintage()
    build_cloud_term(avail_results)
    build_utilization()
    build_venue_prices()
    build_tokens()
    build_providers()
    build_pricing()
    build_price_history()
    build_perf()
    build_aa()
    print(f"\nDone. {len(avail_results)} availability indices, "
          f"{len(supply_results)} supply charts.")
