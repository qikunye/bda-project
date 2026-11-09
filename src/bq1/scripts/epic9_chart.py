"""Epic 9 T9.5 chart: cumulative distribution of risk_score for tail and test rows, positives vs negatives.
Reads artifacts/medium_tail/tail_score_percentiles_medium.csv (101 percentile points per group; test groups come from
Athena approx_percentile, tail groups are exact). Colour = laundering label (validated pair, blue/orange);
line style = tail (dashed) vs test (solid). Light mode, reference palette from the dataviz skill."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUT = "artifacts/medium_tail/"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
POS, NEG = "#2a78d6", "#eb6834"   # categorical slots 1 and 2; validate_palette.js: all checks pass
d = pd.read_csv(OUT + "tail_score_percentiles_medium.csv")
series = [("test negatives", NEG, "-"), ("test positives", POS, "-"),
          ("tail negatives", NEG, (0, (5, 3))), ("tail positives", POS, (0, (5, 3)))]

fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160, facecolor=SURFACE)
ax.set_facecolor(SURFACE)
for name, col, ls in series:
    ax.plot(d[name], d["percentile"], color=col, linestyle=ls, linewidth=2, label=name.capitalize())
ax.set_xscale("log")
ax.set_xlim(1e-5, 1.2)
ax.set_ylim(0, 100)
ax.set_xlabel("risk_score (log scale)", color=INK2)
ax.set_ylabel("Share of rows at or below the score (%)", color=INK2)
ax.grid(True, color=GRID, linewidth=0.8)
ax.set_axisbelow(True)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
for s in ("left", "bottom"):
    ax.spines[s].set_color(GRID)
ax.tick_params(colors=INK2)
ax.legend(frameon=False, labelcolor=INK, loc="lower right", fontsize=10)
fig.suptitle("Tail-day rows score higher than test rows, and laundering rows stand clearly apart",
             x=0.01, ha="left", color=INK, fontsize=12.5, fontweight="bold")
ax.set_title("XGBoost risk_score, LI-Medium. Tail = excluded 17 to 27 Sep (1,370 rows). Test = 14 to 16 Sep (6.25M rows).",
             loc="left", color=INK2, fontsize=9.5)
fig.text(0.01, 0.01, "Tail curves are exact. Test curves use Athena approx_percentile (approximate). Synthetic data.",
         color=INK2, fontsize=8)
fig.tight_layout(rect=(0, 0.03, 1, 0.95))
fig.savefig(OUT + "tail_score_distributions_medium.png", facecolor=SURFACE)
print("saved")
