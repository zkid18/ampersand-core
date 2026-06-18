"""amperstand-admin classifier retrain — fold user feedback into the model.

Inputs:
  bundled  src/amperstand_core/data/labeled.jsonl  (LLM-bootstrap, immutable)
  bundled  src/amperstand_core/data/holdout.jsonl  (frozen eval, never train)
  user     {data_dir}/.classifier/feedback.jsonl  (deletes captured)

Output:
  user     {data_dir}/.classifier/model.joblib       (new model)
  user     {data_dir}/.classifier/model.joblib.bak   (previous, for rollback)

Safety:
- Holdout is never used for training. Only for comparison.
- New model is evaluated against the same holdout the CURRENT model sees.
- If new accuracy < current accuracy, the swap is REJECTED. Old model stays.
- Old model is always kept as `.bak` after a successful swap.

The watcher (amperstand-email-watch) caches the loaded model in-process, so
after a swap you should restart it: `systemctl restart amperstand-email-watch`.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Iterable

import amperstand_core as _pkg
from amperstand_core.server.admin_cli.config import AdminConfig

log = logging.getLogger(__name__)

PKG_ROOT = Path(_pkg.__file__).resolve().parent
BUNDLED_LABELED = PKG_ROOT / "data" / "labeled.jsonl"
BUNDLED_HOLDOUT = PKG_ROOT / "data" / "holdout.jsonl"
BUNDLED_MODEL = PKG_ROOT / "models" / "newsletter_classifier.joblib"

USER_DIR_RELPATH = Path(".classifier")
USER_FEEDBACK = "feedback.jsonl"
USER_MODEL = "model.joblib"
USER_MODEL_BAK = "model.joblib.bak"

BODY_CHARS = 2000  # mirror scripts/train_classifier.py


def _user_dir(data_dir: Path) -> Path:
    return data_dir / USER_DIR_RELPATH


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("classifier: bad jsonl line in %s, skipping", path)
    return rows


def _build_features_row(row: dict) -> str:
    sender = (row.get("sender") or "").lower()
    subject = row.get("subject") or ""
    body = (row.get("body") or "")[:BODY_CHARS]
    domain = sender.partition("@")[2]
    return f"{sender} {sender} {domain} {domain} {subject} {body}"


def _merge_labels(bootstrap: list[dict], feedback: list[dict]) -> list[dict]:
    """User feedback always wins over bootstrap label for the same doc_id.

    Bootstrap rows have an `id` field (vault doc_id). Feedback rows have
    `id` too. Last-write-wins keyed by id; rows without id are appended.
    """
    by_id: dict[str, dict] = {}
    extras: list[dict] = []

    def _ingest(rows: Iterable[dict]) -> None:
        for r in rows:
            doc_id = r.get("id")
            if doc_id:
                by_id[doc_id] = r
            else:
                extras.append(r)

    _ingest(bootstrap)
    _ingest(feedback)
    return list(by_id.values()) + extras


def _evaluate(model, rows: list[dict]) -> float:
    if not rows:
        return float("nan")
    X = [_build_features_row(r) for r in rows]
    y = [r["label"] for r in rows]
    pred = model.predict(X)
    correct = sum(1 for a, b in zip(y, pred) if a == b)
    return correct / len(rows)


def _build_pipeline():
    # Import here so the admin CLI doesn't pay sklearn import cost for
    # commands that don't need it (stats, backup, etc.).
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    return Pipeline([
        ("tfidf", TfidfVectorizer(
            min_df=2, max_df=0.95, ngram_range=(1, 2),
            sublinear_tf=True, max_features=20000,
            strip_accents="unicode", lowercase=True,
        )),
        ("clf", LogisticRegression(
            C=1.0, class_weight="balanced", max_iter=2000,
            solver="liblinear", random_state=42,
        )),
    ])


def _load_current(user_dir: Path):
    """Load whichever model the watcher would load — user-override or bundled."""
    import joblib
    user_path = user_dir / USER_MODEL
    if user_path.exists():
        return joblib.load(user_path), user_path
    if BUNDLED_MODEL.exists():
        return joblib.load(BUNDLED_MODEL), BUNDLED_MODEL
    return None, None


def run(config: AdminConfig, *, force: bool = False) -> None:
    """Train a candidate, evaluate, promote if it's not worse.

    `force=True` skips the regression check and always promotes.
    """
    user_dir = _user_dir(config.data_dir)
    user_dir.mkdir(parents=True, exist_ok=True)

    bootstrap = _read_jsonl(BUNDLED_LABELED)
    holdout = _read_jsonl(BUNDLED_HOLDOUT)
    feedback = _read_jsonl(user_dir / USER_FEEDBACK)

    if not bootstrap and not feedback:
        raise SystemExit("no training data found (bundled or user)")
    if not holdout:
        raise SystemExit(
            f"missing holdout at {BUNDLED_HOLDOUT} — refusing to retrain without "
            f"an evaluation baseline"
        )

    merged = _merge_labels(bootstrap, feedback)
    print(
        f"Training data: bootstrap={len(bootstrap)} + user={len(feedback)} "
        f"→ merged={len(merged)} (after dedup by doc id)"
    )
    print(f"Holdout: {len(holdout)} examples (frozen)")

    X = [_build_features_row(r) for r in merged]
    y = [r["label"] for r in merged]

    candidate = _build_pipeline()
    candidate.fit(X, y)
    new_acc = _evaluate(candidate, holdout)
    print(f"Candidate model: holdout accuracy = {new_acc:.3f}")

    current, current_path = _load_current(user_dir)
    if current is None:
        print("No current model — promoting candidate.")
        cur_acc = float("nan")
    else:
        cur_acc = _evaluate(current, holdout)
        print(f"Current model:   holdout accuracy = {cur_acc:.3f}  (from {current_path})")
        if not force and new_acc < cur_acc:
            print(
                f"\nREJECTED: candidate ({new_acc:.3f}) is worse than current "
                f"({cur_acc:.3f}). Old model stays. Use --force to override."
            )
            return

    # Promote: write candidate to user model path, keep previous as .bak.
    import joblib
    new_path = user_dir / USER_MODEL
    bak_path = user_dir / USER_MODEL_BAK
    if new_path.exists():
        shutil.copy2(new_path, bak_path)
    joblib.dump(candidate, new_path)
    size_kb = new_path.stat().st_size / 1024

    print(f"\nPROMOTED: wrote {new_path} ({size_kb:.1f} KB)")
    if bak_path.exists():
        print(f"  previous → {bak_path}")
    print(
        "\nReminder: the watcher caches the loaded model in-process.\n"
        "  systemctl restart amperstand-email-watch"
    )


def run_diff(config: AdminConfig) -> None:
    """Show per-example holdout disagreements between current and candidate.

    Useful when retrain regresses: tells you *which* holdout examples the
    candidate flipped, so you can judge whether the bootstrap or your
    feedback is right.
    """
    user_dir = _user_dir(config.data_dir)
    bootstrap = _read_jsonl(BUNDLED_LABELED)
    holdout = _read_jsonl(BUNDLED_HOLDOUT)
    feedback = _read_jsonl(user_dir / USER_FEEDBACK)

    if not holdout:
        raise SystemExit(f"missing holdout at {BUNDLED_HOLDOUT}")

    merged = _merge_labels(bootstrap, feedback)
    X_train = [_build_features_row(r) for r in merged]
    y_train = [r["label"] for r in merged]

    candidate = _build_pipeline()
    candidate.fit(X_train, y_train)

    current, current_path = _load_current(user_dir)
    if current is None:
        raise SystemExit("no current model to diff against")

    print(f"Current  : {current_path}")
    print(f"Candidate: trained on bootstrap={len(bootstrap)} + user={len(feedback)} → merged={len(merged)}")
    print(f"Holdout  : {len(holdout)} examples\n")

    X_hold = [_build_features_row(r) for r in holdout]
    y_hold = [r["label"] for r in holdout]
    p_cur = current.predict(X_hold)
    p_new = candidate.predict(X_hold)

    flips = []  # (truth, cur_pred, new_pred, sender, subject)
    for truth, cur, new, row in zip(y_hold, p_cur, p_new, holdout):
        if cur != new:
            flips.append((truth, cur, new, row.get("sender", ""), row.get("subject", "")))

    if not flips:
        print("No disagreements on the holdout — current and candidate agree everywhere.")
        return

    # Bucket: cur_correct→new_wrong (BAD), cur_wrong→new_correct (GOOD), both_wrong_different (mixed)
    bad = [f for f in flips if f[1] == f[0] and f[2] != f[0]]
    good = [f for f in flips if f[1] != f[0] and f[2] == f[0]]
    mixed = [f for f in flips if f[1] != f[0] and f[2] != f[0]]

    print(f"Total disagreements: {len(flips)}")
    print(f"  good (candidate corrects a current mistake):  {len(good)}")
    print(f"  bad  (candidate breaks a correct current call): {len(bad)}")
    print(f"  mixed (both wrong, in different ways):         {len(mixed)}")
    print(f"  net  = good - bad = {len(good) - len(bad)} (positive → promote)\n")

    def _show(name: str, rows: list) -> None:
        if not rows:
            return
        print(f"── {name} ──")
        for truth, cur, new, sender, subject in rows:
            print(f"  truth={truth}  cur→{cur}  new→{new}   {sender:45.45}  {subject[:55]}")
        print()

    _show("GOOD (candidate fixed)", good)
    _show("BAD (candidate broke)", bad)
    _show("MIXED (both wrong)", mixed)


def run_domains(config: AdminConfig) -> None:
    """Print the effective newsletter + promo domain lists, with provenance.

    Helps the user find and edit the config files. Bundled defaults ship
    with the package; user overrides extend them additively at
    {data_dir}/.classifier/newsletter_domains.yaml.
    """
    # Make sure we resolve relative to the configured data_dir, not whatever
    # AMPERSTAND_DATA_DIR the admin shell inherited.
    import os
    os.environ["AMPERSTAND_DATA_DIR"] = str(config.data_dir)

    from amperstand_core.newsletter_domains import load_domains, reload
    reload()  # ignore process-cached state
    d = load_domains()

    print("Bundled defaults:", d.bundled_path)
    if d.user_path:
        print("User override:   ", d.user_path)
    else:
        print(f"User override:    (none — create {config.data_dir}/.classifier/newsletter_domains.yaml to extend)")
    print()

    def _section(label: str, items: frozenset[str], src_prefix: str) -> None:
        print(f"{label} ({len(items)}):")
        if not items:
            print("  (empty)")
            return
        for dom in sorted(items):
            src = d.sources.get(f"{src_prefix}:{dom}", "?")
            tag = " [user]" if src == "user" else ""
            print(f"  {dom}{tag}")
        print()

    _section("newsletter_domains (hard ACCEPT)", d.newsletter_domains, "newsletter")
    _section("promo_domains (hard REJECT)", d.promo_domains, "promo")

    print(
        "Add domains to the user file under `newsletter_domains:` or "
        "`promo_domains:`.\nMerge is ADDITIVE — to remove a bundled default, "
        "edit the bundled file and redeploy."
    )
