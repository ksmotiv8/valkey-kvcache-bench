# SPDX-License-Identifier: Apache-2.0
"""Cross-tier comparison of bench_corpus_e2e runs.

Reads the JSON records written by ``bench_corpus_e2e.py --json`` and prints the
two tables the tier question actually turns on:

1. **Single node** -- cold vs cached TTFT per tier on one vLLM node. Local tiers
   are expected to win here; a network tier pays for the hop.
2. **Fleet** -- the cold pass on one node and the cached pass on another. A
   node-local tier cannot serve a node that did not compute the KV, so ``cpu``
   and ``disk`` should collapse to no confirmed reuse while ``valkey`` holds.
   The delta between the two tables is the finding.

Usage::

    python benchmarks/compare_tiers.py results/tiers

File naming follows the runner: ``<label>_<tier>.json`` (e.g. ``fleet_disk.json``).
"""

# Standard
from pathlib import Path
from typing import Dict, List
import argparse
import json
import sys

TIER_ORDER = ("cpu", "disk", "valkey")


def load_runs(results_dir: Path) -> Dict[str, Dict[str, dict]]:
    """Return {label: {tier: record}} for every ``<label>_<tier>.json`` found."""
    runs: Dict[str, Dict[str, dict]] = {}
    for path in sorted(results_dir.glob("*.json")):
        stem = path.stem
        if "_" not in stem:
            print(f"  WARNING: skipping {path.name} (expected <label>_<tier>.json)")
            continue
        label, tier = stem.rsplit("_", 1)
        try:
            runs.setdefault(label, {})[tier] = json.loads(path.read_text())
        except json.JSONDecodeError as exc:  # noqa: BLE001 - reported, not fatal
            print(f"  WARNING: {path.name} is not valid JSON - {exc}")
    return runs


def _table(label: str, by_tier: Dict[str, dict]) -> None:
    print(f"\n{label}")
    print("-" * 78)
    print(f"{'tier':<10}{'cold ms':>10}{'cached ms':>12}{'speedup':>10}"
          f"{'reuse':>12}{'docs':>7}")
    for tier in [t for t in TIER_ORDER if t in by_tier] + \
                [t for t in by_tier if t not in TIER_ORDER]:
        r = by_tier[tier]
        reuse = f"{r['l2_confirmed']}/{r['docs']}"
        print(f"{tier:<10}{r['cold_median_ms']:>10.1f}{r['cached_median_ms']:>12.1f}"
              f"{r['speedup_median']:>9.2f}x{reuse:>12}{r['docs']:>7}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results_dir", help="directory of *.json run records")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"ERROR: {results_dir} is not a directory")
        sys.exit(1)

    runs = load_runs(results_dir)
    if not runs:
        print(f"ERROR: no run records found under {results_dir}")
        sys.exit(1)

    # Sanity labels first, then single, then fleet: cheapest evidence to
    # strongest, same order they were produced in.
    for label in sorted(runs, key=lambda s: {"sanity": 0, "single": 1, "fleet": 2}.get(s, 3)):
        _table(label, runs[label])

    # ── The comparison the post rests on ──
    single, fleet = runs.get("single"), runs.get("fleet")
    if single and fleet:
        print("\n" + "=" * 78)
        print("Fleet vs single node (cached pass on a node that did NOT compute the KV)")
        print("-" * 78)
        print(f"{'tier':<10}{'single reuse':>15}{'fleet reuse':>14}"
              f"{'single x':>11}{'fleet x':>10}")
        shared: List[str] = []
        for tier in [t for t in TIER_ORDER if t in single and t in fleet]:
            s, f = single[tier], fleet[tier]
            print(f"{tier:<10}{s['l2_confirmed']:>8}/{s['docs']:<6}"
                  f"{f['l2_confirmed']:>7}/{f['docs']:<6}"
                  f"{s['speedup_median']:>10.2f}x{f['speedup_median']:>9.2f}x")
            if f["l2_confirmed"] > 0:
                shared.append(tier)
        print("-" * 78)
        if shared:
            print(f"Tiers that served a second node: {', '.join(shared)}")
        else:
            print("No tier served the second node. If valkey is among them, the "
                  "fleet run did not reach the remote tier - check that the "
                  "cached pass really targeted gpu-b.")
        print("=" * 78)


if __name__ == "__main__":
    main()
