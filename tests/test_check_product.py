"""End-to-end behaviour of check_product: tier selection, notifications,
failure alerting, revalidation, and state transitions."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import FakeNotifier, FakeProvider, html_page, make_ctx, make_product, product_json_ld

import watcher


def _http_client(pages: dict[str, str] | None = None, status: int = 200):
    """Serve canned pages with 200; any unknown URL gets *status* (default 200)."""
    pages = pages or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(status, text="<html><body>error</body></html>")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


LLM_IN_STOCK = json.dumps({
    "in_stock": True, "confidence": 0.95,
    "evidence": "add to cart button present", "css_hint": ".buy-btn",
})
LLM_OUT = json.dumps({
    "in_stock": False, "confidence": 0.9,
    "evidence": "says sold out", "css_hint": None,
})


# --------------------------------------------------------------------------
# tier 1: JSON-LD short-circuits everything else
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_json_ld_answer_skips_llm_entirely():
    provider = FakeProvider()  # no scripted responses - would raise if called
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        provider=provider,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )
    product = make_product(url=url)

    result = await watcher.check_product(product, ctx)

    assert result.status == "in_stock"
    assert result.method == "json-ld"
    assert provider.prompts == []
    assert state["p1"]["last_status"] == "in_stock"


@pytest.mark.asyncio
async def test_json_ld_out_of_stock_does_not_notify():
    notifier = FakeNotifier()
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        notifier=notifier,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("OutOfStock"))}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["last_status"] = "in_stock"  # was in stock yesterday

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "out_of_stock"
    assert notifier.messages == []          # only restocks notify
    assert state["p1"]["last_status"] == "out_of_stock"


@pytest.mark.asyncio
async def test_out_of_stock_to_in_stock_transition_notifies_once():
    notifier = FakeNotifier()
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        notifier=notifier,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["last_status"] = "out_of_stock"

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert result.notified is True
    assert len(notifier.messages) == 1
    assert "In stock at example: Test Product" in notifier.messages[0]
    assert url in notifier.messages[0]
    assert state["p1"]["last_notified_at"] is not None


@pytest.mark.asyncio
async def test_repeated_in_stock_runs_do_not_renotify():
    notifier = FakeNotifier()
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        notifier=notifier,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )

    first = await watcher.check_product(make_product(url=url), ctx)
    second = await watcher.check_product(make_product(url=url), ctx)

    assert first.notified is True   # unknown -> in_stock is a transition
    assert second.notified is False  # already in stock
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_cooldown_suppresses_flapping_product():
    """Product bounces out->in->out->in faster than the cooldown window."""
    notifier = FakeNotifier()
    url = "https://shop.test/p/1"
    config = watcher.Config(notify_cooldown_seconds=3600)
    ctx, state = make_ctx(
        config, notifier=notifier,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["last_status"] = "out_of_stock"

    await watcher.check_product(make_product(url=url), ctx)   # notifies, stamps cooldown
    state["p1"]["last_status"] = "out_of_stock"                # simulate flap
    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert result.notified is False
    assert len(notifier.messages) == 1


# --------------------------------------------------------------------------
# tier 2: cached selector
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cached_selector_used_when_json_ld_absent():
    provider = FakeProvider()
    url = "https://shop.test/p/1"
    body = '<html><body><button class="buy-btn">Add to cart</button></body></html>'
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: body}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["cached_selector"] = ".buy-btn"
    state["p1"]["last_status"] = "out_of_stock"

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert result.method == "cached-selector"
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_ambiguous_cached_selector_falls_through_to_llm():
    provider = FakeProvider(LLM_OUT)
    url = "https://shop.test/p/1"
    body = '<html><body><div class="status">Green, medium</div></body></html>'
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: body}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["cached_selector"] = ".status"

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "out_of_stock"
    assert result.method == "llm"
    assert len(provider.prompts) == 1


@pytest.mark.asyncio
async def test_stale_cached_selector_falls_through_to_llm():
    provider = FakeProvider(LLM_IN_STOCK)
    url = "https://shop.test/p/1"
    body = '<html><body><p>totally different markup now</p></body></html>'
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: body}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["cached_selector"] = ".removed-element"

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.method == "llm"
    assert len(provider.prompts) == 1


# --------------------------------------------------------------------------
# tier 3: LLM
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_verdict_updates_state_and_caches_css_hint():
    provider = FakeProvider(LLM_IN_STOCK)
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: "<html><body>mystery</body></html>"}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["last_status"] = "out_of_stock"

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert result.method == "llm"
    assert result.confidence == 0.95
    assert result.notified is True
    assert state["p1"]["cached_selector"] == ".buy-btn"
    assert state["p1"]["last_llm_at"] is not None
    # the prompt carries the product identity so the LLM can match the variant
    prompt = provider.prompts[0]
    assert "Test Product" in prompt
    assert "a test product" in prompt


@pytest.mark.asyncio
async def test_llm_low_confidence_is_rejected():
    provider = FakeProvider(json.dumps({
        "in_stock": True, "confidence": 0.2, "evidence": "maybe", "css_hint": None,
    }))
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: "<html><body>?</body></html>"}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "unknown"
    assert result.method is None
    assert state["p1"]["last_status"] == "unknown"


@pytest.mark.asyncio
async def test_llm_min_confidence_is_configurable():
    provider = FakeProvider(json.dumps({
        "in_stock": False, "confidence": 0.3, "evidence": "unsure",
    }))
    url = "https://shop.test/p/1"
    config = watcher.Config(llm_min_confidence=0.1, notify_cooldown_seconds=0)
    ctx, _ = make_ctx(
        config, provider=provider,
        http_client=_http_client({url: "<html><body>?</body></html>"}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)
    assert result.status == "out_of_stock"
    assert result.method == "llm"


@pytest.mark.asyncio
async def test_unparseable_llm_response_yields_unknown():
    provider = FakeProvider("I cannot decide, sorry.")
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: "<html><body>?</body></html>"}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "unknown"
    assert state["p1"]["last_status"] == "unknown"


@pytest.mark.asyncio
async def test_llm_transport_failure_is_recorded_as_failure_not_crash():
    provider = FakeProvider(error=watcher.LLMError("ollama down"))
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(
        provider=provider, http_client=_http_client({url: "<html><body>?</body></html>"}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "unknown"
    entry = state["p1"]
    assert entry["consecutive_failures"] == 1
    assert "ollama down" in entry["last_error"]


# --------------------------------------------------------------------------
# revalidation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_nth_check_forces_llm_even_with_json_ld():
    provider = FakeProvider(LLM_OUT)
    url = "https://shop.test/p/1"
    config = watcher.Config(revalidate_every=5, notify_cooldown_seconds=0)
    ctx, state = make_ctx(
        config, provider=provider,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["run_count"] = 4  # next increment -> 5, divisible by 5

    result = await watcher.check_product(make_product(url=url), ctx)

    assert len(provider.prompts) == 1          # LLM forced
    assert result.status == "out_of_stock"      # LLM overrides json-ld on revalidate
    assert result.method == "llm"


@pytest.mark.asyncio
async def test_revalidation_disagreement_keeps_llm_answer_but_logs():
    provider = FakeProvider(LLM_IN_STOCK)
    url = "https://shop.test/p/1"
    config = watcher.Config(revalidate_every=1, notify_cooldown_seconds=0)
    ctx, _ = make_ctx(
        config, provider=provider,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("OutOfStock"))}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert result.method == "llm"


@pytest.mark.asyncio
async def test_revalidation_failure_keeps_tier1_answer():
    provider = FakeProvider(error=watcher.LLMError("down"))
    url = "https://shop.test/p/1"
    config = watcher.Config(revalidate_every=1, notify_cooldown_seconds=0)
    ctx, state = make_ctx(
        config, provider=provider,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"   # json-ld answer survives the LLM outage
    assert result.method == "json-ld"
    assert state["p1"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_revalidate_every_zero_disables_forced_checks():
    provider = FakeProvider()
    url = "https://shop.test/p/1"
    config = watcher.Config(revalidate_every=0)
    ctx, state = make_ctx(
        config, provider=provider,
        http_client=_http_client({url: html_page(json_ld=product_json_ld("InStock"))}),
    )
    state["p1"] = watcher.new_entry()
    state["p1"]["run_count"] = 99

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "in_stock"
    assert provider.prompts == []


# --------------------------------------------------------------------------
# fetch failures + alerts
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_failure_records_error_and_does_not_crash():
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(http_client=_http_client(status=500))

    result = await watcher.check_product(make_product(url=url), ctx)

    assert result.status == "error"
    assert "500" in result.error
    entry = state["p1"]
    assert entry["consecutive_failures"] == 1
    assert entry["last_status"] == "unknown"     # nothing decided
    assert entry["error_alerted"] is False


@pytest.mark.asyncio
async def test_no_alert_before_failure_threshold():
    notifier = FakeNotifier()
    config = watcher.Config(alert_after_failures=3, notify_cooldown_seconds=0)
    ctx, state = make_ctx(config, notifier=notifier, http_client=_http_client(status=500))
    state["p1"] = watcher.new_entry()
    state["p1"]["consecutive_failures"] = 1      # threshold is 3

    await watcher.check_product(make_product(), ctx)
    assert state["p1"]["consecutive_failures"] == 2
    assert notifier.messages == []               # still below threshold

    await watcher.check_product(make_product(), ctx)
    assert state["p1"]["consecutive_failures"] == 3
    assert len(notifier.messages) == 1           # alerted on crossing

    await watcher.check_product(make_product(), ctx)
    assert len(notifier.messages) == 1           # ...and not again


@pytest.mark.asyncio
async def test_alert_sent_once_per_failure_streak():
    notifier = FakeNotifier()
    config = watcher.Config(alert_after_failures=3, notify_cooldown_seconds=0)
    ctx, state = make_ctx(config, notifier=notifier, http_client=_http_client(status=500))
    state["p1"] = watcher.new_entry()

    for _ in range(5):
        await watcher.check_product(make_product(), ctx)

    assert state["p1"]["consecutive_failures"] == 5
    assert len(notifier.messages) == 1
    assert "consecutive failed checks" in notifier.messages[0]
    assert "Test Product" in notifier.messages[0]


@pytest.mark.asyncio
async def test_recovery_resets_failure_streak_and_allows_future_alert():
    notifier = FakeNotifier()
    config = watcher.Config(alert_after_failures=2, notify_cooldown_seconds=0)
    good = html_page(json_ld=product_json_ld("InStock"))
    bad_url = "https://shop.test/bad"
    good_url = "https://shop.test/good"
    ctx, state = make_ctx(
        config, notifier=notifier,
        http_client=_http_client({good_url: good}, status=500),
    )
    state["p1"] = watcher.new_entry()

    def alerts():
        return [m for m in notifier.messages if "consecutive failed checks" in m]

    # two failures -> alert
    await watcher.check_product(make_product(url=bad_url), ctx)
    await watcher.check_product(make_product(url=bad_url), ctx)
    assert len(alerts()) == 1

    # recovery clears the streak (and, since the page is in stock, restock-notifies)
    recovery = await watcher.check_product(make_product(url=good_url), ctx)
    assert recovery.status == "in_stock"
    assert state["p1"]["consecutive_failures"] == 0
    assert state["p1"]["error_alerted"] is False

    # a new streak alerts again
    await watcher.check_product(make_product(url=bad_url), ctx)
    await watcher.check_product(make_product(url=bad_url), ctx)
    assert len(alerts()) == 2
    assert state["p1"]["consecutive_failures"] == 2


@pytest.mark.asyncio
async def test_browser_product_without_browser_is_a_clean_error():
    product = make_product(render="browser")
    ctx, state = make_ctx()

    result = await watcher.check_product(product, ctx)

    assert result.status == "error"
    assert "no browser" in result.error
    assert state["p1"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_unknown_product_id_keys_are_isolated():
    """Two products on the same domain keep independent state."""
    url = "https://shop.test/shared"
    ctx, state = make_ctx(http_client=_http_client({
        url: html_page(json_ld=product_json_ld("InStock")),
    }))

    a = await watcher.check_product(make_product(id="a", url=url), ctx)
    b = await watcher.check_product(make_product(id="b", url=url), ctx)

    assert a.status == b.status == "in_stock"
    assert set(state) == {"a", "b"}
    assert state["a"]["run_count"] == 1
    assert state["b"]["run_count"] == 1


@pytest.mark.asyncio
async def test_run_count_increments_every_check():
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(http_client=_http_client({
        url: html_page(json_ld=product_json_ld("InStock")),
    }))

    for expected in (1, 2, 3):
        await watcher.check_product(make_product(url=url), ctx)
        assert state["p1"]["run_count"] == expected


@pytest.mark.asyncio
async def test_last_checked_timestamp_is_written():
    url = "https://shop.test/p/1"
    ctx, state = make_ctx(http_client=_http_client({
        url: html_page(json_ld=product_json_ld("InStock")),
    }))

    await watcher.check_product(make_product(url=url), ctx)

    assert state["p1"]["last_checked"] is not None
    datetime_fromisoformat(state["p1"]["last_checked"])


def datetime_fromisoformat(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value)
