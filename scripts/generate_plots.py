#!/usr/bin/env python3
"""
Generate publication-quality figures for polymarket-sdk documentation.

Produces:
  - docs/figures/api_latency_comparison.png
  - docs/figures/orderbook_spread_by_type.png

Usage:
  python scripts/generate_plots.py
"""

import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ── Color palette ──────────────────────────────────────────────────────────
PRIMARY = "#2196F3"
SECONDARY = "#4CAF50"
ACCENT = "#E91E63"
ORANGE = "#FF9800"
BG_COLOR = "#FAFAFA"
GRID_COLOR = "#E0E0E0"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "figures")
DPI = 150


def _apply_style(ax):
    """Apply consistent styling to an axes object."""
    ax.set_facecolor(BG_COLOR)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color=GRID_COLOR, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)


def figure_api_latency():
    """Bar chart comparing cold vs pooled connection latency."""
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor(BG_COLOR)
    _apply_style(ax)

    # Data
    categories = ["Single Request", "26 Bin Midpoints"]
    cold = [235, 6100]
    pooled = [30, 800]

    x = np.arange(len(categories))
    width = 0.32

    bars_cold = ax.bar(
        x - width / 2, cold, width, label="Cold Connection",
        color=ACCENT, edgecolor="white", linewidth=0.8, zorder=3
    )
    bars_pooled = ax.bar(
        x + width / 2, pooled, width, label="Pooled Connection",
        color=PRIMARY, edgecolor="white", linewidth=0.8, zorder=3
    )

    # Value labels
    for bar in bars_cold:
        h = bar.get_height()
        label = f"{h:,.0f}ms" if h < 1000 else f"{h / 1000:.1f}s"
        ax.text(bar.get_x() + bar.get_width() / 2, h + max(cold) * 0.02,
                label, ha="center", va="bottom", fontsize=11, fontweight="bold",
                color=ACCENT)
    for bar in bars_pooled:
        h = bar.get_height()
        label = f"{h:,.0f}ms" if h < 1000 else f"{h / 1000:.1f}s"
        ax.text(bar.get_x() + bar.get_width() / 2, h + max(cold) * 0.02,
                label, ha="center", va="bottom", fontsize=11, fontweight="bold",
                color=PRIMARY)

    # Improvement annotations
    for i, (c, p) in enumerate(zip(cold, pooled)):
        improvement = c / p
        mid_y = (c + p) / 2
        ax.annotate(
            f"{improvement:.1f}x faster",
            xy=(i + width / 2, p), xytext=(i + 0.55, mid_y),
            fontsize=12, fontweight="bold", color=SECONDARY,
            arrowprops=dict(arrowstyle="->", color=SECONDARY, lw=1.5),
            ha="left", va="center"
        )

    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=13, fontweight="medium")
    ax.set_ylabel("Latency", fontsize=13)
    ax.set_title("Connection Pooling Impact on API Latency",
                 fontsize=16, fontweight="bold", pad=15)
    ax.legend(fontsize=11, loc="upper left", framealpha=0.9)

    # Custom y-axis formatter
    ax.yaxis.set_major_formatter(
        ticker.FuncFormatter(lambda v, _: f"{v:,.0f}ms" if v < 1000 else f"{v / 1000:.1f}s")
    )

    fig.tight_layout()
    path = os.path.join(OUTPUT_DIR, "api_latency_comparison.png")
    fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    print(f"  Saved {path}")


def figure_orderbook_spread():
    """Horizontal bar chart of typical orderbook spreads by market type."""
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor(BG_COLOR)
    _apply_style(ax)
    ax.grid(axis="x", color=GRID_COLOR, linewidth=0.7, zorder=0)
    ax.grid(axis="y", visible=False)

    # Data: (label, min_spread, max_spread)
    data = [
        ("Low-volume", 5, 15),
        ("NegRisk Multi-outcome", 2, 5),
        ("Crypto (daily)", 1, 3),
        ("Sports (pre-event)", 1, 3),
        ("Political (major)", 0.5, 2),
        ("High-volume Binary", 1, 2),
    ]

    labels = [d[0] for d in data]
    mins = np.array([d[1] for d in data])
    maxs = np.array([d[2] for d in data])
    mids = (mins + maxs) / 2
    ranges = maxs - mins

    y = np.arange(len(labels))
    colors = [ACCENT, ORANGE, PRIMARY, PRIMARY, SECONDARY, SECONDARY]

    # Range bars
    ax.barh(y, ranges, left=mins, height=0.5, color=colors, alpha=0.3,
            edgecolor=[c for c in colors], linewidth=1.5, zorder=3)

    # Midpoint markers
    ax.scatter(mids, y, color=colors, s=100, zorder=4, edgecolors="white", linewidths=1.5)

    # Min/max labels
    for i in range(len(data)):
        ax.text(mins[i] - 0.3, i, f"{mins[i]:.1f}\u00a2", ha="right", va="center",
                fontsize=10, color="#666666")
        ax.text(maxs[i] + 0.3, i, f"{maxs[i]:.1f}\u00a2", ha="left", va="center",
                fontsize=10, color="#666666")

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=12)
    ax.set_xlabel("Typical Spread (cents)", fontsize=13)
    ax.set_title("Polymarket Orderbook Spreads by Market Type",
                 fontsize=16, fontweight="bold", pad=15)
    ax.set_xlim(-1, 17)
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}\u00a2"))

    # Legend for tight vs wide
    ax.axvline(x=3, color=GRID_COLOR, linestyle="--", linewidth=1, zorder=1)
    ax.text(3.2, len(data) - 0.3, "Tight\nspread\nzone", fontsize=9, color="#999999",
            va="top", ha="left", style="italic")

    fig.tight_layout()
    path = os.path.join(OUTPUT_DIR, "orderbook_spread_by_type.png")
    fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    print(f"  Saved {path}")


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("Generating polymarket-sdk figures...")
    figure_api_latency()
    figure_orderbook_spread()
    print("Done.")
