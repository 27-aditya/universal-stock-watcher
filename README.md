# Universal Stock Watcher

This document describes the Universal Stock Watcher.

The Universal Stock Watcher checks product pages on many stores. It sends
a Telegram message when a product comes back in stock. It does not need
code that is specific to a store.

## How the Watcher Works

The watcher checks each product with three methods. It uses the methods
in this order:

1. It reads the schema.org JSON-LD stock data in the page.
2. It uses a CSS selector that is stored from a previous check.
3. It asks an LLM to read the page and to decide the stock status.

The watcher uses the first method that gives an answer.

The LLM also suggests a CSS selector. The watcher stores this selector.
The next checks use this selector. Most stores use the LLM only once or
twice. Then they use the stored selector.

The watcher revalidates every product with the LLM every Nth check. The
default is 20 checks. The variable `REVALIDATE_EVERY` controls this
value. Set this variable to 0 to disable revalidation. Revalidation
detects changes in a store design.

### Concurrency

The watcher checks all products at the same time. Each product runs in
its own task. A slow page does not delay the other pages.

The watcher limits concurrency in four places. These limits have default
values. You can change them with environment variables.

| Variable              | Default | Purpose                                    |
| --------------------- | ------- | ------------------------------------------ |
| `GLOBAL_CONCURRENCY`  | 8       | Maximum number of products checked at once |
| `DOMAIN_CONCURRENCY`  | 2       | Maximum requests per store at the same time |
| `BROWSER_CONCURRENCY` | 3       | Maximum browser pages at the same time     |
| `LLM_CONCURRENCY`     | 5       | Maximum LLM calls at the same time         |

The domain limit prevents you from overloading one store. The LLM limit
controls the cost of the Anthropic API. It also prevents you from
overloading your own Ollama server.

## 1. Create a Telegram Bot

Do this procedure to create a Telegram bot:

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send the command `/newbot`.
3. Follow the instructions from BotFather.
4. Copy the bot token. The token looks like `123456:ABC-DEF...`.
5. Open a chat with your new bot. Press the Start button.
6. Open a chat with [@userinfobot](https://t.me/userinfobot).
7. Write down your numeric chat ID.

## 2. Select an LLM Provider

You can use one of these LLM providers:

- The Anthropic API.
- A self-hosted Ollama server.

### Option A - Use the Anthropic API

Do this procedure to use the Anthropic API:

1. Go to [console.anthropic.com](https://console.anthropic.com).
2. Create an account if you do not have one. This is a separate account
   from claude.ai.
3. Select **Settings - Billing**.
4. Add your billing information.
5. Select **API Keys**.
6. Select **Create Key**.
7. Copy the API key.

The default model is Haiku. One check costs a fraction of a cent.

### Option B - Use a Self-hosted Ollama Server

An Ollama server is free. It runs on your own hardware. It does not need
an API key.

GitHub hosted runners run in the GitHub cloud. They cannot reach
`localhost` or your home network. They cannot reach an Ollama server on
your machine. You have two options:

**Option 1 - Use a self-hosted GitHub runner**

Use this option when you want the Ollama server to stay local:

1. Install the GitHub runner agent on the machine that runs Ollama.
   Go to **Settings - Actions - Runners - New self-hosted runner**.
2. Follow the commands that GitHub gives you.
3. In `.github/workflows/watch.yml`, change `runs-on: ubuntu-latest`.
   Use `runs-on: self-hosted`.
4. Push the change to GitHub.

A self-hosted runner checks the products only when that machine is on.
The runner process must run.

**Option 2 - Expose Ollama through a secure tunnel**

Use this option when you want to keep GitHub hosted runners:

1. Create a tunnel to the Ollama server. Use a Tailscale Funnel or a
   Cloudflare Tunnel.
2. Add access policies to the tunnel.
3. Set the variable `OLLAMA_BASE_URL` to the HTTPS address of the
   tunnel.

Caution: Do not expose the raw Ollama port to the open internet. Anyone
who finds it can send prompts to it and use your compute.

Use a model that can follow JSON instructions. Use a model that fits
your hardware. These models are good starting points:

- `llama3.1:8b`
- `qwen2.5:7b-instruct`

Run this command on the Ollama machine:

```bash
ollama pull llama3.1
```

Smaller models give less reliable results. If you see many
"could not parse ollama response" messages in the log, use a larger
model.

## 3. Set Up the Repository

Do this procedure to set up the repository:

1. Create a new private repository on GitHub.
2. Push the project files to the repository.
3. Go to **Settings - Secrets and variables - Actions**.
4. Add these values in the **Secrets** tab:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY`. Add this value only when you use the Anthropic
     API. Skip it when you use Ollama only.
5. Add these values in the **Variables** tab. All values are optional.

| Variable                     | Default                                 | Purpose                                                   |
| ---------------------------- | --------------------------------------- | --------------------------------------------------------- |
| `LLM_PROVIDER`               | `anthropic`                             | The LLM provider: `anthropic` or `ollama`                 |
| `ANTHROPIC_MODEL`            | `claude-haiku-4-5-20251001`             | The Anthropic model                                       |
| `OLLAMA_BASE_URL`            | `http://localhost:11434`                | The address of the Ollama server                          |
| `OLLAMA_MODEL`               | `llama3.1`                              | The Ollama model                                          |
| `GLOBAL_CONCURRENCY`         | `8`                                     | Maximum products checked at once                          |
| `DOMAIN_CONCURRENCY`         | `2`                                     | Maximum requests per store                                |
| `BROWSER_CONCURRENCY`        | `3`                                     | Maximum browser pages                                     |
| `LLM_CONCURRENCY`            | `5`                                     | Maximum LLM calls. Set to `1` or `2` for one Ollama server |
| `LLM_MIN_CONFIDENCE`         | `0.5`                                   | Minimum confidence for an LLM verdict                     |
| `NOTIFY_COOLDOWN_SECONDS`    | `1800`                                  | Minimum time between notifications for one product        |
| `ALERT_AFTER_FAILURES`       | `3`                                     | Number of failed checks before a failure alert            |
| `FETCH_RETRIES`              | `3`                                     | Number of retries for transient HTTP failures             |
| `REVALIDATE_EVERY`           | `20`                                    | Revalidate with the LLM every Nth check. `0` disables it  |

Note: When `ANTHROPIC_API_KEY` is not set, the watcher still runs.
Products that need the LLM tier report `unknown`. Products that use
JSON-LD or a stored selector keep working.

### Edit the Products File

Edit `products.json` with the products that you want to track. You can
track products from many stores in one file.

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

These are the fields for each product:

| Field         | Purpose                                                             |
| ------------- | ------------------------------------------------------------------- |
| `id`          | A short unique string. The watcher uses it to track the state.      |
| `store`       | A label for logs and Telegram messages. It does not need to be a domain. |
| `name`        | The product name. Make it specific when the page has many variants. |
| `description` | A description of the product. The LLM uses it to match the variant. |
| `render`      | `http` or `browser`                                                 |
| `pincode`     | Optional. A delivery pincode for stores that hide every product until a serviceable pincode is chosen (e.g. shop.amul.com, blinkit.com). Only affects `render: "browser"`: the watcher selects the pincode on the store's home page first, then opens the product page. |

Rules for the `render` field:

- Use `"http"` when the stock status is in the initial HTML of the page.
  This is the default and the fastest method.
- Use `"browser"` for sites that fill the stock status with JavaScript.
  Most grocery and delivery sites need this method.
- When you are not sure, start with `"http"`. If the watcher cannot
  determine the stock status, change to `"browser"`.

### Start the Watcher

Do this procedure to start the watcher on GitHub:

1. Commit your changes.
2. Push the changes to the repository.
3. Open the **Actions** tab.
4. Select **Stock Watcher**.
5. Select **Run workflow**.

The workflow also runs automatically every 5 minutes.

## Run the Watcher Locally

The script runs on any computer that has Python. It does not need GitHub.

```bash
pip install -r requirements.txt
```

Run this command to check all products:

```bash
python watcher.py
```

When Telegram is not configured, the watcher prints the notifications to
the log.

Run this command to check specific products:

```bash
python watcher.py --product amul_whey_1kg --product hotwheels_5pack_blinkit
```

Run this command to inspect the products without sending or saving:

```bash
python watcher.py --dry-run --json
```

Run this command to see all requests and rejections:

```bash
python watcher.py --log-level DEBUG
```

### Exit Codes

The watcher returns these exit codes:

| Code | Meaning                                                                 |
| ---- | ----------------------------------------------------------------------- |
| `0`  | The run finished. It can still have per-product errors. The next run retries. |
| `2`  | Bad configuration or bad command line usage.                            |

### The JSON Output

The option `--json` prints a machine-readable summary to stdout. The
logs go to stderr.

```json
[
  {
    "id": "amul_whey_1kg",
    "store": "amul",
    "name": "Amul Whey Protein 1kg Chocolate",
    "url": "https://seller-site.com/product/amul-whey-1kg-chocolate",
    "status": "in_stock",
    "method": "json-ld",
    "confidence": null,
    "notified": false,
    "error": null
  }
]
```

The `status` field has one of these values:

- `in_stock`
- `out_of_stock`
- `unknown`
- `error`

## How Notifications Work

### Restock Notifications

The watcher sends a message only on a transition from not-in-stock to
in-stock. It does not send a message on every check.

Within `NOTIFY_COOLDOWN_SECONDS`, the watcher does not send another
message for the same product. This prevents message spam when a product
flaps.

### Failure Alerts

The watcher sends a failure alert after `ALERT_AFTER_FAILURES`
consecutive failed checks. The default is 3. A failed check is a page
that does not load, an HTTP 5xx error, or an LLM that is down.

The alert has the product name and the last error. The watcher stops
sending the alert when a check succeeds.

## Notes and Limits

Note: Keep the repository private. The `state.json` file is committed
back to the repository after every run.

Note: The Playwright step adds 1 to 2 minutes to every run. The workflow
installs this step only when at least one product uses `"render":
"browser"`. When none of your products need it, the step is skipped.

Note: The GitHub schedule is not exact. A `*/5` cron can slip by a few
minutes. This is acceptable for a prototype. For a fast-selling item,
move the script to cron on a small VPS.

Note: Restock alerts are transition-only and cooldown-limited. Failure
alerts fire once per failure streak.

Note: The workflow declares `concurrency`. A late run queues behind the
previous run. Two runs cannot write to `state.json` at the same time.

Note: When you use a self-hosted runner for Ollama, the checks depend on
that machine. The machine must be on and reachable. A tunnel or the
Anthropic API keeps the "always runs on schedule" property.

Note: The watcher is one process that runs everything concurrently. This
is enough for tens of products across a few stores. For hundreds of
products, use a message queue and worker processes. Move the state to an
external database.

## Development

Install the development dependencies:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Run these commands:

| Command                       | Purpose                                                  |
| ----------------------------- | -------------------------------------------------------- |
| `python -m pytest -q`         | Run the unit and integration tests. These tests use no network. The run also enforces the 90% coverage floor (`--cov-fail-under=90`). |
| `python e2e_local.py`         | Run a true end-to-end test with a real HTTP server and the real CLI. |
| `python -m ruff check .`      | Check the code style.                                    |

The tests in `tests/` use mocked HTTP. The script `e2e_local.py` starts
a local HTTP server and runs the real `watcher.py`. Both run offline and
give the same result every time.

GitHub Actions enforces all three checks. `.github/workflows/ci.yml` runs
pytest (with the coverage gate), ruff, and the e2e suite on every push and
pull request on Python 3.11, 3.12, and 3.13. `.github/workflows/watch.yml`
is the scheduled stock check itself.