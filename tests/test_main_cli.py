"""End-to-end tests through main(): CLI flags, state persistence, pruning,
dry-run semantics, exit codes, and the run_checks orchestration layer."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeNotifier, FakeProvider, html_page, product_json_ld

import watcher

IN_STOCK_PAGE = html_page(json_ld=product_json_ld("InStock"))
OUT_OF_STOCK_PAGE = html_page(json_ld=product_json_ld("OutOfStock"))


def _pages_client(pages: dict[str, str], status: int = 404):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(status, text="nope")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _write_products(path: Path, products: list[dict]) -> None:
    path.write_text(json.dumps(products), encoding="utf-8")


PRODUCT = {
    "id": "amul_whey_1kg",
    "store": "amul",
    "name": "Amul Whey 1kg",
    "url": "https://seller.test/product/amul-whey",
    "description": "1kg chocolate whey",
    "render": "http",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Run main() against temp products/state files with a mocked fetch layer."""
    products_path = tmp_path / "products.json"
    state_path = tmp_path / "state.json"

    def run_main(pages: dict[str, str], *, extra_args=(), products=None,
                 provider=None, notifier=None, config=None):
        _write_products(products_path, products if products is not None else [PRODUCT])
        config = config or watcher.Config(notify_cooldown_seconds=0)
        original_fetch_http = watcher.fetch_http
        original_build_provider = watcher.build_provider

        async def fake_fetch_http(client, url, *, retries=3, timeout=20.0):
            if url in pages:
                return pages[url]
            raise watcher.FetchError(f"HTTP 404 for {url}")

        def fake_build_provider(cfg, http_client):
            return provider or FakeProvider()

        watcher.fetch_http = fake_fetch_http  # type: ignore[assignment]
        watcher.build_provider = fake_build_provider  # type: ignore[assignment]

        original_run_checks = watcher.run_checks

        async def patched_run_checks(products_list, state, cfg, **kwargs):
            # Reuse the real orchestration, but with our injected notifier
            # and a deterministic config (the env-derived one has cooldowns).
            return await original_run_checks(
                products_list, state, config,
                notifier=notifier or FakeNotifier(), **kwargs,
            )

        watcher.run_checks = patched_run_checks  # type: ignore[assignment]

        argv = [
            "--products-file", str(products_path),
            "--state-file", str(state_path),
            *extra_args,
        ]
        # Config.from_env reads os.environ; make it deterministic
        monkeypatch.setenv("NOTIFY_COOLDOWN_SECONDS", str(config.notify_cooldown_seconds))
        try:
            return watcher.main(argv)
        finally:
            watcher.fetch_http = original_fetch_http  # type: ignore[assignment]
            watcher.build_provider = original_build_provider  # type: ignore[assignment]
            watcher.run_checks = original_run_checks  # type: ignore[assignment]

    return type("Env", (), {
        "run": staticmethod(run_main),
        "products_path": products_path,
        "state_path": state_path,
        "read_state": staticmethod(
            lambda: json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists() else {}
        ),
    })


def test_main_writes_state_and_returns_zero(env, capsys):
    code = env.run({PRODUCT["url"]: IN_STOCK_PAGE})
    assert code == 0
    state = env.read_state()
    assert state["amul_whey_1kg"]["last_status"] == "in_stock"
    assert state["amul_whey_1kg"]["run_count"] == 1


def test_main_json_output(env, capsys):
    code = env.run({PRODUCT["url"]: OUT_OF_STOCK_PAGE}, extra_args=["--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["id"] == "amul_whey_1kg"
    assert out[0]["status"] == "out_of_stock"
    assert out[0]["method"] == "json-ld"
    assert out[0]["url"] == PRODUCT["url"]


def test_main_dry_run_does_not_write_state(env, capsys):
    code = env.run({PRODUCT["url"]: IN_STOCK_PAGE}, extra_args=["--dry-run", "--json"])
    assert code == 0
    assert not env.state_path.exists()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["status"] == "in_stock"
    assert out[0]["notified"] is False


def test_main_dry_run_does_not_notify(env):
    notifier = FakeNotifier()
    code = env.run({PRODUCT["url"]: IN_STOCK_PAGE}, extra_args=["--dry-run"],
                   notifier=notifier)
    assert code == 0
    # notifier is bypassed entirely in dry-run by run_checks' own Notifier,
    # so our injected one must never be asked to send
    assert notifier.messages == []


def test_main_notifies_on_restock_transition(env):
    notifier = FakeNotifier()
    # first run: out of stock
    env.run({PRODUCT["url"]: OUT_OF_STOCK_PAGE}, notifier=notifier)
    assert notifier.messages == []
    # second run: back in stock
    env.run({PRODUCT["url"]: IN_STOCK_PAGE}, notifier=notifier)
    assert len(notifier.messages) == 1
    assert "In stock at amul" in notifier.messages[0]
    # third run: still in stock, no repeat
    env.run({PRODUCT["url"]: IN_STOCK_PAGE}, notifier=notifier)
    assert len(notifier.messages) == 1


def test_main_persists_cached_selector_across_runs(env):
    provider = FakeProvider(json.dumps({
        "in_stock": True, "confidence": 0.9, "evidence": "btn",
        "css_hint": ".add-to-cart",
    }))
    env.run({"https://seller.test/product/amul-whey": "<html><body>mystery</body></html>"},
            provider=provider)
    assert env.read_state()["amul_whey_1kg"]["cached_selector"] == ".add-to-cart"

    # second run: the selector is now known, so the LLM must not be called
    provider2 = FakeProvider()  # would raise if used
    env.run({"https://seller.test/product/amul-whey":
             '<html><body><button class="add-to-cart">Add to cart</button></body></html>'},
            provider=provider2)
    assert provider2.prompts == []
    assert env.read_state()["amul_whey_1kg"]["last_status"] == "in_stock"


def test_main_prunes_stale_state(env):
    env.run({PRODUCT["url"]: IN_STOCK_PAGE})
    assert "amul_whey_1kg" in env.read_state()

    other = dict(PRODUCT, id="other_thing", url="https://seller.test/other")
    env.run({other["url"]: IN_STOCK_PAGE}, products=[other])
    state = env.read_state()
    assert "amul_whey_1kg" not in state
    assert "other_thing" in state


def test_main_no_prune_flag_keeps_stale_state(env):
    env.run({PRODUCT["url"]: IN_STOCK_PAGE})
    other = dict(PRODUCT, id="other_thing", url="https://seller.test/other")
    env.run({other["url"]: IN_STOCK_PAGE}, products=[other],
            extra_args=["--no-prune"])
    state = env.read_state()
    assert "amul_whey_1kg" in state
    assert "other_thing" in state


def test_main_product_filter_checks_only_that_product(env, capsys):
    a = dict(PRODUCT)
    b = dict(PRODUCT, id="second", url="https://seller.test/second")
    code = env.run(
        {a["url"]: IN_STOCK_PAGE, b["url"]: OUT_OF_STOCK_PAGE},
        products=[a, b],
        extra_args=["--product", "second", "--json"],
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in out] == ["second"]


def test_main_product_filter_does_not_prune_others(env):
    a = dict(PRODUCT)
    b = dict(PRODUCT, id="second", url="https://seller.test/second")
    env.run({a["url"]: IN_STOCK_PAGE}, products=[a, b])   # both checked (b 404s)
    env.run({b["url"]: IN_STOCK_PAGE}, products=[a, b],
            extra_args=["--product", "second"])
    state = env.read_state()
    assert "amul_whey_1kg" in state            # untouched, not pruned
    assert state["amul_whey_1kg"]["run_count"] == 1
    assert state["second"]["run_count"] == 2   # ran once per invocation


def test_main_unknown_product_id_exits_2(env, capsys):
    code = env.run({PRODUCT["url"]: IN_STOCK_PAGE}, extra_args=["--product", "nope"])
    assert code == 2


def test_main_missing_products_file_exits_2(tmp_path, capsys):
    code = watcher.main([
        "--products-file", str(tmp_path / "absent.json"),
        "--state-file", str(tmp_path / "state.json"),
    ])
    assert code == 2
    assert "missing" in capsys.readouterr().err


def test_main_invalid_products_file_exits_2(tmp_path, capsys):
    bad = tmp_path / "products.json"
    bad.write_text("[{}]", encoding="utf-8")
    code = watcher.main([
        "--products-file", str(bad),
        "--state-file", str(tmp_path / "state.json"),
    ])
    assert code == 2
    assert "missing 'url'" in capsys.readouterr().err


def test_main_corrupt_state_file_exits_2(tmp_path, capsys):
    products = tmp_path / "products.json"
    products.write_text(json.dumps([PRODUCT]), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    code = watcher.main(["--products-file", str(products), "--state-file", str(state)])
    assert code == 2


def test_main_fetch_failures_still_exit_zero_and_save_state(env, capsys):
    """A broken site must not fail the whole workflow run."""
    code = env.run({}, extra_args=["--json"])   # no page matches -> FetchError
    assert code == 0
    entry = env.read_state()["amul_whey_1kg"]
    assert entry["consecutive_failures"] == 1
    assert entry["last_status"] == "unknown"


def test_main_failure_alert_after_threshold(env):
    notifier = FakeNotifier()
    config = watcher.Config(alert_after_failures=2, notify_cooldown_seconds=0)
    env.run({}, notifier=notifier, config=config)
    env.run({}, notifier=notifier, config=config)
    alerts = [m for m in notifier.messages if "consecutive failed checks" in m]
    assert len(alerts) == 1
    assert "Amul Whey 1kg" in alerts[0]


def test_main_bad_log_level_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        watcher.main(["--log-level", "LOUD"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# run_checks orchestration
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_checks_returns_result_per_product_even_if_one_raises():
    products = [
        {"id": "ok", "store": "a", "name": "A", "url": "https://x.test/ok",
         "description": "", "render": "http"},
        {"id": "boom", "store": "b", "name": "B", "url": "https://x.test/boom",
         "description": "", "render": "http"},
    ]

    async def exploding_fetch(client, url, *, retries=3, timeout=20.0):
        if url.endswith("/boom"):
            raise ValueError("kaboom")
        return html_page(json_ld=product_json_ld("InStock"))

    original = watcher.fetch_http
    watcher.fetch_http = exploding_fetch  # type: ignore[assignment]
    try:
        results = await watcher.run_checks(
            products, {}, watcher.Config(fetch_retries=1),
            provider=FakeProvider(), notifier=FakeNotifier(),
        )
    finally:
        watcher.fetch_http = original  # type: ignore[assignment]

    by_id = {r.product_id: r for r in results}
    assert len(results) == 2
    assert by_id["ok"].status == "in_stock"
    assert by_id["boom"].status == "error"
    assert "kaboom" in by_id["boom"].error


@pytest.mark.asyncio
async def test_run_checks_does_not_close_injected_client():
    """An externally supplied httpx client must stay open for the caller."""
    client = httpx.AsyncClient()
    products = [{"id": "p", "store": "s", "name": "N", "url": "https://x.test/p",
                 "description": "", "render": "http"}]
    await watcher.run_checks(
        products, {}, watcher.Config(fetch_retries=1),
        http=client, provider=FakeProvider(), notifier=FakeNotifier(),
    )
    assert client.is_closed is False
    await client.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_run_checks_closes_client_it_created_itself(monkeypatch):
    """When run_checks builds its own client, it must not leak it."""
    closed = []

    class TrackingClient(httpx.AsyncClient):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    products = [{"id": "p", "store": "s", "name": "N", "url": "https://x.test/p",
                 "description": "", "render": "http"}]
    monkeypatch.setattr(watcher.httpx, "AsyncClient", TrackingClient)
    await watcher.run_checks(
        products, {}, watcher.Config(fetch_retries=1),
        provider=FakeProvider(), notifier=FakeNotifier(),
    )
    assert closed == [True]
