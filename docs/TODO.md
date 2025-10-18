# TODO

## Persona Hot‑Swap

Goal: Switch persona at runtime (without restart) and align bot outward identity (display name, avatar) to the selected persona.

Design notes:

- MessageRouter/PromptTemplateEngine should hot-reload pack changes (already hot-reloads system/persona/lore paths).
- Add runtime persona select API and optional Discord command.
- Update outward identity:
  - Avatar: implemented via your `logs/change avatar.md` notes.
  - Display name: use Discord API to update application/bot nickname per guild.

Tasks:

- [ ] Add `POST /persona/select { pack_path }` in HTTP mode; optional `/persona/reload`.
- [ ] Discord command `/persona select` gated to admins.
- [ ] Identity service: helper to change avatar and display name (per guild nickname if needed).
- [ ] Logging: `[persona-switch]` and `[identity-update]` lines with correlation.

Acceptance Criteria:

- Switching persona updates prompts immediately for new messages (including NSFW override when in NSFW channels).
- Optional identity update changes bot nickname/avatar within a guild and via HTTP UI.

## URL Metadata Enrichment (Link‑Only Messages)

Goal: When a message contains only (or mostly) URLs, enrich context by attaching lightweight page/video metadata so the bot can respond meaningfully without a user question.

Note: Maybe incorporate with 2.7 Web Retrieval.

Approach (fast, safe, no secrets):

- Detect URLs in the incoming message (Discord + Web). If link‑only, fetch compact metadata per URL and inject one small system block before prompt assembly.

- Extraction order (short‑circuit on success):

  1) oEmbed (zero‑auth) when available (e.g., YouTube oEmbed endpoint).

  2) OpenGraph + Twitter Card meta tags (download first ~64–128KB only).

  3) \<title> tag fallback.

  4) JSON‑LD (schema.org) for fields like name/author/datePublished.

- Inject a single system message labeled `[Link metadata]` with: Title, Site, Author/Channel, Date, URL (truncate long fields; no raw HTML).

Safety/perf:

- Allowlist/denylist domains (configurable). Only http(s).
- Per‑URL timeout ~1–2s and global budget per message; abort if slow.
- Max bytes read per URL (e.g., 131072). Text/HTML or JSON only; skip binaries.
- In‑memory LRU cache with TTL (15–60 min). No disk persistence.
- Logging: `urlmeta-start|finish|cache-hit|blocked|timeout` (no content logged).

Config (proposed):

```yaml
url_metadata:
  enabled: true
  max_per_message: 2
  timeout_seconds: 1.5
  total_budget_seconds: 3.0
  max_bytes: 131072
  allow_domains: ["youtube.com", "youtu.be"]
  deny_domains: []
  prefer_oembed: true
  include_thumbnail: false
  cache_ttl_seconds: 1800
  user_agent: "DiscordLLMBot/1.0 (+https://example.com)"
```

Tasks:

- [ ] Add `UrlMetadataService` with fetch/parse/caching.
- [ ] URL detection helper and normalization (strip utm params).
- [ ] Router: if link‑only and enabled, call service and inject `[Link metadata]` system block before template context.
- [ ] ConfigService getters + safe defaults.
- [ ] Unit tests (YouTube oEmbed, generic OG page, timeouts, cache‑hit path).

Acceptance Criteria:

- For a YouTube link‑only message, the reply includes video title and channel context without asking the user for a question.
- Metadata block stays within a tight token budget and is omitted on timeout/over‑budget.
- Works in both Discord and Web paths; no disk persistence of fetched content.

## Web Retrieval (Scrape/Browse)

Goal: Let the bot bring in fresh web content when a user posts a URL or explicitly requests it, while keeping safety, privacy, and token budget under control. Two paths:

- Provider-native browsing: use an LLM/model that can “browse.”
- Manual fetch/parse: fetch the page ourselves, summarize, and inject as context.

Recommendation

- Start with manual fetch/parse for consistency, caching, and tighter control. Add provider-native browsing as an optional mode later.

Design notes

- Triggering
  - Auto: detect URLs in the user message and, if enabled, fetch.
  - Command/API: Discord slash command (e.g., `/web fetch <url>`) and HTTP mode param `fetch=true`.
- Injection point
  - Insert a single system message after lore and before the template context: “You may use the following web context…\n[Web] …”.
  - Include title, canonical URL, and a short, model-generated summary with a few bullet highlights.
  - Cap with `web.max_fraction` of the prompt budget (similar to lore).
- Retention
  - Keep a compact “web memory” for `web.retain_turns` additional turns. Tag entries with a label (e.g., `[Web]`) and source URL. Evict oldest or exceed-ttl entries.
- Safety & hygiene
  - Respect robots.txt (configurable), set a custom User-Agent, rate-limit per domain.
  - Strip scripts/trackers, ignore binary blobs; include image URLs only as links.
  - Domain allowlist/denylist and max document size safeguard.
- Native browsing mode
  - If selected in config, route requests to a browsing-capable model; still enforce allowlist/denylist and a hard token budget for returned content.

Config (proposed)

```yaml
web:
  enabled: false
  mode: scrape   # scrape | native
  retain_turns: 2
  ttl_minutes: 120
  max_fraction: 0.25   # of prompt_budget
  auto_on_url: true
  allowlist: []        # optional domain allowlist
  denylist: []         # optional domain denylist
  user_agent: "Discord LLM Bot/1.0 (+https://example.com)"
  rate_limit_per_domain_rpm: 10
```

Tasks

- [ ] ConfigService: add `web.*` getters with safe defaults.
- [ ] WebFetchService: httpx-based fetcher with timeout, robots.txt check, simple cache (in-memory; optional file cache), and parser (trafilatura or readability + BeautifulSoup fallback) to extract title + main text.
- [ ] Summarizer: small helper to compress extracted text to `N` tokens for the `[Web]` block (reuse LLMClient with a tight max_tokens and deterministic settings).
- [ ] URL detection: reuse/extend existing vision URL extraction or add a lightweight URL regex extractor.
- [ ] Prompt assembly: insert one `[Web]` system block post-lore, pre-template; apply budgeting similar to lore using `web.max_fraction`.
- [ ] Retention: store compact summaries in ConversationMemory with metadata (url, title, fetched_at, turns_remaining); decrement on each reply, evict when 0.
- [ ] Logging: `[web-fetch-start]`, `[web-fetch-finish]`, `[web-cache-hit]`, `[web-include]` with `channel`, `url`, `bytes`, and `correlation`.
- [ ] Discord command `/web fetch <url>` (admins or all, config-gated) and HTTP mode `POST /web/fetch`.
- [ ] Tests: unit tests for URL detection, parser, retention policy; e2e smoke test including budgeting.

Acceptance Criteria

- When a URL is present and `web.enabled=true`, the bot fetches or hits cache and includes a single `[Web]` system block with title, source, and a concise summary.
- The web block respects `web.max_fraction` and is omitted when over budget.
- Entries persist for `retain_turns` responses (unless evicted by TTL) and are clearly labeled.
- When `mode=native`, the browsing-capable path is used with equivalent logging and budgeting.

## Refactor Log messaging into a cleaner format with less junk

Goal: Improve log clarity by reducing noise and standardizing formats

- [ ] Identify and remove redundant or low-value log messages.
- [ ] Standardize log message formats for easier parsing and analysis.

## Stats! - Start capturing some stats on usage

Goal: Collect and log basic usage statistics to help understand how the bot is being used and identify areas for improvement

- [ ] Track number of messages LLM has responded to per day/week.
- [ ] Monitor active users and channels. (only responses to users, not all messages)
- [ ] cog & /command usage to see what is being used.
- [ ] Log stats in a lightweight db (e.g., SQLite)
- [ ] expose stats via a /stats endpoint (admin only), create a simple HTML page with basic stat views.

## Weather Interesting Fact improvement (prevent re-using same fact)

- [ ] Store random fact about the searched location and insert into prompt for subsequent weather requests for that location to negative steer LLM into providing a different fact.
  - [ ] will need to store last say 5 facts for each location, drop oldest when adding a new one beyond 5.
