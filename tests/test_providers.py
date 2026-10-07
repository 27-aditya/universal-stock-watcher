"""LLM provider implementations (Anthropic + Ollama) and provider selection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import watcher

# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

class FakeAnthropicClient:
    """Stands in for anthropic.AsyncAnthropic: records calls, replays a reply."""

    def __init__(self, blocks: list[str] | None = None, error: Exception | None = None):
        self.blocks = blocks if blocks is not None else ['{"in_stock": true}']
        self.error = error
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(text=block) for block in self.blocks],
        )


@pytest.mark.asyncio
async def test_anthropic_provider_builds_request_and_returns_text():
    client = FakeAnthropicClient(blocks=['{"in_stock": true}'])
    provider = watcher.AnthropicProvider("sk-test", "claude-test", client=client)

    out = await provider.complete("hello")

    assert out == '{"in_stock": true}'
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "claude-test"
    assert call["max_tokens"] == 300
    assert call["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_anthropic_provider_joins_multiple_text_blocks():
    client = FakeAnthropicClient(blocks=["part one ", "part two"])
    provider = watcher.AnthropicProvider("sk-test", "claude-test", client=client)
    assert await provider.complete("x") == "part one part two"


@pytest.mark.asyncio
async def test_anthropic_provider_raises_llm_error_on_api_failure():
    client = FakeAnthropicClient(error=RuntimeError("rate limited"))
    provider = watcher.AnthropicProvider("sk-test", "claude-test", client=client)
    with pytest.raises(watcher.LLMError, match="rate limited"):
        await provider.complete("x")


@pytest.mark.asyncio
async def test_anthropic_provider_raises_on_empty_response():
    client = FakeAnthropicClient(blocks=["   "])
    provider = watcher.AnthropicProvider("sk-test", "claude-test", client=client)
    with pytest.raises(watcher.LLMError, match="empty"):
        await provider.complete("x")


def test_anthropic_provider_without_key_raises_config_error_lazily():
    provider = watcher.AnthropicProvider(None, "claude-test")
    with pytest.raises(watcher.ConfigError, match="ANTHROPIC_API_KEY"):
        provider._get_client()


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------

def _ollama(handler) -> watcher.OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return watcher.OllamaProvider("http://localhost:11434/", "llama3.1", client)


@pytest.mark.asyncio
async def test_ollama_provider_sends_chat_request_and_parses_message():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": '{"in_stock": false}'},
            "done": True,
        })

    provider = _ollama(handler)
    out = await provider.complete("check this")

    assert out == '{"in_stock": false}'
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["model"] == "llama3.1"
    assert captured["json"]["stream"] is False
    assert captured["json"]["format"] == "json"
    assert captured["json"]["messages"] == [{"role": "user", "content": "check this"}]


@pytest.mark.asyncio
async def test_ollama_provider_falls_back_to_generate_style_response_field():
    provider = _ollama(lambda request: httpx.Response(200, json={"response": "legacy body"}))
    assert await provider.complete("x") == "legacy body"


@pytest.mark.asyncio
async def test_ollama_provider_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    provider = _ollama(handler)
    with pytest.raises(watcher.LLMError, match="404"):
        await provider.complete("x")


@pytest.mark.asyncio
async def test_ollama_provider_raises_on_empty_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "  "}})

    provider = _ollama(handler)
    with pytest.raises(watcher.LLMError, match="empty"):
        await provider.complete("x")


@pytest.mark.asyncio
async def test_ollama_provider_raises_on_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="not json", headers={"content-type": "application/json"},
        )

    provider = _ollama(handler)
    with pytest.raises(watcher.LLMError):
        await provider.complete("x")


# --------------------------------------------------------------------------
# provider selection
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_provider_anthropic():
    cfg = watcher.Config.from_env({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk"})
    provider = watcher.build_provider(cfg, httpx.AsyncClient())
    assert isinstance(provider, watcher.AnthropicProvider)
    assert provider.model == watcher.DEFAULT_ANTHROPIC_MODEL


@pytest.mark.asyncio
async def test_build_provider_ollama_uses_shared_http_client():
    cfg = watcher.Config.from_env({"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen"})
    async with httpx.AsyncClient() as shared:
        provider = watcher.build_provider(cfg, shared)
        assert isinstance(provider, watcher.OllamaProvider)
        assert provider.model == "qwen"
        assert provider.client is shared


def test_build_provider_unknown_raises():
    cfg = watcher.Config(llm_provider="nope")
    with pytest.raises(watcher.ConfigError):
        watcher.build_provider(cfg, httpx.AsyncClient())
