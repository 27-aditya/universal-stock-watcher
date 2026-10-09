"""
Universal, multi-store stock watcher.

Each product in products.json is checked concurrently as its own asyncio
task - not one after another. Concurrency is capped in a few places so a
single run behaves politely and predictably (all knobs are env-configurable):

  - Limits.global   overall concurrency across every product
  - Limits.domain   per-store cap, so e.g. Blinkit and Amul are checked
                    fully in parallel, but no single site gets hammered
  - Limits.browser  headless browser pages are heavier than plain HTTP
                    requests, so they get their own (smaller) cap
  - Limits.llm      caps concurrent LLM calls (cost / rate limits / hardware)

Extraction is tiered per product:
  1. schema.org JSON-LD (free, instant, when the site provides it)
  2. a cached CSS selector from a previous LLM check (cheap, fast)
  3. an LLM reads the page and decides (universal - works on any site)

The LLM tier is provider-agnostic: Anthropic's API (default) or a
self-hosted Ollama server, selected with LLM_PROVIDER. Either way the LLM
also suggests a CSS selector, which gets cached for next time - so most
sites only hit the LLM once or twice before settling into the cheap path.
Every Nth check per product (REVALIDATE_EVERY, default 20) it re-validates
with the LLM anyway, in case a site redesign changed what the cached
selector means without breaking it outright.

Each product declares how its page should be fetched:
  "render": "http"     - plain HTTP GET (fast, works when stock status is
                          in the initial HTML - e.g. many D2C/Shopify sites)
  "render": "browser"  - headless Chromium via Playwright (needed when
                          stock status is filled in by JavaScript after
                          load - common on app-driven sites like Blinkit)

CLI:
  python watcher.py                  run one check over products.json
  python watcher.py --product ID     check only the given product(s)
  python watcher.py --dry-run        fetch + decide, but notify nobody and
                                     never write state
  python watcher.py --json           print results as JSON on stdout
  python watcher.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("stockwatcher")

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "state.json"

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

IN_STOCK_HINTS = ["add to cart", "add to bag", "buy now", "in stock", "add to basket"]
OUT_OF_STOCK_HINTS = [
    "out of stock", "sold out", "notify me", "currently unavailable",
    "coming soon", "unavailable", "back in stock soon",
]

JSON_LD_IN_STOCK = ("InStock", "LimitedAvailability", "OnlineOnly")
JSON_LD_OUT_OF_STOCK = (
    "OutOfStock", "SoldOut", "Discontinued", "PreOrder", "BackOrder", "PreSale",
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 StockWatcher/1.0"
)


class ConfigError(Exception):
    """Bad configuration (env vars / products.json) - the run cannot start."""


class FetchError(Exception):
    """A page could not be fetched or rendered."""


class LLMError(Exception):
    """The LLM call itself failed (transport / API error)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    llm_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    global_concurrency: int = 8
    domain_concurrency: int = 2
    browser_concurrency: int = 3
    llm_concurrency: int = 5

    llm_min_confidence: float = 0.5
    notify_cooldown_seconds: int = 1800
    alert_after_failures: int = 3
    fetch_retries: int = 3
    revalidate_every: int = 20
    fetch_timeout: float = 20.0
    browser_timeout_ms: int = 30000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def raw(key: str, default: str = "") -> str:
            value = env.get(key)
            return default if value is None else str(value).strip()

        def integer(key: str, default: int, minimum: int = 0) -> int:
            text = raw(key)
            if not text:
                return default
            try:
                value = int(text)
            except ValueError:
                raise ConfigError(f"{key}={text!r} is not an integer") from None
            if value < minimum:
                raise ConfigError(f"{key}={value} must be >= {minimum}")
            return value

        def number(key: str, default: float, minimum: float = 0.0) -> float:
            text = raw(key)
            if not text:
                return default
            try:
                value = float(text)
            except ValueError:
                raise ConfigError(f"{key}={text!r} is not a number") from None
            if value < minimum:
                raise ConfigError(f"{key}={value} must be >= {minimum}")
            return value

        provider = raw("LLM_PROVIDER", "anthropic").lower() or "anthropic"
        if provider not in ("anthropic", "ollama"):
            raise ConfigError(
                f"LLM_PROVIDER={provider!r} is not supported "
                "(use 'anthropic' or 'ollama')"
            )

        min_confidence = number("LLM_MIN_CONFIDENCE", 0.5)
        if min_confidence > 1.0:
            raise ConfigError(f"LLM_MIN_CONFIDENCE={min_confidence} must be <= 1.0")

        return cls(
            telegram_bot_token=raw("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=raw("TELEGRAM_CHAT_ID") or None,
            llm_provider=provider,
            anthropic_api_key=raw("ANTHROPIC_API_KEY") or None,
            anthropic_model=raw("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL) or DEFAULT_ANTHROPIC_MODEL,
            ollama_base_url=raw("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434",
            ollama_model=raw("OLLAMA_MODEL", "llama3.1") or "llama3.1",
            global_concurrency=integer("GLOBAL_CONCURRENCY", 8, minimum=1),
            domain_concurrency=integer("DOMAIN_CONCURRENCY", 2, minimum=1),
            browser_concurrency=integer("BROWSER_CONCURRENCY", 3, minimum=1),
            llm_concurrency=integer("LLM_CONCURRENCY", 5, minimum=1),
            llm_min_confidence=min_confidence,
            notify_cooldown_seconds=integer("NOTIFY_COOLDOWN_SECONDS", 1800),
            alert_after_failures=integer("ALERT_AFTER_FAILURES", 3, minimum=1),
            fetch_retries=integer("FETCH_RETRIES", 3, minimum=1),
            revalidate_every=integer("REVALIDATE_EVERY", 20),
            fetch_timeout=number("FETCH_TIMEOUT", 20.0, minimum=1.0),
            browser_timeout_ms=integer("BROWSER_TIMEOUT_MS", 30000, minimum=1000),
        )


# --------------------------------------------------------------------------
# concurrency limits
# --------------------------------------------------------------------------

@dataclass
class Limits:
    global_: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))
    browser: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    llm: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(5))
    domain_concurrency: int = 2
    _domains: dict[str, asyncio.Semaphore] = field(default_factory=dict, repr=False)

    @classmethod
    def from_config(cls, config: Config) -> Limits:
        return cls(
            global_=asyncio.Semaphore(config.global_concurrency),
            browser=asyncio.Semaphore(config.browser_concurrency),
            llm=asyncio.Semaphore(config.llm_concurrency),
            domain_concurrency=config.domain_concurrency,
        )

    def domain(self, domain: str) -> asyncio.Semaphore:
        """Lazily create a per-domain semaphore so each store is rate-limited
        independently - Blinkit being slow never affects Amul's cap."""
        if domain not in self._domains:
            self._domains[domain] = asyncio.Semaphore(self.domain_concurrency)
        return self._domains[domain]


# --------------------------------------------------------------------------
# LLM providers
# --------------------------------------------------------------------------

class LLMProvider(Protocol):
    name: str

    async def complete(self, prompt: str) -> str:
        """Return the raw text completion for *prompt*."""
        ...


class AnthropicProvider:
    """Anthropic Messages API (cloud, needs ANTHROPIC_API_KEY)."""

    name = "anthropic"

    def __init__(self, api_key: str | None, model: str = DEFAULT_ANTHROPIC_MODEL,
                 client: Any | None = None):
        self.api_key = api_key
        self.model = model
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ConfigError(
                    "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set"
                )
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def complete(self, prompt: str) -> str:
        client = self._get_client()
        try:
            resp = await client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK errors vary by version
            raise LLMError(f"anthropic request failed: {exc}") from exc
        parts = [block.text for block in resp.content if getattr(block, "text", None)]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("anthropic returned an empty response")
        return text


class OllamaProvider:
    """Self-hosted Ollama chat API (needs a reachable OLLAMA_BASE_URL)."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, client: httpx.AsyncClient,
                 timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client
        self.timeout = timeout

    async def complete(self, prompt: str) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = await self.client.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"ollama returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc

        content = ""
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "")
            content = content or str(data.get("response") or "")
        if not content.strip():
            raise LLMError(f"ollama returned an empty response: {data!r:.200}")
        return content.strip()


def build_provider(config: Config, http_client: httpx.AsyncClient) -> LLMProvider:
    if config.llm_provider == "anthropic":
        return AnthropicProvider(config.anthropic_api_key, config.anthropic_model)
    if config.llm_provider == "ollama":
        return OllamaProvider(config.ollama_base_url, config.ollama_model, http_client)
    raise ConfigError(f"unknown LLM provider: {config.llm_provider!r}")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(UTC)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc


def save_json(path: Path, data: Any) -> None:
    """Write state atomically: a crash mid-write can never corrupt the file.

    state.json is committed back to the repo after every CI run, so a torn
    write would poison every later run. We write to a sibling temp file and
    os.replace() it over the target - on POSIX that is an atomic rename.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def new_entry() -> dict[str, Any]:
    return {
        "last_status": "unknown",
        "run_count": 0,
        "cached_selector": None,
        "consecutive_failures": 0,
        "last_error": None,
        "error_alerted": False,
        "last_checked": None,
        "last_llm_at": None,
        "last_notified_at": None,
    }


def get_entry(state: dict[str, Any], product_id: str) -> dict[str, Any]:
    """Get (or create) a state entry, backfilling keys added by newer versions."""
    entry = state.get(product_id)
    if not isinstance(entry, dict):
        entry = {}
        state[product_id] = entry
    for key, value in new_entry().items():
        entry.setdefault(key, value)
    return entry


def prune_state(state: dict[str, Any], active_ids: set[str]) -> list[str]:
    """Drop state for products that are no longer tracked. Returns the ids removed."""
    stale = [pid for pid in state if pid not in active_ids]
    for pid in stale:
        del state[pid]
    return sorted(stale)


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------

def load_products(path: Path) -> list[dict[str, Any]]:
    raw = load_json(path, None)
    if raw is None:
        raise ConfigError(f"{path.name} is missing or empty")
    if not isinstance(raw, list):
        raise ConfigError(f"{path.name} must be a JSON array of products")
    if not raw:
        raise ConfigError(f"{path.name} has no products - add at least one")

    products: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{path.name}[{index}] must be a JSON object")
        url = item.get("url")
        name = item.get("name")
        if not url or not isinstance(url, str):
            raise ConfigError(f"{path.name}[{index}] is missing 'url'")
        if not name or not isinstance(name, str):
            raise ConfigError(f"{path.name}[{index}] is missing 'name'")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{path.name}[{index}] url must be http(s): {url!r}")

        product_id = str(item.get("id") or f"product_{index}")
        if product_id in seen_ids:
            raise ConfigError(f"{path.name}: duplicate product id {product_id!r}")
        seen_ids.add(product_id)

        render = str(item.get("render") or "http").lower()
        if render not in ("http", "browser"):
            raise ConfigError(
                f"{path.name}[{index}] render must be 'http' or 'browser', got {render!r}"
            )

        products.append({
            "id": product_id,
            "store": str(item.get("store") or get_domain(url)),
            "name": name,
            "url": url,
            "description": str(item.get("description") or ""),
            "render": render,
            "pincode": str(item.get("pincode") or "").strip(),
        })
    return products


def get_domain(url: str) -> str:
    return urlparse(url).netloc


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self, http_client: httpx.AsyncClient, config: Config,
                 dry_run: bool = False):
        self.http = http_client
        self.config = config
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(self.config.telegram_bot_token and self.config.telegram_chat_id)

    async def send(self, message: str) -> bool:
        """Send a Telegram message. Returns True only if Telegram accepted it."""
        if self.dry_run:
            log.info("[dry-run] would send to Telegram: %s", message.replace("\n", " | "))
            return False
        if not self.configured:
            log.warning("Telegram not configured, would have sent: %s",
                        message.replace("\n", " | "))
            return False
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        try:
            resp = await self.http.post(
                url,
                data={"chat_id": self.config.telegram_chat_id, "text": message},
                timeout=15.0,
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("Telegram send failed: %s", exc)
            return False


def cooldown_over(entry: dict[str, Any], config: Config,
                  now: datetime | None = None) -> bool:
    """True when enough time has passed since the last notification."""
    last = entry.get("last_notified_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    now = now or utcnow()
    return (now - last_dt).total_seconds() >= config.notify_cooldown_seconds


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

async def fetch_http(client: httpx.AsyncClient, url: str, *,
                     retries: int = 3, timeout: float = 20.0) -> str:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            delay = min(0.5 * (2 ** (attempt - 1)), 5.0) + random.uniform(0, 0.25)
            log.debug("retrying %s in %.2fs (attempt %d)", url, delay, attempt + 1)
            await asyncio.sleep(delay)
        try:
            resp = await client.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=timeout, follow_redirects=True,
            )
            if resp.status_code in RETRYABLE_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp,
                )
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in RETRYABLE_STATUS:
                raise FetchError(f"HTTP {status} for {url}") from exc
            last_error = exc
        except httpx.HTTPError as exc:
            last_error = exc
    raise FetchError(f"could not fetch {url} after {max(1, retries)} attempts: {last_error}")


async def _set_delivery_pincode(page: Any, url: str, pincode: str) -> None:
    """Best-effort: pre-select a delivery pincode for pincode-gated storefronts.

    Some stores (e.g. shop.amul.com, blinkit.com) return a 404 page for every
    product until a serviceable delivery pincode has been chosen.  Runs on the
    store home page first, then the caller navigates to the product URL on the
    same page so the SPA/localState keep the chosen pincode."""
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    try:
        await page.goto(base + "/", wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 - optional nicety, never fatal
            pass
        for label in (
            "Select Delivery Pincode", "Select Pincodes", "Change Pincode",
            "Enter Pincode", "Select Location",
        ):
            btn = page.get_by_text(label, exact=False).first
            try:
                await btn.click(timeout=4000)
                break
            except Exception:  # noqa: BLE001 - try the next label
                continue
        for selector in ("input[type=tel]", "input[maxlength='6']",
                         "input[type=text]", "input"):
            loc = page.locator(selector).first
            try:
                await loc.fill(pincode, timeout=3000)
                break
            except Exception:  # noqa: BLE001 - try the next selector
                continue
        try:
            await page.keyboard.press("Enter")
        except Exception:  # noqa: BLE001
            pass
        for label in ("Apply", "Submit", "Save", "OK"):
            btn = page.get_by_role("button", name=label).first
            try:
                await btn.click(timeout=2500)
                break
            except Exception:  # noqa: BLE001 - try the next label
                continue
        await asyncio.sleep(1)
    except Exception as exc:  # noqa: BLE001 - best effort only, never fatal
        log.debug("pincode setup failed for %s: %s", url, exc)


async def fetch_browser(browser: Any, url: str, limits: Limits, *,
                        retries: int = 1, timeout_ms: int = 30000,
                        pincode: str | None = None) -> str:
    """Render the page with headless Chromium - needed for sites that fill
    in stock status via JavaScript after the initial page load.

    Waits for 'domcontentloaded' first, then *briefly* for networkidle: many
    app-driven sites keep long-polling connections open, so networkidle alone
    would hang until the timeout every single run."""
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            await asyncio.sleep(min(0.5 * attempt, 5.0))
        async with limits.browser:
            page = await browser.new_page(user_agent=USER_AGENT)
            try:
                if pincode:
                    await _set_delivery_pincode(page, url, pincode)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 - optional nicety, never fatal
                    log.debug("networkidle wait timed out for %s (fine)", url)
                return await page.content()
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
            finally:
                await page.close()
    raise FetchError(f"could not render {url} after {max(1, retries)} attempts: {last_error}")


# --------------------------------------------------------------------------
# stock extraction
# --------------------------------------------------------------------------

def _availability_to_bool(value: Any) -> bool | None:
    if isinstance(value, dict):
        value = value.get("@type") or value.get("name") or ""
    text = str(value)
    if not text:
        return None
    for hint in JSON_LD_OUT_OF_STOCK:
        if hint in text:
            return False
    for hint in JSON_LD_IN_STOCK:
        if hint in text:
            return True
    return None


def _offers_to_bool(offers: Any) -> bool | None:
    if isinstance(offers, list):
        for offer in offers:
            result = _offers_to_bool(offer)
            if result is not None:
                return result
        return None
    if isinstance(offers, dict):
        result = _availability_to_bool(offers.get("availability"))
        if result is not None:
            return result
        # AggregateOffer: in stock if any inventory level is positive
        level = offers.get("inventoryLevel")
        if isinstance(level, dict):
            level = level.get("value")
        if isinstance(level, (int, float)):
            return level > 0
    return None


def _walk_json_ld(node: Any):
    """Yield every dict in a JSON-LD tree, including @graph / nested nodes."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_json_ld(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk_json_ld(value)


def check_json_ld(html: str) -> bool | None:
    """Tier 1: schema.org Product/Offer availability, if the site includes it."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_json_ld(data):
            if not isinstance(node, dict):
                continue
            # A node that is itself an Offer (or has offers attached).
            if "availability" in node:
                result = _availability_to_bool(node["availability"])
                if result is not None:
                    return result
            # An AggregateOffer at the top level of its own script block.
            if "inventoryLevel" in node:
                result = _offers_to_bool(node)
                if result is not None:
                    return result
            if "offers" in node:
                result = _offers_to_bool(node["offers"])
                if result is not None:
                    return result
    return None


def check_cached_selector(html: str, selector: str | None) -> bool | None:
    """Tier 2: reuse a selector the LLM identified on a previous run."""
    if not selector:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        el = soup.select_one(selector)
    except Exception:  # noqa: BLE001 - a bad cached selector must not crash a run
        log.debug("cached selector %r is no longer valid", selector)
        return None
    if el is None:
        return None
    text = el.get_text(" ", strip=True).lower()
    if not text:
        return None
    if any(hint in text for hint in OUT_OF_STOCK_HINTS):
        return False
    if any(hint in text for hint in IN_STOCK_HINTS):
        return True
    return None  # ambiguous - let the LLM tier handle it


def clean_text_for_llm(html: str, max_chars: int = 6000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg", "noscript", "iframe"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)[:max_chars]


def build_prompt(product_name: str, description: str, page_text: str) -> str:
    return f"""You are checking whether a specific product is currently in stock and purchasable on a retailer's web page.

Product name: {product_name}
Product description: {description}

Page content:
\"\"\"
{page_text}
\"\"\"

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"in_stock": true or false, "confidence": 0.0 to 1.0, "evidence": "short paraphrase of the text that led to your answer", "css_hint": "a short guess at a CSS selector for the stock-status element, or null"}}

Rules:
- in_stock must be true only if this specific product can be bought right now.
- confidence is how sure you are, from 0.0 (guess) to 1.0 (certain).
- css_hint should select the smallest element containing the stock status text."""


@dataclass(frozen=True)
class Verdict:
    in_stock: bool
    confidence: float
    evidence: str
    css_hint: str | None = None


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_response(raw: str) -> Verdict | None:
    """Parse + validate the LLM's JSON. Returns None if unusable."""
    if not raw or not raw.strip():
        return None
    text = _FENCE_RE.sub("", raw.strip()).strip()
    # Tolerate prose around the JSON object.
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            log.debug("LLM response contained no JSON object: %r", raw[:200])
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.debug("LLM response was not valid JSON (%s): %r", exc, raw[:200])
        return None
    if not isinstance(data, dict):
        return None
    if "in_stock" not in data:
        log.debug("LLM response missing 'in_stock': %r", raw[:200])
        return None

    in_stock = data["in_stock"]
    if isinstance(in_stock, bool):
        pass
    elif isinstance(in_stock, str) and in_stock.strip().lower() in ("true", "false"):
        in_stock = in_stock.strip().lower() == "true"
    elif isinstance(in_stock, (int, float)) and in_stock in (0, 1):
        in_stock = bool(in_stock)
    else:
        log.debug("LLM 'in_stock' is not a boolean: %r", in_stock)
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    evidence = str(data.get("evidence") or "").strip()
    css_hint = data.get("css_hint")
    if css_hint is not None and not isinstance(css_hint, str):
        css_hint = None
    if css_hint is not None:
        css_hint = css_hint.strip() or None

    return Verdict(in_stock=in_stock, confidence=confidence,
                   evidence=evidence, css_hint=css_hint)


async def check_with_llm(provider: LLMProvider, limits: Limits, html: str,
                         product_name: str, description: str) -> Verdict | None:
    """Tier 3: ask the LLM to read the page. Universal - works on any site,
    regardless of store or layout. Raises LLMError on transport failure."""
    prompt = build_prompt(product_name, description, clean_text_for_llm(html))
    async with limits.llm:
        raw = await provider.complete(prompt)
    verdict = parse_llm_response(raw)
    if verdict is None:
        log.warning("could not parse %s response: %r", provider.name, raw[:300])
    return verdict


# --------------------------------------------------------------------------
# per-product check
# --------------------------------------------------------------------------

@dataclass
class ProductResult:
    product_id: str
    store: str
    name: str
    url: str
    status: str = "unknown"      # in_stock | out_of_stock | unknown | error
    method: str | None = None    # json-ld | cached-selector | llm
    confidence: float | None = None
    notified: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("in_stock", "out_of_stock")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.product_id,
            "store": self.store,
            "name": self.name,
            "url": self.url,
            "status": self.status,
            "method": self.method,
            "confidence": self.confidence,
            "notified": self.notified,
            "error": self.error,
        }


@dataclass
class RunContext:
    config: Config
    state: dict[str, Any]
    limits: Limits
    http: httpx.AsyncClient
    provider: LLMProvider
    notifier: Notifier
    browser: Any | None = None


def _record_failure(entry: dict[str, Any], message: str) -> None:
    entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    entry["last_error"] = message


def _clear_failures(entry: dict[str, Any]) -> None:
    entry["consecutive_failures"] = 0
    entry["last_error"] = None
    entry["error_alerted"] = False


async def _maybe_alert_failures(product: dict[str, Any], entry: dict[str, Any],
                                ctx: RunContext, what: str) -> bool:
    """Notify once per failure streak, after ALERT_AFTER_FAILURES consecutive
    failures. Returns True if an alert was sent."""
    failures = int(entry.get("consecutive_failures") or 0)
    if failures < ctx.config.alert_after_failures:
        return False
    if entry.get("error_alerted"):
        return False
    message = (
        f"Stock watcher: {failures} consecutive failed checks for "
        f"{product['name']} ({product['store']})\n"
        f"Last error: {what}\n{product['url']}"
    )
    log.warning("[%s] [%s] %s", product["store"], product["name"], message.splitlines()[0])
    sent = await ctx.notifier.send(message)
    if sent:
        # Only mark alerted on success - if the notification failed to send,
        # leave it unmarked so the next run retries instead of silently losing
        # the alert forever.
        entry["error_alerted"] = True
        entry["last_notified_at"] = utcnow().isoformat()
    return sent


async def check_product(product: dict[str, Any], ctx: RunContext) -> ProductResult:
    """Check one product: fetch, run the extraction tiers, notify on
    out-of-stock -> in-stock transitions, and update its state entry."""
    url = product["url"]
    name = product["name"]
    store = product["store"]
    render = product["render"]
    domain = get_domain(url)

    entry = get_entry(ctx.state, product["id"])
    entry["run_count"] = int(entry.get("run_count") or 0) + 1

    result = ProductResult(product_id=product["id"], store=store, name=name, url=url)

    async with ctx.limits.global_:
        # --- fetch -------------------------------------------------------
        try:
            if render == "browser":
                if ctx.browser is None:
                    raise FetchError(
                        "render='browser' but no browser is running "
                        "(is playwright installed and chromium downloaded?)"
                    )
                async with ctx.limits.domain(domain):
                    html = await fetch_browser(
                        ctx.browser, url, ctx.limits,
                        retries=ctx.config.fetch_retries,
                        timeout_ms=ctx.config.browser_timeout_ms,
                        pincode=product.get("pincode") or None,
                    )
            else:
                async with ctx.limits.domain(domain):
                    html = await fetch_http(
                        ctx.http, url,
                        retries=ctx.config.fetch_retries,
                        timeout=ctx.config.fetch_timeout,
                    )
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
            message = str(exc) or type(exc).__name__
            _record_failure(entry, message)
            result.status = "error"
            result.error = message
            log.error("[%s] [%s] fetch failed (%d consecutive): %s",
                      store, name, entry["consecutive_failures"], message)
            await _maybe_alert_failures(product, entry, ctx, message)
            entry["last_checked"] = utcnow().isoformat()
            return result

        _clear_failures(entry)

        # --- tiered extraction -------------------------------------------
        force_revalidate = (
            ctx.config.revalidate_every > 0
            and entry["run_count"] % ctx.config.revalidate_every == 0
        )

        in_stock = check_json_ld(html)
        method = "json-ld" if in_stock is not None else None

        if in_stock is None and not force_revalidate:
            in_stock = check_cached_selector(html, entry.get("cached_selector"))
            if in_stock is not None:
                method = "cached-selector"

        if in_stock is None or force_revalidate:
            try:
                verdict = await check_with_llm(
                    ctx.provider, ctx.limits, html, name, product["description"],
                )
            except Exception as exc:  # noqa: BLE001 - an LLM outage must not kill the run
                _record_failure(entry, f"llm: {type(exc).__name__}: {exc}")
                log.error("[%s] [%s] LLM check failed: %s", store, name, exc)
                await _maybe_alert_failures(product, entry, ctx, str(exc))
                verdict = None

            if verdict is not None:
                entry["last_llm_at"] = utcnow().isoformat()
                if verdict.confidence < ctx.config.llm_min_confidence:
                    log.warning(
                        "[%s] [%s] LLM confidence %.2f below threshold %.2f, "
                        "ignoring verdict", store, name, verdict.confidence,
                        ctx.config.llm_min_confidence,
                    )
                    if force_revalidate and in_stock is not None:
                        pass  # keep the tier-1 answer we already have
                else:
                    if force_revalidate and in_stock is not None and in_stock != verdict.in_stock:
                        log.warning(
                            "[%s] [%s] revalidation disagrees with %s: %s vs llm=%s "
                            "(evidence: %s)", store, name, method, in_stock,
                            verdict.in_stock, verdict.evidence,
                        )
                    in_stock = verdict.in_stock
                    method = "llm"
                    result.confidence = verdict.confidence
                    if verdict.css_hint:
                        entry["cached_selector"] = verdict.css_hint
                    log.info("[%s] [%s] LLM: in_stock=%s (confidence %.2f) - %s",
                             store, name, in_stock, verdict.confidence, verdict.evidence)

        if in_stock is None:
            result.status = "unknown"
            log.info("[%s] [%s] could not determine stock status this run", store, name)
            entry["last_checked"] = utcnow().isoformat()
            return result

        # --- state + notify ----------------------------------------------
        new_status = "in_stock" if in_stock else "out_of_stock"
        previous = entry.get("last_status")
        entry["last_checked"] = utcnow().isoformat()

        if new_status == "in_stock" and previous != "in_stock":
            if cooldown_over(entry, ctx.config):
                sent = await ctx.notifier.send(f"In stock at {store}: {name}\n{url}")
                result.notified = sent
                if sent:
                    entry["last_notified_at"] = utcnow().isoformat()
            else:
                log.info("[%s] [%s] restock notification suppressed by cooldown",
                         store, name)

        entry["last_status"] = new_status
        result.status = new_status
        result.method = method
        log.info("[%s] [%s] status=%s via %s", store, name, new_status, method or "?")
        return result


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

async def run_checks(products: list[dict[str, Any]], state: dict[str, Any],
                     config: Config, *, dry_run: bool = False,
                     browser: Any | None = None,
                     http: httpx.AsyncClient | None = None,
                     provider: LLMProvider | None = None,
                     notifier: Notifier | None = None) -> list[ProductResult]:
    owns_http = http is None
    http = http or httpx.AsyncClient()
    try:
        # dry-run is a hard guarantee: nothing gets out, whatever notifier
        # the caller may have passed in.
        effective_notifier = (
            Notifier(http, config, dry_run=True)
            if dry_run
            else (notifier or Notifier(http, config))
        )
        ctx = RunContext(
            config=config,
            state=state,
            limits=Limits.from_config(config),
            http=http,
            provider=provider or build_provider(config, http),
            notifier=effective_notifier,
            browser=browser,
        )
        tasks = [check_product(p, ctx) for p in products]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[ProductResult] = []
        for product, outcome in zip(products, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                message = f"{type(outcome).__name__}: {outcome}"
                log.exception("[%s] [%s] unexpected error", product["store"], product["name"])
                entry = get_entry(state, product["id"])
                _record_failure(entry, message)
                results.append(ProductResult(
                    product_id=product["id"], store=product["store"],
                    name=product["name"], url=product["url"],
                    status="error", error=message,
                ))
            else:
                results.append(outcome)
        return results
    finally:
        if owns_http:
            await http.aclose()


async def _launch_browser(config: Config) -> tuple[Any | None, Any | None]:
    """Start Playwright + Chromium. Returns (browser, playwright) or (None, None)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("playwright is not installed - 'render': 'browser' products cannot be checked")
        return None, None
    try:
        playwright = await async_playwright().start()
    except Exception as exc:  # noqa: BLE001
        log.error("could not start playwright driver: %s", exc)
        return None, None
    try:
        browser = await playwright.chromium.launch()
        return browser, playwright
    except Exception as exc:  # noqa: BLE001
        log.error("could not start headless browser: %s", exc)
        await playwright.stop()
        return None, None


async def async_main(args: argparse.Namespace) -> int:
    config = Config.from_env()

    try:
        products = load_products(args.products_file)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.product:
        wanted = set(args.product)
        known = {p["id"] for p in products}
        missing = sorted(wanted - known)
        if missing:
            log.error("unknown product id(s): %s (known: %s)",
                      ", ".join(missing), ", ".join(sorted(known)))
            return 2
        products = [p for p in products if p["id"] in wanted]

    try:
        state = load_json(args.state_file, {})
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if not isinstance(state, dict):
        log.error("%s must contain a JSON object", args.state_file.name)
        return 2

    if args.prune and not args.product:
        for pid in prune_state(state, {p["id"] for p in products}):
            log.info("pruned stale state for removed product %r", pid)

    if not config.telegram_bot_token or not config.telegram_chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - "
                    "notifications will be printed instead of sent")

    browser = playwright = None
    if any(p["render"] == "browser" for p in products):
        browser, playwright = await _launch_browser(config)

    try:
        results = await run_checks(products, state, config,
                                   dry_run=args.dry_run, browser=browser)
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    if not args.dry_run:
        save_json(args.state_file, state)

    counts: dict[str, int] = {}
    for res in results:
        counts[res.status] = counts.get(res.status, 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    log.info("checked %d product(s): %s%s", len(results), summary,
             " (dry-run, state not saved)" if args.dry_run else "")

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watcher.py",
        description="Check product stock across multiple stores and notify on Telegram.",
    )
    parser.add_argument("--products-file", type=Path, default=PRODUCTS_FILE,
                        help="path to products.json (default: %(default)s)")
    parser.add_argument("--state-file", type=Path, default=STATE_FILE,
                        help="path to state.json (default: %(default)s)")
    parser.add_argument("--product", action="append", default=[], metavar="ID",
                        help="only check this product id (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="check everything but send no notifications and save no state")
    parser.add_argument("--json", action="store_true",
                        help="print per-product results as JSON on stdout")
    parser.add_argument("--no-prune", dest="prune", action="store_false",
                        help="keep state for products no longer in products.json")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="logging verbosity (default: %(default)s)")
    return parser


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    # httpx logs every single request at INFO - far too chatty for a watcher
    # that fetches dozens of pages per run.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(async_main(args))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
