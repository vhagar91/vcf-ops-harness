"""Tests for HarnessConfig helpers."""

from src.config.settings import HarnessConfig


def _config(provider: str) -> HarnessConfig:
    return HarnessConfig(
        slack_bot_token="t",
        slack_signing_secret="s",
        llm_provider=provider,
        openai_model="gpt-4o",
        ollama_model="qwen3",
        anthropic_model="claude-opus-4-8",
    )


def test_active_model_openai():
    assert _config("openai").active_model == "gpt-4o"


def test_active_model_ollama():
    assert _config("ollama").active_model == "qwen3"


def test_active_model_anthropic():
    assert _config("anthropic").active_model == "claude-opus-4-8"


def test_active_model_unknown_provider_falls_back_to_openai():
    assert _config("mystery").active_model == "gpt-4o"
