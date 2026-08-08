"""
Universal, multi-store stock watcher.

Each product in products.json is checked concurrently as its own asyncio
task - not one after another. Concurrency is capped in three places so a
single run behaves politely and predictably:

  - GLOBAL_SEM     overall concurrency across every product
  - domain semaphores  per-store cap, so e.g. Blinkit and Amul are checked
                       fully in parallel, but you never hit either site
                       with more than a couple of requests at once
  - BROWSER_SEM    headless browser instances are heavier than plain HTTP
                    requests, so they get their own (smaller) cap
  - LLM_SEM        caps concurrent Claude calls (cost + rate limits)

Extraction is tiered per product, same as before:
  1. schema.org JSON-LD (free, instant, when the site provides it)
  2. a cached CSS selector from a previous LLM check (cheap, fast)
  3. Claude reads the page and decides (universal - works on any site)

Each product declares how its page should be fetched:
  "render": "http"     - plain HTTP GET (fast, works when stock status is
                          in the initial HTML - e.g. many D2C/Shopify sites)
  "render": "browser"  - headless Chromium via Playwright (needed when
                          stock status is filled in by JavaScript after
                          load - common on app-driven sites like Blinkit)
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
import anthropic

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "state.json"

REVALIDATE_EVERY = 20  # force a fresh LLM check every N runs per product

IN_STOCK_HINTS = ["add to cart", "add to bag", "buy now", "in stock", "add to basket"]
OUT_OF_STOCK_HINTS = [
    "out of stock", "sold out", "notify me", "currently unavailable",
    "coming soon", "unavailable", "back in stock soon",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 StockWatcher/1.0"
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# --- concurrency knobs -------------------------------------------------
GLOBAL_SEM = asyncio.Semaphore(8)          # total concurrent product checks
BROWSER_SEM = asyncio.Semaphore(3)         # concurrent headless browser pages
LLM_SEM = asyncio.Semaphore(5)             # concurrent Claude calls
_domain_semaphores: dict[str, asyncio.Semaphore] = {}


def domain_sem(domain: str) -> asyncio.Semaphore:
    """Lazily create a per-domain semaphore so each store is rate-limited
    independently - Blinkit being slow never affects Amul's cap."""
    if domain not in _domain_semaphores:
        _domain_semaphores[domain] = asyncio.Semaphore(2)
    return _domain_semaphores[domain]


def get_domain(url: str) -> str:
    return urlparse(url).netloc


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


async def send_telegram(client: httpx.AsyncClient, message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, would have sent:", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await client.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        print("Telegram send failed:", e)


async def fetch_http(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


async def fetch_browser(browser, url: str) -> str:
    """Render the page with headless Chromium - needed for sites that fill
    in stock status via JavaScript after the initial page load."""
    async with BROWSER_SEM:
        page = await browser.new_page(user_agent=USER_AGENT)
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            return await page.content()
        finally:
            await page.close()


def check_json_ld(html: str):
    """Tier 1: schema.org Product/Offer availability, if the site includes it."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                availability = str(offers.get("availability", ""))
                if "InStock" in availability:
                    return True
                if "OutOfStock" in availability:
                    return False
    return None


def check_cached_selector(html: str, selector: str):
    """Tier 2: reuse a selector the LLM identified on a previous run."""
    if not selector:
        return None
    soup = BeautifulSoup(html, "html.parser")
    try:
        el = soup.select_one(selector)
    except Exception:
        return None
    if not el:
        return None
    text = el.get_text(" ", strip=True).lower()
    if any(hint in text for hint in OUT_OF_STOCK_HINTS):
        return False
    if any(hint in text for hint in IN_STOCK_HINTS):
        return True
    return None  # ambiguous - let the LLM tier handle it


def clean_text_for_llm(html: str, max_chars: int = 6000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)[:max_chars]


async def check_with_llm(llm_client: anthropic.AsyncAnthropic, html: str, product_name: str, description: str):
    """Tier 3: ask Claude to read the page. Universal - works on any site,
    regardless of store or layout."""
    page_text = clean_text_for_llm(html)
    prompt = f"""You are checking whether a specific product is currently in stock and purchasable on a retailer's web page.

Product name: {product_name}
Product description: {description}

Page content:
\"\"\"
{page_text}
\"\"\"

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"in_stock": true or false, "confidence": 0.0 to 1.0, "evidence": "short paraphrase of the text that led to your answer", "css_hint": "a short guess at a CSS selector for the stock-status element, or null"}}"""

    async with LLM_SEM:
        resp = await llm_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("Could not parse LLM response:", raw)
        return None


async def check_product(product_id, product, state, http_client, llm_client, browser, telegram_client):
    url = product["url"]
    name = product["name"]
    description = product.get("description", "")
    store = product.get("store", get_domain(url))
    render = product.get("render", "http")
    domain = get_domain(url)

    entry = state.setdefault(product_id, {
        "last_status": "unknown",
        "run_count": 0,
        "cached_selector": None,
    })
    entry["run_count"] += 1

    async with GLOBAL_SEM, domain_sem(domain):
        try:
            if render == "browser":
                if browser is None:
                    print(f"[{store}] [{name}] render=browser requested but no browser instance running")
                    return
                html = await fetch_browser(browser, url)
            else:
                html = await fetch_http(http_client, url)
        except Exception as e:
            print(f"[{store}] [{name}] fetch failed: {e}")
            return

    in_stock = check_json_ld(html)
    method = "json-ld"

    force_revalidate = entry["run_count"] % REVALIDATE_EVERY == 0
    if in_stock is None and not force_revalidate:
        in_stock = check_cached_selector(html, entry.get("cached_selector"))
        method = "cached-selector"

    if in_stock is None or force_revalidate:
        result = await check_with_llm(llm_client, html, name, description)
        if result:
            in_stock = result["in_stock"]
            method = "llm"
            if result.get("css_hint"):
                entry["cached_selector"] = result["css_hint"]
            print(
                f"[{store}] [{name}] LLM: in_stock={in_stock} "
                f"(confidence {result.get('confidence')}) - {result.get('evidence')}"
            )

    if in_stock is None:
        print(f"[{store}] [{name}] could not determine stock status this run")
        return

    new_status = "in_stock" if in_stock else "out_of_stock"
    print(f"[{store}] [{name}] status={new_status} via {method}")

    if new_status == "in_stock" and entry["last_status"] != "in_stock":
        await send_telegram(telegram_client, f"In stock at {store}: {name}\n{url}")

    entry["last_status"] = new_status
    entry["last_checked"] = datetime.now(timezone.utc).isoformat()


async def main():
    products = load_json(PRODUCTS_FILE, [])
    if not products:
        print("products.json is empty - add at least one product to track.")
        sys.exit(0)

    state = load_json(STATE_FILE, {})
    llm_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    needs_browser = any(p.get("render") == "browser" for p in products)
    playwright_ctx = None
    browser = None
    if needs_browser:
        # Imported here so the dependency is only required when actually used.
        from playwright.async_api import async_playwright
        playwright_ctx = await async_playwright().start()
        browser = await playwright_ctx.chromium.launch()

    try:
        async with httpx.AsyncClient() as http_client:
            tasks = [
                check_product(
                    p.get("id") or f"product_{i}",
                    p, state, http_client, llm_client, browser, http_client,
                )
                for i, p in enumerate(products)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if browser:
            await browser.close()
        if playwright_ctx:
            await playwright_ctx.stop()

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    asyncio.run(main())
