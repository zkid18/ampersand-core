# Amperstand

> **Build a content hub of sources you actually trust — so your agents stop digging through SEO landfill.**

When your agent researches something, it gets the open web's first page: SEO listicles, AI summaries of AI summaries, content farms outranking the people who actually know things. You already know the writers worth reading. Amperstand makes *those* the corpus your agents query — instead of whatever Google ranks today.

Capture once, query forever. Plain markdown on disk you own. Plug into any agent.

## Use it anywhere

<!-- TODO: replace each bullet with a 2-line gif / screenshot once the assets exist -->

- 🖥️ **CLI** — `amperstand capture <url>`. One command, markdown in your vault.
- 🤖 **Telegram bot** — DM a link or text, lands as a doc within seconds.
- 🧷 **Browser extension** — Chrome MV3 clipper, one-click on any page.
- 💬 **Notebook UI** — chat with a working set of vault docs, citations clickable back to the source.
- ✉️ **Email watcher** — IMAP IDLE captures newsletters as they arrive (multi-account, parallel; classifier filters out promos/transactional and learns from your deletes).
- 🦾 **Your agent** — `POST /vault/search/hybrid` from Cursor, Claude Desktop, n8n, custom code.

---

## Capture

Type one command, get a markdown file in your vault. The right extractor is picked automatically by URL.

```bash
amperstand capture https://stratechery.com/2025/cursors-pricing-pivot/
amperstand capture https://www.youtube.com/watch?v=e_NDFEWGW1w   # YouTube → transcript (audio-fallback if no captions)
amperstand capture https://www.linkedin.com/in/some-profile/    # LinkedIn extractor
amperstand capture https://twitter.com/user/status/123          # Twitter/X (via proxy or fxtwitter)
```

Other capture surfaces (same backend, same vault):

- **Telegram bot** — message a URL or plain text to your bot, it lands as a doc
- **Browser extension** (Chrome MV3, in `amperstand-extension/`) — one-click clip the page you're on
- **Web UI** (`/ui/`) — paste, list, search from any browser
- **POST /capture** — for your own scripts

---

## Subscribe

The "RSS for agents" pitch. Add a feed, then `sync` pulls every new item into the vault on a timer.

```bash
amperstand feed add https://stratechery.com/feed --name stratechery
amperstand feed add https://www.techcrunch.com/feed/
amperstand feed list
amperstand feed sync     # pulls new items into the vault
```

Roadmap (the syntax above gestures at what isn't built yet):

- ⏳ `amperstand follow @some-tg-channel` — Telegram channel ingestion. Stub today (`amperstand capture <tg-link>` works one at a time).
- ⏳ `amperstand follow github:trending/python` — GitHub trending watch.
- ⏳ `amperstand follow podcast:<rss>` — podcasts (today: YouTube only).

---

## Email / newsletters

The original capture path. Watches an IMAP mailbox and writes every newsletter into the vault.

```bash
amperstand email setup       # interactive, asks for IMAP creds
amperstand email list        # show configured accounts
amperstand email watch       # IMAP IDLE, fires when new mail arrives
amperstand email sync        # one-shot pull of unread
```

**Multi-account** — call `setup` once per inbox (Yahoo + Gmail + ProtonMail Bridge + your work IMAP), the watcher runs N inboxes in parallel.

### The filter — why your vault doesn't fill with Shein promos

Every incoming email goes through three buckets:

| Bucket | Logic | Cost |
|---|---|---|
| **1. Hard reject** | Sender domain in `promo_domains` (Klaviyo, Shopify, …) or starts with `noreply@` | free, instant |
| **2. Hard accept** | Sender domain in `newsletter_domains` (Substack, Beehiiv, Ghost, …) | free, instant |
| **3. Classifier** | Email has `List-Id` header → TF-IDF + LogReg judges KEEP/SKIP | ~1ms, local |

Domain lists live in `src/amperstand_core/data/newsletter_domains.yaml` — **edit that file to tune the defaults**, or drop a YAML at `{AMPERSTAND_DATA_DIR}/.classifier/newsletter_domains.yaml` to extend additively without forking. Run `amperstand-admin classifier domains` to see what's currently in effect and which entries came from where.

The classifier ships pre-trained on 400 LLM-bootstrapped labels (gpt-4o-mini judging "editorial vs promotional"). New installs get reasonable behavior out of the box — no labeling work required.

### Learning from your deletes

The web UI doc view has a **delete button**. For email-sourced docs, deleting writes a SKIP example to `{AMPERSTAND_DATA_DIR}/.classifier/feedback.jsonl`. When you've banked ~10-20 deletes:

```bash
amperstand-admin classifier retrain
# trains a candidate from bundled + feedback labels,
# evaluates against a frozen 100-doc holdout,
# only promotes if it doesn't regress (--force overrides)

systemctl restart amperstand-email-watch   # picks up the new model
```

The frozen holdout is the load-bearing safety check — bad feedback can't silently degrade the model. If retrain rejects a candidate, run `amperstand-admin classifier diff` to see exactly which holdout examples it would have flipped.

Model resolution order at runtime:
1. `{AMPERSTAND_DATA_DIR}/.classifier/model.joblib` — your retrained model
2. `src/amperstand_core/models/newsletter_classifier.joblib` — bundled bootstrap

The bundled file is never overwritten by retraining, and your retrained file is never overwritten by code deploys. They live in different paths on purpose.

**Auth honesty**: there's no OAuth flow yet. You pass an IMAP password (app password for Gmail/Yahoo) and it sits in `/etc/amperstand/env` (mode `0640`) on the server. That's fine for a single-tenant personal box; it's not fine for a hosted multi-user product. If you care about secrets management, the right next step is plugging in a password manager (1Password CLI, age-encrypted file, Vault) — open issue.

---

## Search

Three modes from the same engine, layered for both **recall** and **precision**.

```bash
amperstand search "brazilian funk miami bass"
# or directly:
curl -X POST $AMPERSTAND_BASE_URL/vault/search/hybrid \
  -H "Authorization: Bearer $AMPERSTAND_API_KEY" \
  -d '{"q":"...", "limit":10, "rerank":true}'
```

| Mode | What | When |
|---|---|---|
| **`fts`** | BM25 over FTS5, exact terms, boolean ops | Names, error messages, quoted phrases |
| **`semantic`** | KNN over OpenAI 512-dim Matryoshka embeddings (sqlite-vec) | Conceptual queries, cross-language |
| **`hybrid`** | Reciprocal Rank Fusion of FTS + semantic | The default — best of both |
| **`hybrid + rerank=true`** | + gpt-4o-mini judges top-30 | When precision matters; ~1s slower, ~$0.001/query |

Cross-language tested: English queries return Russian and Portuguese hits when the topic spans them.

---

## Chat with the vault

> ⚠️ **WIP — notebook UI not yet public.** The `POST /chat` endpoint works today — **but only with `OPENAI_API_KEY` set on the server.** Without it, `/chat`, `/vault/search/semantic`, and `/vault/search/hybrid` all return 503. Set the key in `/etc/amperstand/env` and restart the server. (The bootstrap will prompt you for it as of v0.x; older installs need to add it manually.)

The companion project `amperstand-notebook/` is a three-pane local UI:
- **Left**: deep search → tick docs into a working set
- **Left** (below): paste URL/YouTube → captures into vault → auto-adds to working set
- **Right**: chat panel, answers cite `[doc_id]` markers clickable back to the vault doc

Backed by `POST /chat` — streaming RAG, cites only docs you selected, refuses to invent.

---

## Obsidian sync

The vault on disk is just markdown files with YAML frontmatter — Obsidian opens it as-is. Two-way sync is just `git pull && git push` against the vault directory (or `rsync`/`syncthing` if you don't want git history). Captures from any surface (CLI, bot, extension, email) appear in your Obsidian notebook within seconds.

---

## Architecture

```
                       ┌───────────────────────────────┐
       capture ───────►│   amperstand-core (FastAPI)    │
       surfaces        │   /vault, /capture, /chat,    │
                       │   /search, /feeds, /ui        │
                       └──┬─────────────┬──────────────┘
                          │             │
                ┌─────────▼──┐    ┌─────▼──────────────┐
                │  Markdown  │    │  SQLite indexes    │
                │  vault     │    │  - FTS5 (BM25)     │
                │  (files)   │    │  - sqlite-vec (KNN)│
                │            │    │  - meta_index      │
                └────────────┘    └────────────────────┘
```

**Where the data lives**: your vault is just markdown files on disk. By default on the server: `/var/lib/amperstand/vault/` (one file per doc, hash-addressed under `.store/by-id/`, organised by year/month for browsing). You can `tar` it, `git` it, `rsync` it. Nothing is locked into a database — the SQLite files are *derived* indexes that can be rebuilt from the markdown in minutes.

**API auth**: a single shared bearer token, `AMPERSTAND_API_KEY`, lives in `/etc/amperstand/env` (mode `0640`, owned by the `amperstand` user). Every `/vault/*`, `/capture`, `/chat` request must include `Authorization: Bearer <key>`. The bot, the CLI, the extension, the notebook all carry the same key. **There's no per-user auth.** Rotate via `amperstand-admin rotate-key`.

**HTTP only** today (raw IP, no TLS) — fine for a personal box you only hit from your own network, **not fine** for a public deployment. TLS is a one-line Caddyfile swap (`Caddyfile.tls` is in `deploy/`) once you point a domain at the box.

**Anti-bot / proxying**: news sites and Twitter/X block datacenter IPs aggressively. Amperstand routes those fetches through a residential proxy (we use [Webshare](https://www.webshare.io) — `AMPERSTAND_HTTP_PROXY=http://user:pass@host:port` in the env). If you have opinions about a different provider (Bright Data, IPRoyal, your own self-hosted Squid), it's literally a single env var to swap. The proxy is also where YouTube transcript fetching is routed because Google rate-limits cloud IPs hard.

**Audio transcription**: for YouTube videos without captions, we fall back to `yt-dlp` → OpenAI Whisper. Off by default (`AMPERSTAND_YOUTUBE_AUDIO_FALLBACK=1` to enable). ~$0.006/min of audio.

**Embeddings**: OpenAI `text-embedding-3-small` at 512-dim (Matryoshka) for vector search. ~$0.40 to embed 6,800 docs once; pennies/day after. Local-model alternative (BGE/Nomic via llama.cpp) is on the wish list but not built.

---

## Plug it into your agents

Three integration patterns, in order of friction:

1. **Drop the prompt** — `prompts/vault-agent.md` is a ready-to-paste system prompt that teaches any LLM how to query the vault HTTP API. Works in Custom GPTs, Claude Projects, Cursor `.cursorrules`, n8n LLM nodes.

2. **Call `/chat`** — POST your conversation history + a working set of doc IDs, get back a streamed RAG answer with citations. ~150 lines of glue from any language.

3. **Use the search endpoints directly** — `POST /vault/search/hybrid` returns ranked passages with snippets. Your agent does its own reasoning over them.

A future MCP server (so Claude Desktop / Cursor can call the vault as a native tool) is on the roadmap.

---

## What you'll need (external dependencies & costs)

Amperstand is mostly self-contained Python + SQLite, but a few features lean on outside services. Some are required, some are optional — here's the honest breakdown.

### Required to capture + run FTS search

| Thing | Why | Cost | Notes |
|---|---|---|---|
| **Python 3.10+** | runtime | free | nothing exotic |
| **SQLite with `load_extension` enabled** | `sqlite-vec` needs it for vectors | free | Linux distros + Homebrew Python ship it on. **macOS python.org Python ships it OFF** — use `brew install python` if you're on Mac. |
| **A box to run it on** | hosting | $6/mo (DO droplet) / free (Raspberry Pi / laptop / NAS) | Personal box is fine. The deploy docs assume a 1 GB DigitalOcean droplet. |

### Required for semantic search, chat, and audio fallback

| Thing | Why | Cost | What breaks if you skip it |
|---|---|---|---|
| **OpenAI API key** | embeddings (`text-embedding-3-small`), chat (`gpt-4o-mini`), Whisper (audio transcription) | ~$0.40 to embed 6,800 docs once. Then pennies/day for new captures. ~$0.001 per chat query. ~$0.006/min of Whisper. | `mode=semantic` / `mode=hybrid` / `rerank=true` / `POST /chat` / no-caption YouTube fallback all become unavailable. **BM25 search still works.** |

> If you want a local-models alternative (BGE / Nomic via llama.cpp) instead of OpenAI, it's on the roadmap but not built yet. PR welcome.

### Strongly recommended if you'll capture YouTube or anti-bot sites

| Thing | Why | Cost | What breaks if you skip it |
|---|---|---|---|
| **Residential proxy** ([Webshare](https://www.webshare.io) is what I use; Bright Data / IPRoyal / your own Squid all work) | Google rate-limits cloud IPs on the YouTube transcript API; Cloudflare and Russian news sites do similar | $25-50/mo for a residential plan | YouTube captures fail intermittently from cloud IPs. JS-heavy / Cloudflare-walled sites return challenge pages. **From your home IP this is usually not a problem.** Set `AMPERSTAND_HTTP_PROXY=http://user:pass@host:port` to enable. |

### Optional, surface-specific

| If you want… | You also need… | Cost |
|---|---|---|
| Telegram bot | A bot token from [@BotFather](https://t.me/BotFather). Free. | free |
| Email watcher | An IMAP-capable mail account + an **app password** (Gmail/Yahoo require 2FA first) | free |
| Browser extension (Chrome MV3 clipper) | A Chrome / Brave / Edge with Developer Mode on | free |
| Notebook chat UI | The OpenAI key above (for `/chat`) | covered above |
| TLS + domain (instead of HTTP-on-IP) | A domain, an A-record, the bundled Caddyfile | $10-20/yr for the domain |

### Total cost: a personal deployment, all-in

- **Bare minimum (FTS only, no proxy, your laptop)**: $0
- **Recommended (droplet + OpenAI + proxy)**: ~$30-60/mo. Most of that is the residential proxy.
- **Without the proxy** (you accept YouTube intermittently fails): ~$10-15/mo.

---

## Install

### 1. Generate the API key

Every request needs `Authorization: Bearer <AMPERSTAND_API_KEY>`. There's no signup / dashboard — you generate it yourself.

```bash
openssl rand -hex 32
# example output:
# 9b0d3b...f8f9    ← save this; you'll paste it as AMPERSTAND_API_KEY
```

That's it — it's just a 64-char hex string used as a shared secret. **The bootstrap script does this for you** if you run it (see step 3). To rotate later: `amperstand-admin rotate-key --env-file /etc/amperstand/env`.

### 2. Local-only client (talk to a vault server somewhere else)

```bash
git clone https://github.com/zkid18/amperstand-core
cd amperstand-core
uv venv && uv pip install -e .

export AMPERSTAND_BASE_URL=https://your-vault-server      # or http://192.168.x.y:8765 for LAN
export AMPERSTAND_API_KEY=<the 64-char hex from step 1>
amperstand capture <some-url>
```

### 3. Full self-hosted server (vault + FTS + vector + chat)

```bash
# on your $6/mo droplet (Ubuntu 24.04 fresh)
git clone https://github.com/zkid18/amperstand-core /opt/amperstand/amperstand-core
cd /opt/amperstand/amperstand-core
sudo bash deploy/bootstrap.sh
# bootstrap will:
#   - create the `amperstand` system user
#   - generate AMPERSTAND_API_KEY via openssl, write to /etc/amperstand/env (mode 0640)
#   - install the venv, install the package editable
#   - install + enable amperstand-server.service
#   - print the generated key ONCE so you can copy it to your clients
```

For the rest of the production setup (Caddy reverse proxy, optional email watcher, optional bot), see [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

### 4. Connect external dependencies (if you want them)

Append to `/etc/amperstand/env` and `systemctl restart amperstand-server`:

```sh
OPENAI_API_KEY=sk-proj-...                                # for vector/chat/rerank/Whisper
AMPERSTAND_HTTP_PROXY=http://user:pass@webshare-host:80    # for YouTube + anti-bot
AMPERSTAND_YOUTUBE_AUDIO_FALLBACK=1                        # opt-in to Whisper audio for no-caption videos
TELEGRAM_BOT_TOKEN=...                                    # for the bot
BOT_ALLOWED_USER_IDS=12345678                             # comma-sep numeric Telegram IDs allowed to use the bot
```

> ⚠️ **Single-tenant warning**: `AMPERSTAND_API_KEY` is a master key. Anyone holding it can read, capture, and chat against your entire vault. Don't expose the server to the public internet without TLS, and don't share the key. Rotate after any leak (a Telegram bot token in a screenshot, a config posted on Stack Overflow, etc.).

---

## Companion repos

- [`amperstand`](https://github.com/zkid18/amperstand) — the CLI (thin client over the HTTP API)
- [`amperstand-tg-bot`](https://github.com/zkid18/amperstand-tg-bot) — Telegram bot
- [`amperstand-extension`](https://github.com/zkid18/amperstand-extension) — Chrome MV3 clipper
- `amperstand-notebook` — local chat-with-vault UI (WIP, not yet public)
- `prompts/` — drop-in prompts for plugging the vault into other LLM apps

---

## License

MIT. Fork it, change the proxy provider, swap embeddings, build a different UI on top — it's yours. PRs welcome but not required; I'd rather you build the thing *you* need on top of this than wait for me to add it.

---

## Roadmap / honest gaps

What's not there yet, in rough priority order:

- [ ] **MCP server** so Claude Desktop / Cursor can call the vault as a native tool
- [ ] **TLS + domain** instead of HTTP-on-raw-IP
- [ ] **Twitter/X capture** via Anysite or similar (currently best-effort via og:meta scraping)
- [ ] **Telegram channel ingestion** (one-off captures work; subscribing to a channel doesn't)
- [ ] **OAuth IMAP** so passwords don't sit in an env file
- [ ] **Podcast capture** (YouTube only today)
- [ ] **Image / PDF first-class capture** (PDFs work via `/capture` URL; uploads don't)
- [ ] **Local embeddings option** (BGE / Nomic via llama.cpp) for an OpenAI-free path
- [ ] **Server-side notebook sessions** (`amperstand-notebook` is localStorage-only today)
- [ ] **Tagging / collection UI** in the web view
- [ ] **Off-box backup** automation (you have markdown; you should have a cron job)
- [ ] **Watch lists / alerts** — "tell me when X shows up in the next captures"
- [ ] **Capture-time transformations** (auto-summarize, auto-translate, auto-tag)
