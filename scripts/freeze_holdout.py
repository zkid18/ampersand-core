"""Carve a stratified holdout from labeled.jsonl. ONE-SHOT.

After running this once, you have:
- src/ampersand_core/data/labeled.jsonl   training set
- src/ampersand_core/data/holdout.jsonl   frozen eval set (never train on)

The retrain command evaluates every candidate model against holdout.jsonl
and only promotes if accuracy doesn't regress. That's the load-bearing
safety check — don't recreate holdout.jsonl after deploys, or you destroy
the audit trail.

  scripts/freeze_holdout.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "scripts" / "data" / "labeled.jsonl"
DEFAULT_PACKAGE_DATA = ROOT / "src" / "ampersand_core" / "data"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_PACKAGE_DATA)
    p.add_argument("--holdout-n", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if not args.source.exists():
        print(f"error: {args.source} not found", file=sys.stderr)
        return 1

    with args.source.open() as f:
        rows = [json.loads(l) for l in f if l.strip()]

    if not rows:
        print("error: source is empty", file=sys.stderr)
        return 1

    # Stratify by label so holdout has both classes in proportion
    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(r["label"], []).append(r)

    rng = random.Random(args.seed)
    holdout: list[dict] = []
    train: list[dict] = []
    for label, group in by_label.items():
        rng.shuffle(group)
        n_holdout = max(1, int(round(args.holdout_n * len(group) / len(rows))))
        holdout.extend(group[:n_holdout])
        train.extend(group[n_holdout:])

    args.out_dir.mkdir(parents=True, exist_ok=True)
    train_path = args.out_dir / "labeled.jsonl"
    holdout_path = args.out_dir / "holdout.jsonl"

    if holdout_path.exists():
        print(
            f"error: {holdout_path} already exists. This script is one-shot — "
            f"if you really want to recreate the holdout, delete it manually first.",
            file=sys.stderr,
        )
        return 2

    with train_path.open("w") as f:
        for r in train:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with holdout_path.open("w") as f:
        for r in holdout:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def counts(rs: list[dict]) -> str:
        k = sum(1 for r in rs if r["label"] == "KEEP")
        s = sum(1 for r in rs if r["label"] == "SKIP")
        return f"KEEP={k} SKIP={s}"

    print(f"  train   → {train_path}   n={len(train)} ({counts(train)})")
    print(f"  holdout → {holdout_path} n={len(holdout)} ({counts(holdout)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
