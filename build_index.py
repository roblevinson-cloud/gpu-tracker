"""
Build the GPU Availability Index from the raw log.

Turns data/availability_log.csv (one row per check) into:
  - data/daily_index.csv : daily availability % + 30-day average
  - data/index_chart.png : a chart like 3Fourteen's

Run this whenever you want to see the index (it does not need to
run automatically). On GitHub you can run it with one click using
the "Build Index Chart" workflow, or on your computer with:
    pip install pandas matplotlib
    python build_index.py
"""

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

LOG_FILE = "data/availability_log.csv"

df = pd.read_csv(LOG_FILE, parse_dates=["timestamp_utc"])
df = df.dropna(subset=["overall_available"])
df["overall_available"] = df["overall_available"].astype(float)

# Daily availability % = share of checks that day where a GPU was rentable
daily = (
    df.set_index("timestamp_utc")["overall_available"]
    .resample("D")
    .mean()
    .mul(100)
    .rename("availability_pct")
    .to_frame()
)

# 30-day smoothed index (this is the teal line in 3Fourteen's charts)
daily["availability_30d_avg"] = daily["availability_pct"].rolling(30, min_periods=5).mean()

daily.to_csv("data/daily_index.csv")

fig, ax = plt.subplots(figsize=(12, 6))
ax.plot(daily.index, daily["availability_pct"], color="lightgray", lw=1,
        label="Daily availability %")
ax.plot(daily.index, daily["availability_30d_avg"], color="#2ab5ac", lw=2.5,
        label="30-day average (the index)")
ax.set_title("H100 On-Demand Availability Index (<$4/hr, 80GB)")
ax.set_ylabel("% of checks with a GPU available")
ax.set_ylim(0, 100)
ax.legend()
ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig("data/index_chart.png", dpi=150)

print(daily.tail(10))
print("\nSaved: data/daily_index.csv and data/index_chart.png")
