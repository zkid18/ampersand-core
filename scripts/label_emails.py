"""Label captured email docs as KEEP/SKIP via gpt-4o-mini, write JSONL.

One-shot labeling pass used to bootstrap training data for the newsletter
classifier. Inputs come from the vault HTTP API; output is a JSONL file
ready for scripts/train_classifier.py.

  scripts/label_emails.py --n 30  --out scripts/data/labeled.spot.jsonl
  scripts/label_emails.py --n 500 --out scripts/data/labeled.jsonl

Env:
  AMPERSAND_API_KEY  (vault auth)
  OPENAI_API_KEY     (judge)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from openai import OpenAI

VAULT_URL_DEFAULT = "http://68.183.29.223"
JUDGE_MODEL = "gpt-4o-mini"
BODY_SNIPPET_CHARS = 800

PROMPT = """You are filtering email for a personal research vault.

KEEP: editorial content worth re-reading — essays, articles, reported
pieces, analysis, interviews, technical writeups, industry digests where
the body itself contains substantive prose.

SKIP: transactional or promotional — order confirmations, "your plan is
pending", loyalty/rewards updates, merchandise promos, shipping notices,
account / security alerts, social-app notifications (GitHub events,
LinkedIn pokes, etc.), short marketing teasers that just link out to a sale.

Email:
From: {sender}
Subject: {subject}
Body (first ~800 chars after markdown-to-text):
{body}

Answer with exactly one word: KEEP or SKIP."""


def fetch_doc_ids(client: httpx.Client, vault_url: str, n_target: int) -> list[str]:
    """Paginate /vault, return IDs whose source is `email://...`."""
    ids: list[str] = []
    cursor: str | None = None
    while len(ids) < n_target * 3:  # over-fetch so random.sample has room
        params: dict = {"limit": 500, "order": "captured"}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{vault_url}/vault", params=params)
        r.raise_for_status()
        data = r.json()
        for item in data["items"]:
            if (item.get("source") or "").startswith("email://"):
                ids.append(item["id"])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return ids


def fetch_doc(client: httpx.Client, vault_url: str, doc_id: str) -> dict | None:
    r = client.get(f"{vault_url}/vault/{doc_id}")
    if r.status_code != 200:
        return None
    return r.json()


def strip_markdown(body: str) -> str:
    s = re.sub(r"!\[.*?\]\([^)]*\)", " ", body)        # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)     # links → label
    s = re.sub(r"^#+\s*", "", s, flags=re.MULTILINE)   # heading markers
    s = re.sub(r"`+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def llm_label(oai: OpenAI, sender: str, subject: str, body: str) -> tuple[str, str]:
    body_snippet = strip_markdown(body)[:BODY_SNIPPET_CHARS]
    prompt = PROMPT.format(sender=sender, subject=subject, body=body_snippet)
    rsp = oai.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=4,
    )
    raw = (rsp.choices[0].message.content or "").strip().upper()
    if raw.startswith("KEEP"):
        label = "KEEP"
    elif raw.startswith("SKIP"):
        label = "SKIP"
    else:
        label = "?"
    return label, raw


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--n", type=int, default=30, help="Number of docs to label")
    p.add_argument("--out", type=Path, required=True, help="Output JSONL path")
    p.add_argument("--vault-url", default=VAULT_URL_DEFAULT)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    vault_key = os.environ.get("AMPERSAND_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not vault_key:
        print("error: AMPERSAND_API_KEY not set", file=sys.stderr)
        return 1
    if not openai_key:
        print("error: OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)

    headers = {"Authorization": f"Bearer {vault_key}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        print(f"Listing email docs from {args.vault_url}…", file=sys.stderr)
        ids = fetch_doc_ids(client, args.vault_url, args.n)
        print(f"  found {len(ids)} email docs, sampling {args.n}", file=sys.stderr)
        random.seed(args.seed)
        chosen = random.sample(ids, min(args.n, len(ids)))

        print("Fetching doc bodies…", file=sys.stderr)
        docs: list[dict] = []
        for i, doc_id in enumerate(chosen, 1):
            d = fetch_doc(client, args.vault_url, doc_id)
            if d:
                docs.append(d)
            if i % 50 == 0:
                print(f"  {i}/{len(chosen)}", file=sys.stderr)

    print(
        f"Labeling {len(docs)} docs with {JUDGE_MODEL} "
        f"(concurrency={args.concurrency})…",
        file=sys.stderr,
    )
    oai = OpenAI(api_key=openai_key)

    results: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {}
        for d in docs:
            sender = (d.get("extra") or {}).get("sender_email", "")
            subject = d.get("title", "")
            body = d.get("body") or ""
            fut = pool.submit(llm_label, oai, sender, subject, body)
            futs[fut] = d
        for i, fut in enumerate(as_completed(futs), 1):
            d = futs[fut]
            try:
                label, raw = fut.result()
            except Exception as e:
                print(f"  error on {d['id']}: {e}", file=sys.stderr)
                continue
            results.append({
                "id": d["id"],
                "sender": (d.get("extra") or {}).get("sender_email", ""),
                "subject": d.get("title", ""),
                "body": (d.get("body") or "")[:4000],
                "label": label,
                "raw": raw,
            })
            if i % 25 == 0:
                rate = i / (time.time() - t0)
                print(f"  {i}/{len(docs)}  ({rate:.1f}/s)", file=sys.stderr)

    with args.out.open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    keeps = sum(1 for r in results if r["label"] == "KEEP")
    skips = sum(1 for r in results if r["label"] == "SKIP")
    unk = sum(1 for r in results if r["label"] == "?")
    print(f"\nWrote {len(results)} labels to {args.out}", file=sys.stderr)
    print(f"  KEEP: {keeps}  SKIP: {skips}  ?: {unk}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
