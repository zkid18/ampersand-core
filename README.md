# Ampersand

> **Build a content hub of sources you actually trust — so your agents stop digging through SEO landfill.**

When your agent researches something, it gets the open web's first page: SEO listicles, AI summaries of AI summaries, content farms outranking the people who actually know things. You already know the writers worth reading. Ampersand makes *those* the corpus your agents query — instead of whatever Google ranks today.

Capture once, query forever. Plain markdown on disk you own. Plug into any agent.

## Use it anywhere

<!-- TODO: replace each bullet with a 2-line gif / screenshot once the assets exist -->

- 🖥️ **CLI** — `ampersand capture <url>`. One command, markdown in your vault.
- 🤖 **Telegram bot** — DM a link or text, lands as a doc within seconds.
- 🧷 **Browser extension** — Chrome MV3 clipper, one-click on any page.
- 💬 **Notebook UI** — chat with a working set of vault docs, citations clickable back to the source.
- ✉️ **Email watcher** — IMAP IDLE captures newsletters as they arrive (multi-account, parallel).
- 🦾 **Your agent** — `POST /vault/search/hybrid` from Cursor, Claude Desktop, n8n, custom code.

---

## Capture

Type one command, get a markdown file in your vault. The right extractor is picked automatically by URL.

```bash
ampersand capture https://stratechery.com/2025/cursors-pricing-pivot/
ampersand capture https://www.youtube.com/watch?v=e_NDFEWGW1w   # YouTube → transcript (audio-fallback if no captions)
ampersand capture https://www.linkedin.com/in/some-profile/    # LinkedIn extractor
ampersand capture https://twitter.com/user/status/123          # Twitter/X (via proxy or fxtwitter)
```

Other capture surfaces (same backend, same vault):

- **Telegram bot** — message a URL or plain text to your bot, it lands as a doc
- **Browser extension** (Chrome MV3, in `ampersand-extension/`) — one-click clip the page you're on
- **Web UI** (`/ui/`) — paste, list, search from any browser
- **POST /capture** — for your own scripts

---

## Subscribe

The "RSS for agents" pitch. Add a feed, then `sync` pulls every new item into the vault on a timer.

```bash
ampersand feed add https://stratechery.com/feed --name stratechery
ampersand feed add https://www.techcrunch.com/feed/
ampersand feed list
ampersand feed sync     # pulls new items into the vault
```

Roadmap (the syntax above gestures at what isn't built yet):

- ⏳ `ampersand follow @some-tg-channel` — Telegram channel ingestion. Stub today (`ampersand capture <tg-link>` works one at a time).
- ⏳ `ampersand follow github:trending/python` — GitHub trending watch.
- ⏳ `ampersand follow podcast:<rss>` — podcasts (today: YouTube only).

---

## Email / newsletters

The original capture path. Watches an IMAP mailbox and writes every newsletter into the vault.

```bash
ampersand email setup       # interactive, asks for IMAP creds
ampersand email list        # show configured accounts
ampersand email watch       # IMAP IDLE, fires when new mail arrives
ampersand email sync        # one-shot pull of unread
```

**Multi-account** — call `setup` once per inbox (Yahoo + Gmail + ProtonMail Bridge + your work IMAP), the watcher runs N inboxes in parallel.

**Auth honesty**: there's no OAuth flow yet. You pass an IMAP password (app password for Gmail/Yahoo) and it sits in `/etc/ampersand/env` (mode `0640`) on the server. That's fine for a single-tenant personal box; it's not fine for a hosted multi-user product. If you care about secrets management, the right next step is plugging in a password manager (1Password CLI, age-encrypted file, Vault) — open issue.

---

## Search

Three modes from the same engine, layered for both **recall** and **precision**.

```bash
ampersand search "brazilian funk miami bass"
# or directly:
curl -X POST $AMPERSAND_BASE_URL/vault/search/hybrid \
  -H "Authorization: Bearer $AMPERSAND_API_KEY" \
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

The companion project `ampersand-notebook/` is a three-pane local UI:
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
       capture ───────►│   ampersand-core (FastAPI)    │
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

**Where the data lives**: your vault is just markdown files on disk. By default on the server: `/var/lib/ampersand/vault/` (one file per doc, hash-addressed under `.store/by-id/`, organised by year/month for browsing). You can `tar` it, `git` it, `rsync` it. Nothing is locked into a database — the SQLite files are *derived* indexes that can be rebuilt from the markdown in minutes.

**API auth**: a single shared bearer token, `AMPERSAND_API_KEY`, lives in `/etc/ampersand/env` (mode `0640`, owned by the `ampersand` user). Every `/vault/*`, `/capture`, `/chat` request must include `Authorization: Bearer <key>`. The bot, the CLI, the extension, the notebook all carry the same key. **There's no per-user auth.** Rotate via `ampersand-admin rotate-key`.

**HTTP only** today (raw IP, no TLS) — fine for a personal box you only hit from your own network, **not fine** for a public deployment. TLS is a one-line Caddyfile swap (`Caddyfile.tls` is in `deploy/`) once you point a domain at the box.

**Anti-bot / proxying**: news sites and Twitter/X block datacenter IPs aggressively. Ampersand routes those fetches through a residential proxy (we use [Webshare](https://www.webshare.io) — `AMPERSAND_HTTP_PROXY=http://user:pass@host:port` in the env). If you have opinions about a different provider (Bright Data, IPRoyal, your own self-hosted Squid), it's literally a single env var to swap. The proxy is also where YouTube transcript fetching is routed because Google rate-limits cloud IPs hard.

**Audio transcription**: for YouTube videos without captions, we fall back to `yt-dlp` → OpenAI Whisper. Off by default (`AMPERSAND_YOUTUBE_AUDIO_FALLBACK=1` to enable). ~$0.006/min of audio.

**Embeddings**: OpenAI `text-embedding-3-small` at 512-dim (Matryoshka) for vector search. ~$0.40 to embed 6,800 docs once; pennies/day after. Local-model alternative (BGE/Nomic via llama.cpp) is on the wish list but not built.

---

## Plug it into your agents

Three integration patterns, in order of friction:

1. **Drop the prompt** — `prompts/vault-agent.md` is a ready-to-paste system prompt that teaches any LLM how to query the vault HTTP API. Works in Custom GPTs, Claude Projects, Cursor `.cursorrules`, n8n LLM nodes.

2. **Call `/chat`** — POST your conversation history + a working set of doc IDs, get back a streamed RAG answer with citations. ~150 lines of glue from any language.

3. **Use the search endpoints directly** — `POST /vault/search/hybrid` returns ranked passages with snippets. Your agent does its own reasoning over them.

A future MCP server (so Claude Desktop / Cursor can call the vault as a native tool) is on the roadmap.

---

## Install

```bash
# Local: capture from your terminal, send to a remote vault server
git clone https://github.com/zkid18/ampersand-core
cd ampersand-core
uv venv && uv pip install -e .
export AMPERSAND_BASE_URL=https://your-vault-server
export AMPERSAND_API_KEY=...
ampersand capture <some-url>
```

For a full self-hosted server (vault + FTS + vector + chat) on a $6/mo DigitalOcean droplet, see [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

---

## Companion repos

- [`ampersand`](https://github.com/zkid18/ampersand) — the CLI (thin client over the HTTP API)
- `ampersand-bot` — Telegram bot
- `ampersand-extension` — Chrome MV3 clipper
- `ampersand-notebook` — local chat-with-vault UI
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
- [ ] **Server-side notebook sessions** (`ampersand-notebook` is localStorage-only today)
- [ ] **Tagging / collection UI** in the web view
- [ ] **Off-box backup** automation (you have markdown; you should have a cron job)
- [ ] **Watch lists / alerts** — "tell me when X shows up in the next captures"
- [ ] **Capture-time transformations** (auto-summarize, auto-translate, auto-tag)
