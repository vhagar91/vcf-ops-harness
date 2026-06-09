"""Configuration loader — loads environment variables and provides typed config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# Grounding system prompt — instructs the model to answer only from tool data.
DEFAULT_SYSTEM_PROMPT = (
    "You are an infrastructure operations assistant for VMware vRealize/Aria "
    "Operations (vROps). You answer questions about alerts, health, and "
    "performance by calling the available tools.\n\n"
    "Rules:\n"
    "- Use tools to fetch data. Never invent metric values, alert text, "
    "resource names, or IDs.\n"
    "- To answer about a named resource, first find its ID with "
    "vrops_search_resources, then call the health/stats/alert tools with that ID.\n"
    "- A request about 'resource consumption', 'utilization', 'usage', or "
    "'performance' REQUIRES you to call vrops_get_latest_stats (current values) "
    "or vrops_get_stats (trends). You MUST fetch the metrics yourself. NEVER tell "
    "the user that values 'require additional queries' or 'can be retrieved' — "
    "retrieve them, then report the numbers.\n"
    "- Keep working (calling tools across turns) until you have everything needed "
    "to fully answer; only then write the final reply.\n"
    "- Common VM/host metric keys: cpu|usage_average (CPU %), mem|usage_average "
    "(memory %), mem|consumed_average (KB), disk|usage_average (KBps), "
    "virtualDisk|totalLatency (ms), net|usage_average (KBps). If unsure which "
    "keys exist for a resource, call vrops_get_stat_keys first.\n"
    "- If a name matches multiple resources, ask the user to disambiguate.\n"
    "- If a tool returns nothing or an error, say so plainly — do not guess.\n"
    "- Be concise and technical. Report numbers and units exactly as returned."
)


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

    system_prompt: str = field(default_factory=lambda: DEFAULT_SYSTEM_PROMPT)
    max_conversation_turns: int = 50

    # LLM guardrails
    max_output_tokens: int = 800
    request_timeout_s: float = 60.0
    max_tool_iterations: int = 5

    # Thinking-model handling ("auto" -> detect by model name)
    is_thinking_model: bool = False


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


_THINKING_MODEL_HINTS = ("qwen3", "qwq", "think", "deepseek-r1", "r1")


def _detect_thinking_model(provider: str, model: str) -> bool:
    """Resolve IS_THINKING_MODEL=auto|true|false against the active model name."""
    setting = os.environ.get("IS_THINKING_MODEL", "auto").lower()
    if setting in ("true", "1", "yes"):
        return True
    if setting in ("false", "0", "no"):
        return False
    name = model.lower()
    return any(hint in name for hint in _THINKING_MODEL_HINTS)


def load_config() -> HarnessConfig:
    """Read environment variables and return a validated ``HarnessConfig``."""
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3")
    active_model = ollama_model if provider == "ollama" else openai_model

    return HarnessConfig(
        slack_bot_token=_required("SLACK_BOT_TOKEN"),
        slack_signing_secret=_required("SLACK_SIGNING_SECRET"),
        slack_app_token=os.environ.get("SLACK_APP_TOKEN"),
        slack_port=int(os.environ.get("SLACK_PORT", "3000")),
        llm_provider=provider,
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=openai_model,
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        ollama_model=ollama_model,
        vrops_server=os.environ.get("VROPS_SERVER", ""),
        vrops_username=os.environ.get("VROPS_USERNAME", ""),
        vrops_password=os.environ.get("VROPS_PASSWORD", ""),
        vrops_auth_source=os.environ.get("VROPS_AUTH_SOURCE", "Local"),
        system_prompt=os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        max_conversation_turns=int(os.environ.get("MAX_CONVERSATION_TURNS", "50")),
        max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "800")),
        request_timeout_s=float(os.environ.get("REQUEST_TIMEOUT_S", "60")),
        max_tool_iterations=int(os.environ.get("MAX_TOOL_ITERATIONS", "5")),
        is_thinking_model=_detect_thinking_model(provider, active_model),
    )
