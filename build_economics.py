"""
Provider economics: implied revenue, cost/margin band, energy efficiency,
and growth decomposition - driven by economics_assumptions.yml.
Reads only files the tracker already produces.
"""
import os

import pandas as pd
import yaml
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, MUTED, GRID, PAPER = "#1C2B29", "#66756F", "#E7ECE9", "#FBFCFB"
TEAL, GOLD, ROSE, BLUE = "#14B8A9", "#C9962E", "#C75D9C", "#3B82C4"

plt.rcParams.update({
    "figure.facecolor": PAPER, "axes.facecolor": PAPER,
    "savefig.facecolor": PAPER, "font.size": 13,
    "axes.titlesize": 20, "axes.titleweight": "bold", "axes.titlepad": 28,
    "axes.labelsize": 13, "axes.labelcolor": MUTED,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelsize": 12, "ytick.labelsize": 12, "text.color": INK,
})

DEFAULTS = {
    "input_output_ratio": [3, 1], "free_price_threshold_usd_per_m": 0.01,
    "cost_gpu_vintage": "h100", "fallback_gpu_usd_per_hr": 2.50,
    "node_gpus": 8, "concurrency_low": 50, "concurrency_high": 150,
    "gpu_tdp_watts": 700, "node_overhead_factor": 1.5, "pue": 1.3,
    "electricity_usd_per_kwh": 0.08, "fleet_growth_pct_per_year": 200,
}


def load_assumptions():
    a = dict(DEFAULTS)
    if os.path.exists("economics_assumptions.yml"):
        with open("economics_assumptions.yml") as f:
            user = yaml.safe_load(f) or {}
        a.update({k: v for k, v in user.items() if v is not None})
    return a


def style(ax, ylabel=""):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.grid(axis="x", visible=False)
    ax.tick_params(length=0)
    if ylabel:
        ax.set_ylabel(ylabel)


def sub(ax, text):
    ax.annotate(text, xy=(0, 1), xycoords="axes fraction",
                xytext=(0, 7), textcoords="offset points",
                fontsize=12, color=MUTED, va="bottom", annotation_clip=False)


def save(fig, name, note="assumption-driven estimate - see economics_assumptions.yml"):
    fig.text(0.006, 0.006, note, ha="left", fontsize=9, color="#9AA6A1")
    fig.savefig(name, dpi=150)
    plt.close(fig)


def build():
    A = load_assumptions()
    ri, ro = A["input_output_ratio"]

    # ---------- Layer 1: implied revenue ----------
    tok = pd.read_csv("data/tokens_by_model.csv", parse_dates=["date"])
    tok["tokens"] = pd.to_numeric(tok["tokens"], errors="coerce")
    tok = tok.dropna(subset=["date", "tokens"])
    tok = tok[tok["model"] != "other"]

    pr = pd.read_csv("data/model_prices.csv", parse_dates=["date"])
    for c in ["prompt_usd_per_m", "completion_usd_per_m"]:
        pr[c] = pd.to_numeric(pr[c], errors="coerce")
    pr["blended"] = (ri * pr["prompt_usd_per_m"] + ro * pr["completion_usd_per_m"]) / (ri + ro)

    piv = pr.pivot_table(index="date", columns="model", values="blended",
                         aggfunc="last").sort_index()
    all_dates = pd.date_range(tok["date"].min(), tok["date"].max(), freq="D")
    piv = piv.reindex(piv.index.union(all_dates)).ffill().bfill()
    plong = piv.stack().rename("blended").reset_index()
    plong.columns = ["date", "model", "blended"]

    df = tok.merge(plong, on=["date", "model"], how="left").dropna(subset=["blended"])
    df["revenue"] = df["tokens"] * df["blended"] / 1e6
    df["paid"] = df["blended"] > A["free_price_threshold_usd_per_m"]

    daily = df.groupby("date").apply(lambda g: pd.Series({
        "revenue_usd": g["revenue"].sum(),
        "paid_revenue_usd": g.loc[g["paid"], "revenue"].sum(),
        "tokens": g["tokens"].sum(),
        "paid_tokens": g.loc[g["paid"], "tokens"].sum(),
    }), include_groups=False).sort_index()
    daily["rev_per_m"] = daily["revenue_usd"] / daily["tokens"] * 1e6
    daily["rev_per_m_paid"] = (daily["paid_revenue_usd"] /
                               daily["paid_tokens"].replace(0, pd.NA) * 1e6)
    daily.to_csv("data/econ_daily.csv")

    ma = daily.rolling(7, min_periods=3).mean()

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ax.plot(ma.index, ma["revenue_usd"] / 1e6, color=TEAL, lw=3, label="all tokens")
    ax.plot(ma.index, ma["paid_revenue_usd"] / 1e6, color=GOLD, lw=2.5,
            linestyle=(0, (4, 3)), label="paid tokens only")
    ax.set_ylim(bottom=0)
    style(ax, "Implied revenue ($M per day)")
    ax.legend(loc="upper left", frameon=False, fontsize=12)
    ax.set_title("Implied token revenue - OpenRouter flow", loc="left")
    sub(ax, f"Tokens x list price, {ri}:{ro} input:output blend, 7d avg - gross capacity, not receipts")
    save(fig, "data/econ_revenue_chart.png")

    fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
    ax.plot(ma.index, ma["rev_per_m"], color=TEAL, lw=3, label="all tokens")
    ax.plot(ma.index, ma["rev_per_m_paid"], color=GOLD, lw=2.5,
            linestyle=(0, (4, 3)), label="paid tokens only")
    ax.set_ylim(bottom=0)
    style(ax, "Revenue per M tokens ($)")
    ax.legend(loc="upper left", frameon=False, fontsize=12)
    ax.set_title("Revenue per token", loc="left")
    sub(ax, "Falling line + rising revenue = volume outrunning price cuts (Jevons)")
    save(fig, "data/econ_revper_chart.png")
    print(f"[econ] revenue OK - latest ${ma['revenue_usd'].iloc[-1]/1e6:.2f}M/day implied")

    # ---------- Layer 2: cost band vs price ----------
    have_perf = os.path.exists("data/perf_log.csv")
    have_px = os.path.exists("data/pricing_index.csv")
    if have_perf:
        perf = pd.read_csv("data/perf_log.csv", parse_dates=["timestamp_utc"])
        perf["throughput_tps"] = pd.to_numeric(perf["throughput_tps"], errors="coerce")
        tps = (perf.dropna(subset=["throughput_tps"])
               .set_index("timestamp_utc")["throughput_tps"].resample("D").median())

        gpath = f"data/daily_index_{A['cost_gpu_vintage']}.csv"
        if os.path.exists(gpath):
            gd = pd.read_csv(gpath, parse_dates=["timestamp_utc"])
            gd = gd.set_index("timestamp_utc")
            gpu_hr = pd.to_numeric(gd.get("lowest_price"), errors="coerce") \
                .resample("D").mean().ffill()
        else:
            gpu_hr = pd.Series(dtype=float)
        gpu_hr = gpu_hr.reindex(tps.index).ffill().fillna(A["fallback_gpu_usd_per_hr"])
        node_hr = gpu_hr * A["node_gpus"]

        def cost_per_m(conc):
            return node_hr * 1e6 / (tps * conc * 3600)

        lo = cost_per_m(A["concurrency_high"]).dropna()
        hi = cost_per_m(A["concurrency_low"]).dropna()
        mid = cost_per_m((A["concurrency_low"] + A["concurrency_high"]) / 2).dropna()
        pd.DataFrame({"cost_low": lo, "cost_high": hi, "cost_mid": mid}
                     ).to_csv("data/econ_margin.csv")

        fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
        ax.fill_between(lo.index, lo, hi, color=BLUE, alpha=0.18,
                        label="serving cost band")
        ax.plot(mid.index, mid, color=BLUE, lw=2)
        if have_px:
            px = pd.read_csv("data/pricing_index.csv", parse_dates=["date"])
            px = px.set_index("date")["avg_completion_usd_per_m"].dropna()
            ax.plot(px.index, px, color=ROSE, lw=3, label="weighted market price")
        ax.set_ylim(bottom=0)
        style(ax, "$ per M output tokens")
        ax.legend(loc="upper left", frameon=False, fontsize=12)
        ax.set_title("Serving cost vs market price", loc="left")
        sub(ax, f"{A['node_gpus']}x {A['cost_gpu_vintage'].upper()} node at tracker rental price / "
                f"measured tok/s x {A['concurrency_low']}-{A['concurrency_high']} streams")
        save(fig, "data/econ_margin_chart.png")
        print(f"[econ] margin OK - mid cost ${mid.iloc[-1]:.2f}/M vs market")

        # ---------- Layer 3: energy efficiency ----------
        node_kw = (A["node_gpus"] * A["gpu_tdp_watts"] / 1000
                   * A["node_overhead_factor"] * A["pue"])
        conc_mid = (A["concurrency_low"] + A["concurrency_high"]) / 2
        tokens_per_kwh = (tps * conc_mid * 3600 / node_kw).dropna()
        energy_cost_m = node_kw * A["electricity_usd_per_kwh"] * 1e6 / \
            (tps * conc_mid * 3600)
        fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
        ax.plot(tokens_per_kwh.index, tokens_per_kwh / 1000, color=TEAL, lw=3)
        ax.set_ylim(bottom=0)
        style(ax, "Thousand tokens per kWh")
        ax.set_title("Energy efficiency of serving", loc="left")
        sub(ax, f"Node draw {node_kw:.1f} kW incl overhead+PUE - energy is "
                f"~${energy_cost_m.dropna().iloc[-1]:.2f}/M tokens ({A['electricity_usd_per_kwh']}/kWh)")
        save(fig, "data/econ_efficiency_chart.png")
        print(f"[econ] efficiency OK - {tokens_per_kwh.iloc[-1]/1000:.0f}K tokens/kWh")
    else:
        print("[econ] no perf_log yet - skipping margin & efficiency")

    # ---------- Layer 4: growth decomposition ----------
    td = pd.read_csv("data/tokens_daily.csv", parse_dates=["date"])
    td["total_tokens"] = pd.to_numeric(td["total_tokens"], errors="coerce")
    s = td.dropna().set_index("date")["total_tokens"].sort_index()
    ma7 = s.rolling(7, min_periods=3).mean()
    chg90 = ma7.pct_change(90).dropna()
    if len(chg90):
        ann = (1 + chg90) ** (365 / 90) - 1
        fleet = A["fleet_growth_pct_per_year"] / 100
        residual = ((1 + ann) / (1 + fleet) - 1) * 100
        fig, ax = plt.subplots(figsize=(10.5, 6), constrained_layout=True)
        ax.axhline(0, color="#9AA6A1", lw=1)
        ax.plot(residual.index, residual, color=GOLD, lw=3)
        style(ax, "Implied efficiency growth (% per year)")
        ax.set_title("Token growth beyond assumed compute growth", loc="left")
        sub(ax, f"Annualized 90d token growth minus {A['fleet_growth_pct_per_year']}%/yr "
                "assumed fleet growth - includes OpenRouter share gains (roughest layer)")
        save(fig, "data/econ_growth_chart.png")
        print(f"[econ] decomposition OK - residual {residual.iloc[-1]:+.0f}%/yr")


if __name__ == "__main__":
    build()
