"""Shared fixtures for the stock-watcher test suite."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import watcher

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class FakeProvider:
    """LLM provider stub: replays scripted responses and records prompts."""

    name = "fake"

    def __init__(self, *responses: str, error: Exception | None = None):
        self.responses = list(responses)
        self.error = error
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self.responses.pop(0)


class FakeNotifier:
    """Notifier stub: records messages instead of hitting Telegram."""

    def __init__(self, configured: bool = True, fail: bool = False):
        self.configured = configured
        self.fail = fail
        self.messages: list[str] = []

    async def send(self, message: str) -> bool:
        self.messages.append(message)
        if self.fail:
            return False
        return self.configured


def make_product(**overrides: Any) -> dict[str, Any]:
    product = {
        "id": "p1",
        "store": "example",
        "name": "Test Product",
        "url": "https://shop.example.com/product/1",
        "description": "a test product",
        "render": "http",
    }
    product.update(overrides)
    return product


def html_page(*, json_ld: str | None = None, body: str = "<html><body></body></html>") -> str:
    if json_ld is None:
        return body
    return (
        '<html><head><script type="application/ld+json">'
        + json_ld
        + "</script></head><body>"
        + body
        + "</body></html>"
    )


def product_json_ld(availability: str) -> str:
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Test Product",
        "offers": {
            "@type": "Offer",
            "price": "10.00",
            "availability": f"https://schema.org/{availability}",
        },
    })


def make_ctx(
    config: watcher.Config | None = None,
    *,
    state: dict[str, Any] | None = None,
    provider: Any | None = None,
    notifier: Any | None = None,
    http_client: httpx.AsyncClient | None = None,
    browser: Any | None = None,
) -> tuple[watcher.RunContext, dict[str, Any]]:
    config = config or watcher.Config()
    state = state if state is not None else {}
    ctx = watcher.RunContext(
        config=config,
        state=state,
        limits=watcher.Limits.from_config(config),
        http=http_client or httpx.AsyncClient(transport=httpx.MockTransport(_echo)),
        provider=provider or FakeProvider(),
        notifier=notifier or FakeNotifier(),
        browser=browser,
    )
    return ctx, state


def _echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html><body>ok</body></html>")


def run(coro):
    """Run a coroutine from sync tests."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def config() -> watcher.Config:
    return watcher.Config(
        telegram_bot_token="token",
        telegram_chat_id="42",
        notify_cooldown_seconds=0,
    )


@pytest.fixture
def products_file(tmp_path):
    def _write(products: list[dict]) -> str:
        path = tmp_path / "products.json"
        path.write_text(json.dumps(products), encoding="utf-8")
        return str(path)

    return _write


@pytest.fixture
def state_file(tmp_path):
    def _write(state: dict) -> str:
        path = tmp_path / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return str(path)

    return _write
