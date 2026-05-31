"""Train TF-IDF + LogReg newsletter classifier from a labeled JSONL file.

Inputs:
  scripts/data/labeled.jsonl   produced by scripts/label_emails.py

Outputs:
  src/ampersand_core/models/newsletter_classifier.joblib   trained pipeline
  scripts/data/training_report.txt                          metrics dump

  scripts/train_classifier.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
PKG_DATA = ROOT / "src" / "ampersand_core" / "data"
DEFAULT_DATA = PKG_DATA / "labeled.jsonl"
DEFAULT_MODEL = ROOT / "src" / "ampersand_core" / "models" / "newsletter_classifier.joblib"
DEFAULT_REPORT = ROOT / "scripts" / "data" / "training_report.txt"

# Body chars used as features. We mirror this at inference time, so changing
# it here changes both training and serving.
BODY_CHARS = 2000


def build_features(row: dict) -> str:
    sender = (row.get("sender") or "").lower()
    subject = row.get("subject") or ""
    body = (row.get("body") or "")[:BODY_CHARS]
    # Sender carries strong signal; weight it by repetition so TF-IDF picks
    # it up over the much longer body. We also keep the domain explicitly so
    # bigrams can pick up combos like "shein edm".
    domain = sender.partition("@")[2]
    return f"{sender} {sender} {domain} {domain} {subject} {body}"


def load_labeled(path: Path) -> tuple[list[str], list[str]]:
    X, y = [], []
    with path.open() as f:
        for line in f:
            row = json.loads(line)
            if row["label"] not in ("KEEP", "SKIP"):
                continue
            X.append(build_features(row))
            y.append(row["label"])
    return X, y


def build_pipeline() -> Pipeline:
    return Pipeline([
        ("tfidf", TfidfVectorizer(
            min_df=2,
            max_df=0.95,
            ngram_range=(1, 2),
            sublinear_tf=True,
            max_features=20000,
            strip_accents="unicode",
            lowercase=True,
        )),
        ("clf", LogisticRegression(
            C=1.0,
            class_weight="balanced",
            max_iter=2000,
            solver="liblinear",
            random_state=42,
        )),
    ])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--report-out", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--test-size", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if not args.data.exists():
        print(f"error: {args.data} not found — run label_emails.py first", file=sys.stderr)
        return 1

    X, y = load_labeled(args.data)
    n_keep = sum(1 for v in y if v == "KEEP")
    n_skip = sum(1 for v in y if v == "SKIP")
    print(f"Loaded {len(X)} labeled examples — KEEP={n_keep} SKIP={n_skip}")

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y,
        test_size=args.test_size,
        stratify=y,
        random_state=args.seed,
    )

    # 1) Held-out evaluation
    eval_pipe = build_pipeline()
    eval_pipe.fit(X_tr, y_tr)
    y_pred = eval_pipe.predict(X_te)
    acc = accuracy_score(y_te, y_pred)
    cm = confusion_matrix(y_te, y_pred, labels=["KEEP", "SKIP"])
    report = classification_report(y_te, y_pred, labels=["KEEP", "SKIP"], digits=3)

    lines = [
        f"Held-out eval (test_size={args.test_size}, n={len(y_te)}):",
        f"  accuracy = {acc:.3f}",
        "",
        "Confusion matrix (rows=true, cols=pred; labels=[KEEP, SKIP]):",
        f"  {cm[0]}",
        f"  {cm[1]}",
        "",
        report,
    ]
    print("\n".join(lines))

    # 2) Refit on full data for shipping
    final = build_pipeline()
    final.fit(X, y)
    args.model_out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, args.model_out)
    size_kb = args.model_out.stat().st_size / 1024
    print(f"\nFinal model written to {args.model_out} ({size_kb:.1f} KB)")

    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text("\n".join(lines) + f"\n\nFinal model: {args.model_out} ({size_kb:.1f} KB)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
