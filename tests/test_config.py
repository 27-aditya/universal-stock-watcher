"""Config.from_env parsing and validation."""

from __future__ import annotations

import pytest

import watcher


def test_defaults():
    cfg = watcher.Config.from_env({})
    assert cfg.llm_provider == "anthropic"
    assert cfg.anthropic_model == watcher.DEFAULT_ANTHROPIC_MODEL
    assert cfg.ollama_base_url == "http://localhost:11434"
    assert cfg.ollama_model == "llama3.1"
    assert cfg.llm_concurrency == 5
    assert cfg.global_concurrency == 8
    assert cfg.domain_concurrency == 2
    assert cfg.browser_concurrency == 3
    assert cfg.llm_min_confidence == 0.5
    assert cfg.notify_cooldown_seconds == 1800
    assert cfg.alert_after_failures == 3
    assert cfg.fetch_retries == 3
    assert cfg.revalidate_every == 20
    assert cfg.telegram_bot_token is None
    assert cfg.anthropic_api_key is None


def test_all_values_read_from_env():
    cfg = watcher.Config.from_env({
        "TELEGRAM_BOT_TOKEN": "t0ken",
        "TELEGRAM_CHAT_ID": " 99 ",
        "LLM_PROVIDER": "OLLAMA",
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_MODEL": "claude-sonnet-4-5",
        "OLLAMA_BASE_URL": "https://llm.internal:11434/",
        "OLLAMA_MODEL": "qwen2.5:7b-instruct",
        "LLM_CONCURRENCY": "2",
        "GLOBAL_CONCURRENCY": "16",
        "DOMAIN_CONCURRENCY": "1",
        "BROWSER_CONCURRENCY": "4",
        "LLM_MIN_CONFIDENCE": "0.75",
        "NOTIFY_COOLDOWN_SECONDS": "600",
        "ALERT_AFTER_FAILURES": "10",
        "FETCH_RETRIES": "5",
        "REVALIDATE_EVERY": "7",
        "FETCH_TIMEOUT": "45.5",
        "BROWSER_TIMEOUT_MS": "45000",
    })
    assert cfg.telegram_bot_token == "t0ken"
    assert cfg.telegram_chat_id == "99"
    assert cfg.llm_provider == "ollama"
    assert cfg.anthropic_api_key == "sk-test"
    assert cfg.anthropic_model == "claude-sonnet-4-5"
    assert cfg.ollama_base_url == "https://llm.internal:11434/"
    assert cfg.ollama_model == "qwen2.5:7b-instruct"
    assert cfg.llm_concurrency == 2
    assert cfg.global_concurrency == 16
    assert cfg.domain_concurrency == 1
    assert cfg.browser_concurrency == 4
    assert cfg.llm_min_confidence == 0.75
    assert cfg.notify_cooldown_seconds == 600
    assert cfg.alert_after_failures == 10
    assert cfg.fetch_retries == 5
    assert cfg.revalidate_every == 7
    assert cfg.fetch_timeout == 45.5
    assert cfg.browser_timeout_ms == 45000


@pytest.mark.parametrize("env", [
    {"LLM_PROVIDER": "gemini"},
    {"LLM_CONCURRENCY": "many"},
    {"LLM_CONCURRENCY": "0"},
    {"LLM_MIN_CONFIDENCE": "1.5"},
    {"LLM_MIN_CONFIDENCE": "high"},
    {"FETCH_RETRIES": "-1"},
    {"REVALIDATE_EVERY": "x"},
    {"BROWSER_TIMEOUT_MS": "10"},
])
def test_invalid_values_raise_config_error(env):
    with pytest.raises(watcher.ConfigError):
        watcher.Config.from_env(env)


def test_empty_provider_falls_back_to_anthropic():
    # An empty repo variable (`vars.LLM_PROVIDER || 'anthropic'` in the
    # workflow) arrives as "" - that must not be treated as invalid.
    cfg = watcher.Config.from_env({"LLM_PROVIDER": "  "})
    assert cfg.llm_provider == "anthropic"
