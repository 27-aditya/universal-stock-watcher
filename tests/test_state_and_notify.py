"""State handling, product loading, and the Notifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import watcher

# --------------------------------------------------------------------------
# state entries
# --------------------------------------------------------------------------

def test_new_entry_has_all_keys():
    entry = watcher.new_entry()
    assert entry["last_status"] == "unknown"
    assert entry["run_count"] == 0
    assert entry["cached_selector"] is None
    assert entry["consecutive_failures"] == 0
    assert entry["error_alerted"] is False


def test_get_entry_creates_and_backfills():
    state = {"p1": {"last_status": "in_stock", "run_count": 4}}
    entry = watcher.get_entry(state, "p1")
    # existing values preserved
    assert entry["last_status"] == "in_stock"
    assert entry["run_count"] == 4
    # new keys backfilled
    assert entry["cached_selector"] is None
    assert entry["consecutive_failures"] == 0
    assert entry["last_notified_at"] is None


def test_get_entry_replaces_corrupt_entry():
    state = {"p1": "not a dict"}
    entry = watcher.get_entry(state, "p1")
    assert isinstance(entry, dict)
    assert entry["run_count"] == 0


def test_prune_state_removes_only_stale():
    state = {"keep": {}, "drop": {}, "also_drop": {}}
    removed = watcher.prune_state(state, {"keep"})
    assert removed == ["also_drop", "drop"]
    assert set(state) == {"keep"}


def test_prune_state_nothing_to_remove():
    state = {"a": {}, "b": {}}
    assert watcher.prune_state(state, {"a", "b"}) == []
    assert set(state) == {"a", "b"}


# --------------------------------------------------------------------------
# load / save json
# --------------------------------------------------------------------------

def test_load_json_missing_returns_default(tmp_path):
    assert watcher.load_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}


def test_load_json_invalid_raises_config_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(watcher.ConfigError, match="not valid JSON"):
        watcher.load_json(path, {})


def test_save_json_roundtrip(tmp_path):
    path = tmp_path / "out.json"
    watcher.save_json(path, {"b": 1, "a": [1, 2]})
    assert watcher.load_json(path, None) == {"b": 1, "a": [1, 2]}


# --------------------------------------------------------------------------
# load_products
# --------------------------------------------------------------------------

def test_load_products_fills_defaults(tmp_path):
    path = tmp_path / "products.json"
    path.write_text('[{"url": "https://shop.test/p/1", "name": "Thing"}]', encoding="utf-8")
    products = watcher.load_products(path)
    assert products[0]["id"] == "product_0"
    assert products[0]["store"] == "shop.test"
    assert products[0]["render"] == "http"
    assert products[0]["description"] == ""


@pytest.mark.parametrize("payload,match", [
    (None, "missing or empty"),
    ("{}", "must be a JSON array"),
    ("[]", "no products"),
    ('[{"name": "x"}]', "missing 'url'"),
    ('[{"url": "https://a.test/x"}]', "missing 'name'"),
    ('[{"url": "ftp://a.test/x", "name": "x"}]', "http"),
    ('[{"url": "https://a.test/x", "name": "x", "render": "ssr"}]', "render"),
    ('[{"url": "https://a.test/x", "name": "x", "id": "a"},'
    ' {"url": "https://a.test/y", "name": "y", "id": "a"}]', "duplicate"),
    ('{"url": "https://a.test/x"}', "JSON array"),
    ('[42]', "JSON object"),
])
def test_load_products_rejects_bad_input(tmp_path, payload, match):
    path = tmp_path / "products.json"
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    with pytest.raises(watcher.ConfigError, match=match):
        watcher.load_products(path)


def test_load_products_missing_file(tmp_path):
    with pytest.raises(watcher.ConfigError, match="missing"):
        watcher.load_products(tmp_path / "products.json")


# --------------------------------------------------------------------------
# cooldown
# --------------------------------------------------------------------------

def _entry_with_notification(seconds_ago: float) -> dict:
    stamp = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return {"last_notified_at": stamp.isoformat()}


def test_cooldown_true_when_never_notified():
    cfg = watcher.Config(notify_cooldown_seconds=1800)
    assert watcher.cooldown_over({}, cfg) is True


def test_cooldown_false_inside_window():
    cfg = watcher.Config(notify_cooldown_seconds=1800)
    assert watcher.cooldown_over(_entry_with_notification(60), cfg) is False


def test_cooldown_true_after_window():
    cfg = watcher.Config(notify_cooldown_seconds=1800)
    assert watcher.cooldown_over(_entry_with_notification(3600), cfg) is True


def test_cooldown_zero_seconds_always_allows():
    cfg = watcher.Config(notify_cooldown_seconds=0)
    assert watcher.cooldown_over(_entry_with_notification(0), cfg) is True


def test_cooldown_unparseable_timestamp_allows():
    cfg = watcher.Config(notify_cooldown_seconds=1800)
    assert watcher.cooldown_over({"last_notified_at": "not-a-date"}, cfg) is True


def test_cooldown_naive_timestamp_treated_as_utc():
    cfg = watcher.Config(notify_cooldown_seconds=60)
    naive = (datetime.now(UTC) - timedelta(seconds=5)).replace(tzinfo=None)
    assert watcher.cooldown_over({"last_notified_at": naive.isoformat()}, cfg) is False


# --------------------------------------------------------------------------
# Notifier
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_not_configured_returns_false_and_prints():
    cfg = watcher.Config()
    notifier = watcher.Notifier(httpx.AsyncClient(), cfg)
    assert notifier.configured is False
    assert await notifier.send("hello") is False


@pytest.mark.asyncio
async def test_notifier_dry_run_skips_network():
    cfg = watcher.Config(telegram_bot_token="t", telegram_chat_id="1")
    notifier = watcher.Notifier(httpx.AsyncClient(), cfg, dry_run=True)
    assert await notifier.send("hello") is False


@pytest.mark.asyncio
async def test_notifier_posts_to_telegram_and_returns_true():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        seen["url"] = str(request.url)
        seen["body"] = {
            key: values[0]
            for key, values in parse_qs(request.content.decode()).items()
        }
        return httpx.Response(200, json={"ok": True})

    cfg = watcher.Config(telegram_bot_token="SECRET", telegram_chat_id="42")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = watcher.Notifier(client, cfg)
        assert await notifier.send("back in stock") is True

    assert seen["url"] == "https://api.telegram.org/botSECRET/sendMessage"
    assert seen["body"]["chat_id"] == "42"
    assert seen["body"]["text"] == "back in stock"


@pytest.mark.asyncio
async def test_notifier_returns_false_on_http_error():
    cfg = watcher.Config(telegram_bot_token="t", telegram_chat_id="1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = watcher.Notifier(client, cfg)
        assert await notifier.send("x") is False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_arg_parser_defaults():
    args = watcher.build_arg_parser().parse_args([])
    assert args.product == []
    assert args.dry_run is False
    assert args.json is False
    assert args.prune is True
    assert args.log_level == "INFO"


def test_arg_parser_repeatable_product_flag():
    args = watcher.build_arg_parser().parse_args(
        ["--product", "a", "--product", "b", "--dry-run", "--json"]
    )
    assert args.product == ["a", "b"]
    assert args.dry_run is True
    assert args.json is True
