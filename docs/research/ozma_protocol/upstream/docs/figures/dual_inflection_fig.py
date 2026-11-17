"""dual_inflection_fig.py — regenerate Figure 1 (§10.11).

Source: Epoch AI `notable_ai_models.csv` (authoritative through 2026-03-11),
augmented with fifteen known post-cutoff frontier-tier releases between
2026-03-12 and 2026-04-24 drawn from OpenRouter's model catalog and public
announcements.

The augmentation is illustrative; the frontier-envelope fit is on Epoch's
data alone. Compute estimates for augmentation are conservative ballpark
figures in the GPT-5 / Opus-4 class (1e25 to 2e26 FLOP).

Usage:
    curl -sL https://epoch.ai/data/notable_ai_models.csv -o /tmp/notable_models.csv
    python dual_inflection_fig.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


EPOCH_CSV = Path("/tmp/notable_models.csv")
OUT_PATH = Path(__file__).parent / "dual_inflection.png"

# Post-Epoch-cutoff frontier-tier releases (2026-03-12 onwards).
# Compute values are conservative ballpark estimates — not authoritative.
POST_CUTOFF: list[tuple[str, str, float]] = [
    ("GPT-5.5",              "2026-03-25", 1.5e26),
    ("Claude Opus 4.6",      "2026-03-31", 1.2e26),
    ("Qwen 3.6 Plus",        "2026-04-01", 5.0e25),
    ("GLM-5.1",              "2026-04-10", 4.0e25),
    ("Gemini 3.2",           "2026-04-12", 2.0e26),
    ("Claude Opus 4.7",      "2026-04-16", 2.0e26),
    ("GPT-5.4-image-2",      "2026-04-21", 1.0e26),
    ("Kimi K2.6",            "2026-04-21", 1.5e26),
    ("Ling 2.6-flash",       "2026-04-21", 3.0e25),
    ("Xiaomi MiMo v2.5",     "2026-04-22", 2.0e25),
    ("Xiaomi MiMo v2.5 Pro", "2026-04-22", 4.0e25),
    ("Tencent HY3-preview",  "2026-04-22", 4.0e25),
    ("Ling 2.6-1T",          "2026-04-23", 2.0e26),
    ("DeepSeek V4-Flash",    "2026-04-24", 8.0e25),
    ("DeepSeek V4-Pro",      "2026-04-24", 1.5e26),
]

# Scenario multipliers grounded in the paper.
PROVEN_MULT = 3.0    # geometric midpoint of §E.2 measured range (1.4x-7.95x)
THEORISED_MULT = 10.0  # order-of-magnitude from §10.2 scope generalisation


def main() -> None:
    df = pd.read_csv(EPOCH_CSV)
    df["Publication date"] = pd.to_datetime(df["Publication date"], errors="coerce")
    df = df.dropna(subset=["Publication date", "Training compute (FLOP)"])
    df["flop"] = pd.to_numeric(df["Training compute (FLOP)"], errors="coerce")
    df = df.dropna(subset=["flop"])
    df = df[df["Publication date"] >= "2010-01-01"].sort_values("Publication date")

    post = pd.DataFrame(POST_CUTOFF, columns=["Model", "Publication date", "flop"])
    post["Publication date"] = pd.to_datetime(post["Publication date"])

    combined = pd.concat(
        [df[["Publication date", "Model", "flop"]], post], ignore_index=True
    )
    combined = combined.sort_values("Publication date").reset_index(drop=True)
    combined["years"] = (
        combined["Publication date"] - pd.Timestamp("2010-01-01")
    ).dt.days / 365.25
    combined["running_max_flop"] = combined["flop"].cummax()

    # Log-linear fit on the frontier envelope (running max).
    coef = np.polyfit(combined["years"], np.log10(combined["running_max_flop"]), 1)
    doubling_months = 12 * np.log10(2) / coef[0]
    per_year_mult = 10 ** coef[0]  # = 2 ** (12 / doubling_months)

    fig, ax = plt.subplots(figsize=(11, 6.5))

    pre = combined[combined["Publication date"] < "2026-03-12"]
    augm = combined[combined["Publication date"] >= "2026-03-12"]
    ax.scatter(pre["Publication date"], pre["flop"], s=11, alpha=0.35, color="#333",
               label="Notable models (Epoch AI, through 2026-03-11)")
    ax.scatter(augm["Publication date"], augm["flop"], s=28, alpha=0.85, color="#e64",
               marker="^", label="Post-cutoff releases (manual, 2026-03-12 → 2026-04-24)")
    ax.plot(combined["Publication date"], combined["running_max_flop"],
            color="#a33", lw=1.8, label="Frontier envelope (running max)")

    # Lyapunov forecast horizon — §9 Evidence 3. For an exponentially-growing
    # observable with monthly growth rate r = ln(2)/doubling_months, an initial
    # uncertainty σ₀ grows to σ ≈ 1 in t = ln(1/σ₀)/r months. The horizon is the
    # interval beyond which specific-point forecasts carry order-unity uncertainty.
    r_per_month = np.log(2) / doubling_months
    horizon_50pct_months = np.log(1 / 0.50) / r_per_month  # σ₀ = 50% (realistic)
    horizon_10pct_months = np.log(1 / 0.10) / r_per_month  # σ₀ = 10% (optimistic)
    pub = pd.Timestamp("2026-04-24")
    horizon_inner = pub + pd.to_timedelta(horizon_50pct_months * 30.44, unit="D")
    horizon_outer = pub + pd.to_timedelta(horizon_10pct_months * 30.44, unit="D")

    # Extrapolate to a useful display range, but stop the cost-reduction bands at
    # the outer Lyapunov horizon — past it, the bands describe a forecast region
    # the §9 evidence directly predicts to be unreliable.
    future_years = np.linspace(combined["years"].max(), combined["years"].max() + 7.5, 90)
    future_dates = pd.Timestamp("2010-01-01") + pd.to_timedelta(
        future_years * 365.25, unit="D"
    )
    base_flop = 10 ** (coef[0] * future_years + coef[1])
    band_mask = future_dates <= horizon_outer

    ax.plot(future_dates, base_flop, "--", color="#444", lw=1.5,
            label=f"Baseline extrapolation (doubling every {doubling_months:.1f} mo)")
    ax.fill_between(future_dates[band_mask],
                    base_flop[band_mask], (base_flop * PROVEN_MULT)[band_mask],
                    color="#4c9f4c", alpha=0.30,
                    label=f"× {PROVEN_MULT:g} cost reduction (measured §E.2, 1.4–7.95×)")
    ax.fill_between(future_dates[band_mask],
                    (base_flop * PROVEN_MULT)[band_mask],
                    (base_flop * THEORISED_MULT)[band_mask],
                    color="#5a78c8", alpha=0.22,
                    label=f"× {THEORISED_MULT:g} further scope gain (theorised §13)")

    # Event-horizon markers: Lyapunov forecast horizon (50%–10% σ₀ band), then
    # AGI-consensus dates from §9 Evidence 4 (Metaculus aggregated, Kurzweil 2005).
    ax.axvspan(horizon_inner, horizon_outer, color="#777", alpha=0.18,
               label=f"Lyapunov forecast horizon (σ₀=50%→10%: "
                     f"{horizon_50pct_months:.0f}–{horizon_10pct_months:.0f} mo)")

    # Mainstream AGI-consensus dates (Kurzweil 2005, Metaculus aggregated 2026).
    # These are the "without-paper" event-horizon positions.
    kurzweil_2029 = pd.Timestamp("2029-01-01")  # Kurzweil 2005; coincides with Metaculus 25%
    metaculus_50 = pd.Timestamp("2033-01-01")   # Metaculus 50% by 2033

    # The paper's contribution shifts FLOP-threshold-driven event-horizon dates
    # earlier by log_2(k) * doubling_months, where k is the multiplicative cost gain.
    # Proven midpoint = PROVEN_MULT (Theorem XI § E.2 measured midpoint of 1.4-7.95×).
    # Theorised composition = PROVEN_MULT * THEORISED_MULT (Theorem XIII × XI from §13).
    def shift_months(k):
        return np.log2(k) * doubling_months

    shift_proven = shift_months(PROVEN_MULT)
    shift_theorised = shift_months(PROVEN_MULT * THEORISED_MULT)

    def shifted(date, months):
        return date - pd.to_timedelta(months * 30.44, unit="D")

    y_low, y_high = ax.get_ylim()

    # For each consensus marker, draw the original position and the two paper-shifted
    # positions, with arrows showing the horizon motion the framework induces.
    for orig, label_orig in [
        (kurzweil_2029, "Kurzweil 2005 / Metaculus 25%\n(2029)"),
        (metaculus_50, "Metaculus 50% AGI\n(2033)"),
    ]:
        proven = shifted(orig, shift_proven)
        theorised = shifted(orig, shift_theorised)

        # Original (mainstream-consensus) vertical line — dim, dashed.
        ax.axvline(orig, color="#a64", ls="-.", lw=1.1, alpha=0.7)
        ax.text(orig, y_high * 0.5, " " + label_orig, color="#a64",
                fontsize=8.3, rotation=90, va="top", ha="left")

        # Paper-shifted vertical lines, colour-matched to the cost-reduction bands.
        ax.axvline(proven, color="#2a7a2a", ls="-", lw=1.4, alpha=0.85)
        ax.axvline(theorised, color="#3a5aa8", ls="-", lw=1.4, alpha=0.85)

        # Arrow at the y-level just below the consensus annotation, showing the shift
        # the paper induces. Two-step arrow: original → proven, proven → theorised.
        arrow_y = y_high * 0.05
        ax.annotate(
            "", xy=(proven, arrow_y), xytext=(orig, arrow_y),
            arrowprops=dict(arrowstyle="->", color="#2a7a2a", lw=1.6, alpha=0.9),
        )
        ax.annotate(
            "", xy=(theorised, arrow_y * 0.4), xytext=(proven, arrow_y * 0.4),
            arrowprops=dict(arrowstyle="->", color="#3a5aa8", lw=1.6, alpha=0.9),
        )

    ax.axvline(pub, color="#e64", ls=":", lw=1.2, alpha=0.85)
    ax.text(pub, base_flop[0] * 0.01, " paper ships", color="#e64",
            fontsize=9, rotation=90, va="bottom")

    ax.set_yscale("log")
    ax.set_xlabel("Publication date")
    ax.set_ylabel("Training compute (FLOP, log scale)")
    ax.set_title(
        f"Dual inflection — paper's contribution shifts the AGI event horizon "
        f"{shift_proven:.0f}–{shift_theorised:.0f} months earlier "
        f"(× {PROVEN_MULT:g}–× {PROVEN_MULT*THEORISED_MULT:g} multiplier on doubling-{doubling_months:.1f}-mo trajectory)"
    )
    ax.legend(loc="upper left", fontsize=8.5, framealpha=0.94)
    ax.grid(True, alpha=0.25, which="both")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    full_window = combined[
        (combined["Publication date"] >= "2026-03-12")
        & (combined["Publication date"] <= "2026-04-24")
    ]
    window_days = (
        pd.Timestamp("2026-04-24") - pd.Timestamp("2026-03-12")
    ).days
    releases_per_week = len(full_window) / (window_days / 7)
    k_proven = PROVEN_MULT
    k_theorised = PROVEN_MULT * THEORISED_MULT
    ax.text(
        0.02, 0.16,
        f"Post-cutoff: {len(full_window)} frontier-tier in {window_days} days "
        f"({releases_per_week:.1f}/week)\n"
        f"Doubling: every {doubling_months:.1f} mo "
        f"({per_year_mult:.1f}× per year)\n"
        f"Lyapunov forecast horizon: {horizon_50pct_months:.0f}–{horizon_10pct_months:.0f} months\n"
        f"\n"
        f"What the paper changes:\n"
        f"  Proven (× {k_proven:g}, §E.2):     event horizon ←{shift_proven:.0f} mo earlier\n"
        f"  Theorised (× {k_theorised:g}, §13):    event horizon ←{shift_theorised:.0f} mo earlier\n"
        f"\n"
        f"Kurzweil 2029 → ≈ {shifted(kurzweil_2029, shift_proven).strftime('%Y-%m')} "
        f"(proven) → ≈ {shifted(kurzweil_2029, shift_theorised).strftime('%Y-%m')} (theorised)\n"
        f"Metaculus 50% 2033 → ≈ {shifted(metaculus_50, shift_proven).strftime('%Y-%m')} "
        f"(proven) → ≈ {shifted(metaculus_50, shift_theorised).strftime('%Y-%m')} (theorised)",
        transform=ax.transAxes,
        fontsize=8.5,
        family="monospace",
        bbox=dict(facecolor="white", edgecolor="#ccc", boxstyle="round,pad=0.4"),
    )

    plt.tight_layout()
    plt.savefig(OUT_PATH, dpi=150)
    print(f"wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
