"""Outcome-disparity screening over CFPB consumer complaints.

**This is a screening methodology, not a finding about any company.** Disparity screening
flags cells that warrant investigation; it cannot establish that a company treated anyone
unfairly, and nothing here should be read as saying so. The limitations are not a footnote
to this module — they are most of what it is for, and they are stated in full in
analytics/METHODOLOGY.md.

What it computes, for every (company, product) cell with enough complaints to support a
test: the **relief rate** (closed with monetary or non-monetary relief, against closed with
explanation only) and the **timely-response rate**, each with a Wilson interval, each
compared against the rate for the same product across all *other* companies by a
two-proportion z-test, with Benjamini-Hochberg applied across the whole family of tests.

Three design decisions carry the statistics:

**The baseline excludes the cell being tested.** Comparing Capital One's credit-card relief
rate against a product baseline that includes Capital One's own complaints tests a cell
against a number it helped produce. With a large issuer that is a substantial share of the
baseline, and it biases every test toward finding nothing. Each cell is therefore compared
against the same product at every *other* company.

**Benjamini-Hochberg, not Bonferroni.** This is a screen — the cost of a false positive is
an analyst's afternoon, and the cost of a false negative is missing the thing the screen
exists to find. Controlling the false discovery rate is the right trade at this stage;
controlling the family-wise error rate at several hundred tests would leave the screen
unable to flag anything.

**A minimum cell size, applied before testing rather than after.** 1,124 companies x 4
products is mostly cells holding a handful of complaints, where a z-test is invalid and a
Wilson interval spans most of the unit interval. Filtering after computing p-values would
still let those cells inflate the multiple-testing correction and suppress real signals
elsewhere.

Not computed, because the data does not contain it: the **consumer-dispute rate** the build
guide asks for. CFPB stopped publishing `consumer_disputed` in April 2017 and the field is
absent from this extract. It is named here rather than quietly dropped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_SOURCE = ROOT / "data" / "raw" / "cfpb_structured.parquet"

# Below this many complaints in a cell, a two-proportion z-test is not valid and a Wilson
# interval is too wide to act on. 50 is a judgement call, stated rather than hidden: it is
# roughly where the normal approximation behind the z-test becomes defensible at the relief
# rates seen here (~20%), since it puts the expected success count near 10.
MIN_CELL = 50

RELIEF = {"Closed with monetary relief", "Closed with non-monetary relief"}
# "In progress" is neither an outcome nor a non-outcome yet, so it is excluded from the
# denominator rather than counted as "no relief" — counting it would understate relief for
# whichever company happens to have open cases on the extract date.
UNRESOLVED = {"In progress"}


def load(source: Path = DEFAULT_SOURCE) -> pd.DataFrame:
    df = pd.read_parquet(source)
    df = df[~df["company_response"].isin(UNRESOLVED)].copy()
    df["relief"] = df["company_response"].isin(RELIEF)
    df["on_time"] = df["timely"].eq("Yes")
    # 3-digit prefix only: that is the finest geography CFPB publishes, and it is why any
    # geographic reading here is area-level. See METHODOLOGY.md on ecological inference.
    df["zip3"] = df["zip_code"].astype(str).str.extract(r"^(\d{3})", expand=False)
    return df


def _wilson(successes: int, n: int) -> tuple[float, float]:
    from statsmodels.stats.proportion import proportion_confint

    if n == 0:
        return (float("nan"), float("nan"))
    low, high = proportion_confint(successes, n, alpha=0.05, method="wilson")
    return float(low), float(high)


def screen(df: pd.DataFrame, outcome: str = "relief",
           min_cell: int = MIN_CELL) -> pd.DataFrame:
    """One row per (company, product) cell, tested against the same product elsewhere."""
    from statsmodels.stats.proportion import proportions_ztest

    rows = []
    for (company, product), cell in df.groupby(["company", "product"], observed=True):
        n = len(cell)
        if n < min_cell:
            continue
        successes = int(cell[outcome].sum())

        # The baseline: same product, every other company. Excluding the cell is what
        # keeps a large issuer from being compared against itself.
        others = df[(df["product"] == product) & (df["company"] != company)]
        if len(others) < min_cell:
            continue
        base_successes, base_n = int(others[outcome].sum()), len(others)

        stat, p = proportions_ztest([successes, base_successes], [n, base_n])
        low, high = _wilson(successes, n)
        rows.append({
            "company": company, "product": product, "n": n,
            "rate": successes / n,
            "ci_low": low, "ci_high": high,
            "baseline_rate": base_successes / base_n,
            "baseline_n": base_n,
            "difference": successes / n - base_successes / base_n,
            "z": float(stat), "p_value": float(p),
        })

    # Columns declared even when no cell qualified, so a caller can filter or inspect an
    # empty screen without special-casing it. A bare pd.DataFrame([]) has no columns at
    # all and turns "nothing to report" into an AttributeError.
    columns = ["company", "product", "n", "rate", "ci_low", "ci_high",
               "baseline_rate", "baseline_n", "difference", "z", "p_value"]
    out = pd.DataFrame(rows, columns=columns)
    if out.empty:
        return out
    return out.sort_values("p_value").reset_index(drop=True)


def adjust(screened: pd.DataFrame, alpha: float = 0.05) -> pd.DataFrame:
    """Benjamini-Hochberg across the whole family of tests.

    Applied once, over every cell tested, because the family is "every comparison this
    screen ran" — correcting within each product separately would understate the number of
    chances taken and let a cell clear a bar it should not.
    """
    from statsmodels.stats.multitest import multipletests

    if screened.empty:
        return screened
    rejected, q_values, _, _ = multipletests(screened["p_value"], alpha=alpha,
                                             method="fdr_bh")
    out = screened.copy()
    out["q_value"] = q_values
    out["flagged"] = rejected
    return out


def report(df: pd.DataFrame, outcome: str, alpha: float, min_cell: int) -> pd.DataFrame:
    screened = adjust(screen(df, outcome, min_cell), alpha)
    total = len(screened)
    flagged = int(screened["flagged"].sum()) if total else 0
    raw = int((screened["p_value"] < alpha).sum()) if total else 0

    print(f"\n{outcome} rate — {total} cells tested (>= {min_cell} complaints each)")
    print(f"  overall {outcome} rate: {df[outcome].mean():.4f} over {len(df):,} complaints")
    print(f"  significant before correction: {raw}")
    print(f"  flagged after Benjamini-Hochberg at alpha={alpha}: {flagged}")
    # The gap between those two numbers is the reason the correction is here.
    if total:
        print(f"  ({raw - flagged} of the {raw} would have been reported as findings "
              f"without it)")

    # The number that decides whether this screen is usable, printed whether or not it
    # flatters the module. A screen that flags most of what it tests is not identifying
    # anomalies; it is telling you the comparison group is wrong.
    if total:
        share = flagged / total
        med_flagged = screened.loc[screened["flagged"], "difference"].abs().median()
        med_quiet = screened.loc[~screened["flagged"], "difference"].abs().median()
        print(f"  share of tested cells flagged: {share:.0%}  "
              f"(median |difference| {med_flagged:.3f} flagged vs {med_quiet:.3f} not)")
        if share > 0.25:
            print(f"  WARNING: {share:.0%} is too high to read as anomaly detection. The "
                  f"effects are\n           large rather than marginal, so this is "
                  f"heterogeneity between companies,\n           not a power artifact — "
                  f"the product baseline is mixing peer groups that\n           do not "
                  f"belong together. See METHODOLOGY.md, 'Why the peer group is wrong'.")

    if flagged:
        cols = ["company", "product", "n", "rate", "ci_low", "ci_high",
                "baseline_rate", "difference", "q_value"]
        shown = screened[screened["flagged"]].head(15)[cols]
        print()
        print(shown.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return screened


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--outcome", default="relief", choices=["relief", "on_time"])
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--min-cell", type=int, default=MIN_CELL)
    ap.add_argument("--out", default=None, help="write the full screened table as CSV")
    args = ap.parse_args()

    df = load(Path(args.source))
    print(f"loaded {len(df):,} resolved complaints, "
          f"{df['company'].nunique():,} companies, {df['product'].nunique()} products")
    screened = report(df, args.outcome, args.alpha, args.min_cell)

    print("\nThis is a screening methodology, not a finding about any company.")
    print("A flagged cell warrants investigation; it does not establish unfair treatment.")
    print("See analytics/METHODOLOGY.md — the limitations are the substance here.")

    if args.out and not screened.empty:
        screened.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
