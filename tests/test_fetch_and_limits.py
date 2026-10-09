"""Fetching: HTTP retries, browser rendering, semaphore behaviour."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import (
    FakeNotifier,
    FakeProvider,
    html_page,
    make_ctx,
    make_product,
    product_json_ld,
)

import watcher

# --------------------------------------------------------------------------
# HTTP fetching
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_http_returns_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await watcher.fetch_http(client, "https://x.test/a") == "<html>hello</html>"


@pytest.mark.asyncio
async def test_fetch_http_retries_on_5xx_then_succeeds():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="finally")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = await watcher.fetch_http(client, "https://x.test/a", retries=3)

    assert body == "finally"
    assert attempts == 3


@pytest.mark.asyncio
async def test_fetch_http_does_not_retry_404():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, text="gone")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(watcher.FetchError, match="404"):
            await watcher.fetch_http(client, "https://x.test/a", retries=3)

    assert attempts == 1


@pytest.mark.asyncio
async def test_fetch_http_raises_after_exhausting_retries():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(watcher.FetchError, match="after 2 attempts"):
            await watcher.fetch_http(client, "https://x.test/a", retries=2)

    assert attempts == 2


@pytest.mark.asyncio
async def test_fetch_http_retries_on_429():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await watcher.fetch_http(client, "https://x.test/a", retries=2) == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_fetch_http_sends_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await watcher.fetch_http(client, "https://x.test/a")

    assert seen["ua"] == watcher.USER_AGENT
    assert "StockWatcher" in seen["ua"]


# --------------------------------------------------------------------------
# browser rendering
# --------------------------------------------------------------------------

class FakePage:
    def __init__(self, html="<html>rendered</html>", goto_error=None, idle_error=None):
        self.html = html
        self.goto_error = goto_error
        self.idle_error = idle_error
        self.closed = False
        self.goto_calls = []
        self.idle_calls = 0
        self.waited_for = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_error is not None:
            raise self.goto_error

    async def wait_for_load_state(self, state, timeout=None):
        self.waited_for.append(state)
        self.idle_calls += 1
        if self.idle_error is not None:
            raise self.idle_error

    async def content(self):
        return self.html

    async def close(self):
        self.closed = True


class PincodePage(FakePage):
    def __init__(self, html="<html>rendered</html>"):
        super().__init__(html=html)
        self.filled = []
        self.entered = False
        self.clicked = []

    def get_by_text(self, text, exact=False):
        return _StubLocator(self.clicked, text)

    def locator(self, selector):
        return _StubLocator(self.filled, selector)

    def get_by_role(self, role, name):
        return _StubLocator(self.clicked, name)

    @property
    def keyboard(self):
        return _StubKeyboard(self)


class _StubKeyboard:
    def __init__(self, owner):
        self.owner = owner

    async def press(self, key):
        self.owner.pressed = getattr(self.owner, "pressed", [])
        self.owner.pressed.append(key)


class _StubLocator:
    def __init__(self, log, label):
        self._log = log
        self._label = label
        self._first = False

    @property
    def first(self):
        self._first = True
        return self

    async def count(self):
        return 1

    async def click(self, timeout=None):
        self._log.append(("click", self._label))

    async def fill(self, value, timeout=None):
        self._log.append(("fill", value))


class FakeBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = 0

    async def new_page(self, **kwargs):
        self.created += 1
        return self.pages.pop(0)


@pytest.mark.asyncio
async def test_fetch_browser_waits_for_domcontentloaded_then_networkidle():
    page = FakePage(idle_error=TimeoutError())
    browser = FakeBrowser([page])
    ctx, _ = make_ctx()

    html = await watcher.fetch_browser(browser, "https://x.test/a", ctx.limits)

    assert html == "<html>rendered</html>"
    assert page.goto_calls[0][1] == "domcontentloaded"
    assert page.waited_for == ["networkidle"]
    assert page.closed


@pytest.mark.asyncio
async def test_fetch_browser_retries_after_goto_failure():
    bad = FakePage(goto_error=RuntimeError("nav failed"))
    good = FakePage(html="<html>second try</html>")
    browser = FakeBrowser([bad, good])
    ctx, _ = make_ctx()

    html = await watcher.fetch_browser(browser, "https://x.test/a", ctx.limits, retries=2)

    assert html == "<html>second try</html>"
    assert bad.closed and good.closed
    assert browser.created == 2


@pytest.mark.asyncio
async def test_fetch_browser_raises_after_exhausting_retries():
    browser = FakeBrowser([FakePage(goto_error=RuntimeError("nope")) for _ in range(2)])
    ctx, _ = make_ctx()

    with pytest.raises(watcher.FetchError, match="after 2 attempts"):
        await watcher.fetch_browser(browser, "https://x.test/a", ctx.limits, retries=2)


@pytest.mark.asyncio
async def test_fetch_browser_sets_delivery_pincode_before_product():
    page = PincodePage()
    browser = FakeBrowser([page])
    ctx, _ = make_ctx()

    html = await watcher.fetch_browser(
        browser, "https://shop.amul.com/en/product/amul", ctx.limits,
        pincode="400001",
    )

    assert html == "<html>rendered</html>"
    home, product = page.goto_calls
    assert home[0] == "https://shop.amul.com/"
    assert product[0] == "https://shop.amul.com/en/product/amul"
    assert ("click", "Select Delivery Pincode") in page.clicked
    assert ("fill", "400001") in page.filled
    assert ("click", "Apply") in page.clicked


@pytest.mark.asyncio
async def test_fetch_browser_skips_pincode_when_none():
    page = FakePage()
    browser = FakeBrowser([page])
    ctx, _ = make_ctx()

    await watcher.fetch_browser(browser, "https://a.test/x", ctx.limits)

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] == "https://a.test/x"


@pytest.mark.asyncio
async def test_products_loader_reads_pincode():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False
    ) as fh:
        json.dump([
            {"id": "a", "name": "A", "url": "https://a.test/x",
             "pincode": "400001"},
        ], fh)
        path = fh.name
    try:
        products = watcher.load_products(Path(path))
        assert products[0]["pincode"] == "400001"
    finally:
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_fetch_browser_respects_browser_semaphore_cap():
    in_flight = 0
    peak = 0

    class CountingPage(FakePage):
        async def content(self):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.02)
                return self.html
            finally:
                in_flight -= 1

    browser = FakeBrowser([CountingPage() for _ in range(5)])
    ctx, _ = make_ctx()
    ctx.limits.browser = asyncio.Semaphore(1)

    await asyncio.gather(*[
        watcher.fetch_browser(browser, f"https://x.test/{i}", ctx.limits)
        for i in range(5)
    ])
    assert peak == 1


# --------------------------------------------------------------------------
# semaphores
# --------------------------------------------------------------------------

def test_domain_semaphores_are_per_domain_and_reused():
    limits = watcher.Limits.from_config(watcher.Config(domain_concurrency=2))
    a1 = limits.domain("shop-a.test")
    a2 = limits.domain("shop-a.test")
    b1 = limits.domain("shop-b.test")
    assert a1 is a2
    assert a1 is not b1
    assert a1._value == 2  # noqa: SLF001 - asserting the configured cap


def test_limits_from_config_uses_all_concurrency_knobs():
    cfg = watcher.Config(
        global_concurrency=4, browser_concurrency=1, llm_concurrency=7,
        domain_concurrency=3,
    )
    limits = watcher.Limits.from_config(cfg)
    assert limits.global_._value == 4  # noqa: SLF001
    assert limits.browser._value == 1  # noqa: SLF001
    assert limits.llm._value == 7  # noqa: SLF001
    assert limits.domain_concurrency == 3


@pytest.mark.asyncio
async def test_global_semaphore_caps_concurrent_checks():
    """Even with many products, no more than GLOBAL_CONCURRENCY checks run at once."""
    import watcher as w

    cfg = watcher.Config(global_concurrency=2, fetch_retries=1)
    in_flight = 0
    peak = 0

    async def slow_fetch(client, url, *, retries=1, timeout=20.0):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return html_page(json_ld=product_json_ld("InStock"))

    original = w.fetch_http
    w.fetch_http = slow_fetch  # type: ignore[assignment]
    try:
        products = [make_product(id=f"p{i}", url=f"https://x.test/{i}") for i in range(6)]
        results = await w.run_checks(
            products, {}, cfg, dry_run=True, provider=FakeProvider(),
            notifier=FakeNotifier(),
        )
    finally:
        w.fetch_http = original  # type: ignore[assignment]

    assert len(results) == 6
    assert peak <= 2
    assert peak >= 1
