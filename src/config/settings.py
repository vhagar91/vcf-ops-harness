"""Configuration loader — loads environment variables and provides typed config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class HarnessConfig:
    """Typed configuration loaded from environment variables."""

    # Required fields (no defaults)
    slack_bot_token: str
    slack_signing_secret: str

    # OpenAI (used when llm_provider == "openai")
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Ollama (used when llm_provider == "ollama")
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3"

    # Which LLM provider to use: "openai" or "ollama"
    llm_provider: str = "openai"

    # Optional Slack fields
    slack_app_token: str | None = None
    slack_port: int = 3000

    # vROps (for vROps actions)
    vrops_server: str = ""
    vrops_username: str = ""
    vrops_password: str = ""
    vrops_auth_source: str = "Local"

    system_prompt: str = field(
        default_factory=lambda: (
            "You are a helpful infrastructure automation assistant. "
            "Interpret the user's intent and, when appropriate, trigger available actions "
            "to fulfil their request. Always be concise and technical."
        )
    )
    max_conversation_turns: int = 50


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def load_config() -> HarnessConfig:
    """Read environment variables and return a validated ``HarnessConfig``."""
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()

    return HarnessConfig(
        slack_bot_token=_required("SLACK_BOT_TOKEN"),
        slack_signing_secret=_required("SLACK_SIGNING_SECRET"),
        slack_app_token=os.environ.get("SLACK_APP_TOKEN"),
        slack_port=int(os.environ.get("SLACK_PORT", "3000")),
        llm_provider=provider,
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3"),
        vrops_server=os.environ.get("VROPS_SERVER", ""),
        vrops_username=os.environ.get("VROPS_USERNAME", ""),
        vrops_password=os.environ.get("VROPS_PASSWORD", ""),
        vrops_auth_source=os.environ.get("VROPS_AUTH_SOURCE", "Local"),
        system_prompt=os.environ.get(
            "SYSTEM_PROMPT",
            (
                "You are a helpful infrastructure automation assistant. "
                "Interpret the user's intent and, when appropriate, trigger available actions "
                "to fulfil their request. Always be concise and technical."
            ),
        ),
        max_conversation_turns=int(os.environ.get("MAX_CONVERSATION_TURNS", "50")),
    )
