"""
Reads all tally_log_*.csv files and generates per-account charts:
  - graphs/ACCOUNT_TIMESTAMP_velocity.png   — batch duration + batches-per-hour
  - graphs/ACCOUNT_TIMESTAMP_efficiency.png — filter catch rate + cumulative calls avoided
  - graphs/ACCOUNT_TIMESTAMP_summary.csv    — stats table

Falls back to tally_log.csv if no per-account files are found.

Usage: python3 plot_tally.py
"""

import csv
import glob
import itertools
import sys
from datetime import datetime
from pathlib import Path
TALLY_FALLBACK = "tally_log.csv"
COST_PER_CALL  = 0.00021  # GPT-4o mini equivalent: $0.15/1M input + $0.60/1M output (~800 in + 150 out tokens/call)
COST_MODEL     = "GPT-4o mini"

# ── Palette ──────────────────────────────────────────────────────────────────
BG        = "#0d1117"
PANEL     = "#161b22"
GRID      = "#21262d"
TEXT      = "#e6edf3"
SUBTEXT   = "#8b949e"
BLUE      = "#58a6ff"
GREEN     = "#3fb950"
RED       = "#f85149"
ORANGE    = "#e3b341"
PURPLE    = "#bc8cff"
TEAL      = "#39d353"


def _style(fig, axes):
    fig.patch.set_facecolor(BG)
    for ax in axes:
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=SUBTEXT, labelsize=8)
        ax.xaxis.label.set_color(SUBTEXT)
        ax.yaxis.label.set_color(SUBTEXT)
        ax.title.set_color(TEXT)
        for spine in ax.spines.values():
            spine.set_edgecolor(GRID)
        ax.grid(color=GRID, linewidth=0.6, linestyle="--", alpha=0.8)
        ax.set_axisbelow(True)


def find_tally_files() -> list[tuple[str, str]]:
    """Return [(account_id, filepath), ...] for all tally CSVs found."""
    per_account = sorted(glob.glob("tally_log_*.csv"))
    if per_account:
        return [(Path(f).stem.removeprefix("tally_log_"), f) for f in per_account]
    if Path(TALLY_FALLBACK).exists():
        return [("all", TALLY_FALLBACK)]
    return []


def load(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        print(f"Error: {path} not found.")
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames] if reader.fieldnames else None
        return [r for r in reader if r.get("batch")]


def plot_velocity(rows: list[dict], account_id: str = "all") -> None:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    if len(rows) < 2:
        print(f"[{account_id}] Need at least 2 batches for velocity chart.")
        return

    timestamps     = [datetime.fromisoformat(r["timestamp"]) for r in rows]
    batches        = [int(r["batch"]) for r in rows]
    durations_sec  = [(timestamps[i] - timestamps[i-1]).total_seconds() for i in range(1, len(timestamps))]
    durations_min  = durations_sec
    batches_per_hr = [3600 / d for d in durations_sec]
    gap_labels     = [f"{batches[i]}→{batches[i+1]}" for i in range(len(durations_sec))]
    x              = list(range(len(gap_labels)))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8))
    fig.suptitle(f"InboxAssassin  ·  Batch Velocity  [{account_id}]", fontsize=15, fontweight="bold",
                 color=TEXT, y=0.97)
    _style(fig, [ax1, ax2])

    # ── Top: time per batch ──────────────────────────────────────────────────
    bars = ax1.bar(x, durations_min, color=BLUE, width=0.55, zorder=3,
                   edgecolor=BG, linewidth=0.8)
    # Gradient-ish: highlight the fastest bar
    min_idx = durations_min.index(min(durations_min))
    bars[min_idx].set_color(TEAL)
    ax1.set_ylabel("Seconds", color=SUBTEXT)
    ax1.set_title("Time between batches", fontsize=10, pad=8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(gap_labels, rotation=35, ha="right", fontsize=7.5)
    for xi, val in enumerate(durations_min):
        ax1.text(xi, val + 0.015 * max(durations_min), f"{val:.0f}s",
                 ha="center", va="bottom", fontsize=7.5, color=TEXT)

    # ── Bottom: batches per hour ─────────────────────────────────────────────
    ax2.plot(x, batches_per_hr, color=ORANGE, linewidth=2.5, marker="o",
             markersize=7, markerfacecolor=BG, markeredgewidth=2.5,
             markeredgecolor=ORANGE, zorder=3)
    ax2.fill_between(x, batches_per_hr, alpha=0.15, color=ORANGE)
    ax2.set_ylabel("Batches / hour", color=SUBTEXT)
    ax2.set_title("Processing velocity", fontsize=10, pad=8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(gap_labels, rotation=35, ha="right", fontsize=7.5)
    ax2.yaxis.set_major_locator(ticker.MaxNLocator(integer=False, nbins=5))


    plt.tight_layout(rect=[0, 0, 1, 0.96])
    Path("graphs").mkdir(exist_ok=True)
    out = f"graphs/{account_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_velocity.png"
    plt.savefig(out, dpi=150, facecolor=BG)
    plt.close()
    print(f"Saved: {out}")


def plot_efficiency(rows: list[dict], account_id: str = "all") -> None:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    # Use sequential index for x-axis (batch numbers may be non-sequential)
    x          = list(range(1, len(rows) + 1))
    catch_rate = [
        int(r["pre_filtered"]) / int(r["total"]) * 100 if int(r["total"]) else 0
        for r in rows
    ]
    cum_saved  = list(itertools.accumulate(int(r["pre_filtered"]) for r in rows))
    cum_total  = list(itertools.accumulate(int(r["total"]) for r in rows))
    mean_rate  = sum(catch_rate) / len(catch_rate)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8))
    fig.suptitle(f"InboxAssassin  ·  Filter Efficiency  [{account_id}]", fontsize=15, fontweight="bold",
                 color=TEXT, y=0.97)
    _style(fig, [ax1, ax2])

    # ── Top: catch rate per batch ────────────────────────────────────────────
    colors = [GREEN if c >= mean_rate else RED for c in catch_rate]
    ax1.bar(x, catch_rate, color=colors, width=0.55, zorder=3,
            edgecolor=BG, linewidth=0.8)
    ax1.axhline(mean_rate, color=ORANGE, linestyle="--", linewidth=1.5,
                zorder=4, label=f"Mean  {mean_rate:.1f}%")
    ax1.set_ylim(0, 110)
    ax1.set_ylabel("% pre-filtered", color=SUBTEXT)
    ax1.set_xlabel("Batch", color=SUBTEXT)
    ax1.set_title("Filter catch rate per batch  (green ≥ mean, red < mean)", fontsize=10, pad=8)
    ax1.legend(fontsize=8, facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)
    ax1.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # ── Bottom: cumulative calls saved vs total ───────────────────────────────
    total_saved  = cum_saved[-1]
    cost_saved   = total_saved * COST_PER_CALL
    cost_label   = f"${cost_saved:.4f}" if cost_saved >= 0.001 else f"{cost_saved*100:.4f}¢"

    ax2.plot(x, cum_total, color=SUBTEXT, linewidth=2, linestyle="--",
             label="Total emails (no filter)", zorder=3)
    ax2.plot(x, cum_saved, color=GREEN, linewidth=2.5,
             marker="o", markersize=6, markerfacecolor=BG,
             markeredgewidth=2.5, markeredgecolor=GREEN, zorder=4,
             label="Calls saved by filter")
    ax2.fill_between(x, cum_saved, cum_total, alpha=0.12, color=GREEN)
    ax2.set_ylabel("Cumulative emails", color=SUBTEXT)
    ax2.set_xlabel("Batch", color=SUBTEXT)
    ax2.set_title("Gemini calls avoided by pre-filter", fontsize=10, pad=8)
    ax2.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax2.legend(fontsize=8, facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)

    # Big call count
    ax2.annotate(f"{total_saved} calls saved",
                 xy=(x[-1], cum_saved[-1]),
                 xytext=(-10, 18), textcoords="offset points",
                 fontsize=13, fontweight="bold", color=GREEN, ha="right")

    # Cost badge in bottom-right corner
    ax2.text(0.98, 0.06, f"{cost_label} saved  vs {COST_MODEL}",
             transform=ax2.transAxes, ha="right", va="bottom",
             fontsize=11, fontweight="bold", color=GREEN,
             bbox=dict(boxstyle="round,pad=0.4", facecolor=PANEL,
                       edgecolor=GREEN, linewidth=1.5))

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    Path("graphs").mkdir(exist_ok=True)
    out = f"graphs/{account_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_efficiency.png"
    plt.savefig(out, dpi=150, facecolor=BG)
    plt.close()
    print(f"Saved: {out}")


def plot_summary(rows: list[dict], account_id: str = "all") -> None:
    timestamps     = [datetime.fromisoformat(r["timestamp"]) for r in rows]
    durations_sec  = [(timestamps[i] - timestamps[i-1]).total_seconds() for i in range(1, len(timestamps))]
    totals         = [int(r["total"])          for r in rows]
    pre_filtered   = [int(r["pre_filtered"])   for r in rows]
    archived       = [int(r["archived"])       for r in rows]
    read_only      = [int(r["read_only"])      for r in rows]
    action_req     = [int(r["action_required"]) for r in rows]

    total_emails   = sum(totals)
    total_batches  = len(rows)
    total_pf       = sum(pre_filtered)
    total_ai       = sum(archived) + sum(read_only) + sum(action_req)
    total_arch     = sum(archived)
    total_ro       = sum(read_only)
    total_ar       = sum(action_req)
    catch_rates    = [pf / t * 100 if t else 0 for pf, t in zip(pre_filtered, totals)]
    overall_catch  = total_pf / total_emails * 100 if total_emails else 0
    cost_saved     = total_pf * COST_PER_CALL
    cost_made      = total_ai * COST_PER_CALL
    snr            = total_ar / total_emails * 100 if total_emails else 0

    career_emails      = 121 * 260 * 40          # 121/day × 260 working days × 40 years
    career_saved       = career_emails * (overall_catch / 100) * COST_PER_CALL
    seconds_per_email  = 13.4                    # Litmus 2018: avg time spent reading an email
    career_pf_count    = career_emails * (overall_catch / 100)
    career_time_sec    = career_pf_count * seconds_per_email
    career_time_hrs    = career_time_sec / 3600
    career_time_days   = career_time_hrs / 24
    career_time_weeks  = career_time_days / 7

    avg_sec = sum(durations_sec) / len(durations_sec) if durations_sec else 0
    min_sec = min(durations_sec) if durations_sec else 0
    max_sec = max(durations_sec) if durations_sec else 0
    total_runtime  = sum(durations_sec)
    bph_overall    = 3600 / avg_sec if avg_sec else 0

    best_batch  = catch_rates.index(max(catch_rates)) + 1 if catch_rates else "-"
    worst_batch = catch_rates.index(min(catch_rates)) + 1 if catch_rates else "-"
    mean_cr     = overall_catch
    variance    = sum((c - mean_cr) ** 2 for c in catch_rates) / len(catch_rates) if catch_rates else 0
    stddev_cr   = variance ** 0.5

    sections = [
        ("SPEED", [
            ("Avg batch time",        f"{avg_sec:.0f}s"),
            ("Fastest batch",         f"{min_sec:.0f}s"),
            ("Slowest batch",         f"{max_sec:.0f}s"),
            ("Total runtime",         f"{total_runtime/60:.1f} min"),
            ("Batches / hour (avg)",  f"{bph_overall:.1f}"),
        ]),
        ("VOLUME", [
            ("Total emails processed", f"{total_emails:,}"),
            ("Total batches",          f"{total_batches}"),
            ("Avg emails / batch",     f"{total_emails/total_batches:.1f}" if total_batches else "—"),
        ]),
        ("FILTER PERFORMANCE", [
            ("Total pre-filtered",     f"{total_pf:,}"),
            ("Total AI calls made",    f"{total_ai:,}"),
            ("Overall catch rate",     f"{overall_catch:.1f}%"),
            ("Best catch rate batch",  f"Batch {best_batch}  ({max(catch_rates):.0f}%)" if catch_rates else "—"),
            ("Worst catch rate batch", f"Batch {worst_batch}  ({min(catch_rates):.0f}%)" if catch_rates else "—"),
            ("Std dev (catch rate)",   f"{stddev_cr:.2f}%"),
        ]),
        ("CLASSIFICATION", [
            ("Archived  (AI p1-2)",    f"{total_arch:,}"),
            ("Read only  (AI p3)",     f"{total_ro:,}"),
            ("Action required",        f"{total_ar:,}"),
            ("Signal-to-noise ratio",  f"{snr:.1f}%  (action / total)"),
        ]),
        ("COST  vs " + COST_MODEL, [
            ("AI calls saved",              f"{total_pf:,}"),
            ("Estimated cost saved",        f"${cost_saved:.4f}"),
            ("Estimated cost of calls made",f"${cost_made:.4f}"),
        ]),
        ("40-YEAR CAREER PROJECTION  (121 emails/day, Litmus 2018: 13.4s/email)", [
            ("Total career emails",         f"{career_emails:,}"),
            ("Pre-filtered at current rate",f"{int(career_pf_count):,}  ({overall_catch:.1f}%)"),
            ("Estimated career cost saved", f"${career_saved:,.2f}  vs {COST_MODEL}"),
            ("Time saved (hours)",          f"{career_time_hrs:,.1f} hrs"),
            ("Time saved (days)",           f"{career_time_days:,.1f} days"),
            ("Time saved (weeks)",          f"{career_time_weeks:,.1f} weeks"),
        ]),
    ]

    # Flatten into rows with section headers as dividers
    table_rows  = []
    row_colors  = []
    for section_title, metrics in sections:
        table_rows.append(("", ""))                        # spacer
        table_rows.append((section_title, ""))             # header
        row_colors.append(BG)
        row_colors.append(GRID)
        for label, value in metrics:
            table_rows.append((label, value))
            row_colors.append(PANEL)

    # Remove leading spacer
    table_rows  = table_rows[1:]
    row_colors  = row_colors[1:]

    Path("graphs").mkdir(exist_ok=True)
    out = f"graphs/{account_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_summary.csv"
    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for label, value in table_rows:
            writer.writerow([label, value])
    print(f"Saved: {out}")


def main():
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("Install matplotlib first: pip install matplotlib")
        sys.exit(1)

    tally_files = find_tally_files()
    if not tally_files:
        print("No tally_log_*.csv files found. Run run_all.py first.")
        sys.exit(0)

    for account_id, path in tally_files:
        print(f"\n── {account_id}  ({path}) ──")
        rows = load(path)
        if not rows:
            print(f"  No data yet.")
            continue
        plot_velocity(rows, account_id)
        plot_efficiency(rows, account_id)
        plot_summary(rows, account_id)


if __name__ == "__main__":
    main()
