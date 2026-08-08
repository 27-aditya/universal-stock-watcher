# Universal stock watcher

Watches product pages across multiple stores at once and pings you on
Telegram the moment any of them come back in stock. Works without
site-specific code:

1. Checks for schema.org JSON-LD stock data first (free, instant, when the
   site provides it).
2. Falls back to a cached CSS selector from a previous check (cheap, fast).
3. Falls back to asking Claude to read the page and decide (works on any
   site, costs a small LLM call). Claude also suggests a selector, which
   gets cached for next time - so most sites only hit the LLM once or
   twice before settling into the cheap path.
4. Every 20th check per product, it re-validates with the LLM anyway, in
   case a site redesign changed what the cached selector means without
   breaking it outright.

**Every product is checked concurrently, not one after another.** Each
product runs as its own async task, so a slow Blinkit page never delays
your Amul check. Concurrency is capped in a few places so this stays
polite and predictable:
- up to 8 products checked at once overall
- up to 2 concurrent requests per store/domain, so no single site gets
  hammered even if you're tracking several items there
- up to 3 concurrent headless browser pages (these are heavier than plain
  HTTP requests)
- up to 5 concurrent Claude calls (cost and rate-limit control)

## 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. You'll get a bot token like `123456:ABC-DEF...`.
2. Start a chat with your new bot (search its username, hit Start).
3. Get your chat ID: message [@userinfobot](https://t.me/userinfobot) and it
   will reply with your numeric ID.

## 2. Get an Anthropic API key

Create one at [console.anthropic.com](https://console.anthropic.com) if you
don't already have one. This is a separate account from claude.ai - API
usage is billed per token (Haiku, which this uses, is inexpensive).

## 3. Set up the repo

1. Create a new **private** GitHub repo and push these files to it.
2. In the repo, go to **Settings → Secrets and variables → Actions** and
   add three repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY`
3. Edit `products.json` with the products you want to track, across as
   many stores as you like:

```json
[
  {
    "id": "amul_whey_1kg",
    "store": "amul",
    "name": "Amul Whey Protein 1kg Chocolate",
    "url": "https://seller-site.com/product/amul-whey-1kg-chocolate",
    "description": "1kg chocolate flavour whey protein by Amul",
    "render": "http"
  },
  {
    "id": "hotwheels_5pack_blinkit",
    "store": "blinkit",
    "name": "Hot Wheels 5-Car Gift Pack",
    "url": "https://blinkit.com/prn/hot-wheels-5-car-gift-pack/prid/123456",
    "description": "Hot Wheels 5-pack of die-cast toy cars, gift set",
    "render": "browser"
  }
]
```

   - `id`: any short unique string, used to track state per product.
   - `store`: a label used in logs and Telegram messages - doesn't need to
     match the domain, just something you recognise.
   - `name` / `description`: what to look for - keep this specific if the
     page lists multiple flavours or sizes, since the LLM step uses this
     to match the right variant.
   - `render`: `"http"` (default, fast) for sites where stock status is in
     the page's initial HTML, or `"browser"` for sites that fill it in
     with JavaScript after load - most app-driven grocery/delivery sites
     (Blinkit, Zepto, Instamart, etc.) need `"browser"`. If you're not
     sure which a site needs, start with `"http"` - if it consistently
     can't determine stock status, switch to `"browser"`.
4. Commit and push. The workflow runs automatically every 5 minutes, or
   trigger it immediately from the **Actions** tab → *Stock Watcher* →
   *Run workflow*.
# Universal stock watcher

Watches product pages across multiple stores at once and pings you on
Telegram the moment any of them come back in stock. Works without
site-specific code:

1. Checks for schema.org JSON-LD stock data first (free, instant, when the
   site provides it).
2. Falls back to a cached CSS selector from a previous check (cheap, fast).
3. Falls back to an LLM reading the page and deciding (works on any site).
   You choose the provider: Anthropic's API (cloud, no setup) or your own
   self-hosted Ollama server. Either way it also suggests a selector,
   which gets cached for next time - so most sites only hit the LLM once
   or twice before settling into the cheap path.
4. Every 20th check per product, it re-validates with the LLM anyway, in
   case a site redesign changed what the cached selector means without
   breaking it outright.

**Every product is checked concurrently, not one after another.** Each
product runs as its own async task, so a slow Blinkit page never delays
your Amul check. Concurrency is capped in a few places so this stays
polite and predictable:
- up to 8 products checked at once overall
- up to 2 concurrent requests per store/domain, so no single site gets
  hammered even if you're tracking several items there
- up to 3 concurrent headless browser pages (these are heavier than plain
  HTTP requests)
- up to 5 concurrent LLM calls (cost/rate-limit control on Anthropic,
  or just not overloading your own hardware on Ollama - see below)

## 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. You'll get a bot token like `123456:ABC-DEF...`.
2. Start a chat with your new bot (search its username, hit Start).
3. Get your chat ID: message [@userinfobot](https://t.me/userinfobot) and it
   will reply with your numeric ID.

## 2. Choose your LLM provider

**Option A - Anthropic's API (fastest to get running):**

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign
   up if you haven't already - this is a separate account from claude.ai.
2. Add billing under **Settings → Billing** (API usage is metered
   separately from any claude.ai subscription; Haiku, which this uses,
   costs fractions of a cent per check).
3. Go to **API Keys → Create Key**, copy it. That's the whole setup - no
   servers, no networking to think about.

**Option B - your own self-hosted Ollama server:**

Free to run, keeps everything on your own hardware, no API key needed.
The tradeoff is entirely about *reachability*: GitHub's own runners
(`ubuntu-latest`) execute in GitHub's cloud and cannot see `localhost` or
your home/office LAN - they can't reach an Ollama server sitting on your
machine no matter what URL you give them. You have two ways around this:

- **Self-hosted GitHub Actions runner (recommended)** - install GitHub's
  runner agent on the same machine (or network) as Ollama, so the
  workflow itself executes locally and can just reach
  `http://localhost:11434`. Repo → **Settings → Actions → Runners → New
  self-hosted runner**, follow the download/config commands it gives you,
  then in `watch.yml` change `runs-on: ubuntu-latest` to
  `runs-on: self-hosted`. Caveat: checks only run while that machine is
  on and the runner process is running.
- **Expose Ollama through a secure tunnel** if you'd rather keep using
  GitHub's hosted runners - e.g. a [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
  or a Cloudflare Tunnel with access policies, then set `OLLAMA_BASE_URL`
  to that tunnel's HTTPS address. Don't expose Ollama's raw port to the
  open internet without authentication in front of it - anyone who finds
  it can send it prompts and burn your compute.

Pick a model that follows JSON instructions reasonably well and is sized
for your hardware - `llama3.1:8b` or `qwen2.5:7b-instruct` are reasonable
starting points (`ollama pull llama3.1` on the Ollama machine). Smaller
models are less reliable at strict JSON output and stock-status judgment,
so if you see a lot of "could not parse Ollama response" in the logs,
try a larger model.

## 3. Set up the repo

1. Create a new **private** GitHub repo and push these files to it.
2. In the repo, go to **Settings → Secrets and variables → Actions**.
   Add these under the **Secrets** tab:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY` (only needed if using Option A - skip entirely
     if you're going Ollama-only)

   And these under the **Variables** tab (not secret, just config - all
   optional, shown with their defaults):
   - `LLM_PROVIDER` - `anthropic` (default) or `ollama`
   - `OLLAMA_BASE_URL` - default `http://localhost:11434`
   - `OLLAMA_MODEL` - default `llama3.1`
   - `LLM_CONCURRENCY` - default `5`; for a single local Ollama instance
     without a lot of spare GPU/CPU headroom, drop this to `1` or `2` -
     a self-hosted model usually handles requests better one at a time
     than a cloud API does.
3. Edit `products.json` with the products you want to track, across as
   many stores as you like:

```json
[
  {
    "id": "amul_whey_1kg",
    "store": "amul",
    "name": "Amul Whey Protein 1kg Chocolate",
    "url": "https://seller-site.com/product/amul-whey-1kg-chocolate",
    "description": "1kg chocolate flavour whey protein by Amul",
    "render": "http"
  },
  {
    "id": "hotwheels_5pack_blinkit",
    "store": "blinkit",
    "name": "Hot Wheels 5-Car Gift Pack",
    "url": "https://blinkit.com/prn/hot-wheels-5-car-gift-pack/prid/123456",
    "description": "Hot Wheels 5-pack of die-cast toy cars, gift set",
    "render": "browser"
  }
]
```

   - `id`: any short unique string, used to track state per product.
   - `store`: a label used in logs and Telegram messages - doesn't need to
     match the domain, just something you recognise.
   - `name` / `description`: what to look for - keep this specific if the
     page lists multiple flavours or sizes, since the LLM step uses this
     to match the right variant.
   - `render`: `"http"` (default, fast) for sites where stock status is in
     the page's initial HTML, or `"browser"` for sites that fill it in
     with JavaScript after load - most app-driven grocery/delivery sites
     (Blinkit, Zepto, Instamart, etc.) need `"browser"`. If you're not
     sure which a site needs, start with `"http"` - if it consistently
     can't determine stock status, switch to `"browser"`.
4. Commit and push. The workflow runs automatically every 5 minutes, or
   trigger it immediately from the **Actions** tab → *Stock Watcher* →
   *Run workflow*.

## Notes and limits

- **Repo must stay private** if the product URLs or your setup are
  sensitive - `state.json` is committed back to the repo after every run.
- **The Playwright browser install step adds real time to every run**
  (roughly 1-2 minutes to download and cache Chromium) and only runs when
  at least one product uses `"render": "browser"`. If none of your
  products need it, you can remove that step and the `playwright` line
  from `requirements.txt` to keep runs fast.
- **GitHub Actions' schedule isn't exact** - a `*/5` cron can slip by a
  few minutes under load. Fine for a prototype; if you need tighter timing
  for a specific fast-selling item, move this to cron on a small VPS
  later (same script, just run `python watcher.py` on a real timer).
- **Notification spam**: it only notifies on the transition from
  not-in-stock to in-stock, not on every check.
- **If you switch to a self-hosted runner for Ollama**, remember checks
  now depend on that machine being on and reachable, not on GitHub's
  uptime. If that's not something you want to guarantee, the tunnel
  option (or just using Anthropic) keeps the "always runs on schedule"
  property of hosted runners.
- **This is one process running everything concurrently, not truly
  separate machines.** That's intentional and enough for tens of products
  across a handful of stores. If you ever grow into tracking hundreds of
  products across many stores, the next architectural step is a real
  message queue (e.g. Redis-backed) feeding independent worker processes,
  with state moved from `state.json` to a shared external database
  instead of a file committed to git.
## Notes and limits

- **Repo must stay private** if the product URLs or your setup are
  sensitive - `state.json` is committed back to the repo after every run.
- **The Playwright browser install step adds real time to every run**
  (roughly 1-2 minutes to download and cache Chromium) and only runs when
  at least one product uses `"render": "browser"`. If none of your
  products need it, you can remove that step and the `playwright` line
  from `requirements.txt` to keep runs fast.
- **GitHub Actions' schedule isn't exact** - a `*/5` cron can slip by a
  few minutes under load. Fine for a prototype; if you need tighter timing
  for a specific fast-selling item, move this to cron on a small VPS
  later (same script, just run `python watcher.py` on a real timer).
- **Notification spam**: it only notifies on the transition from
  not-in-stock to in-stock, not on every check.
- **This is one process running everything concurrently, not truly
  separate machines.** That's intentional and enough for tens of products
  across a handful of stores. If you ever grow into tracking hundreds of
  products across many stores, the next architectural step is a real
  message queue (e.g. Redis-backed) feeding independent worker processes,
  with state moved from `state.json` to a shared external database
  instead of a file committed to git.
