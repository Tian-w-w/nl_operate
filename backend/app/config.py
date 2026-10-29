import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def env(name, default, cast=str):
    return field(default_factory=lambda: cast(os.getenv(name, default)))


@dataclass
class Settings:
    database_url: str = env('DATABASE_URL', 'sqlite:///./runtime/llm4web_demo.db')
    reference_time: str = env('REFERENCE_TIME', '2026-10-26T12:00:00+08:00')
    result_ttl: int = env('RESULT_TTL_SECONDS', '1800', int)
    confirm_ttl: int = env('CONFIRM_TTL_SECONDS', '300', int)
    auth_mode: str = env('AUTH_MODE', 'demo')
    model_adapter: str = env('MODEL_ADAPTER', 'rules')
    model_enabled: bool = env('MODEL_CALLS_ENABLED', 'false', lambda v: v.lower() == 'true')
    model_name: str = env('MODEL_NAME', '')
    model_timeout: float = env('MODEL_TIMEOUT_SECONDS', '20', float)
    model_max_output_tokens: int = env('MODEL_MAX_OUTPUT_TOKENS', '1024', int)
    model_max_calls: int = env('MODEL_MAX_CALLS_PER_SESSION', '20', int)
    agent_max_model_calls: int = env('AGENT_MAX_MODEL_CALLS', '6', int)
    agent_max_tool_calls: int = env('AGENT_MAX_TOOL_CALLS', '4', int)
    agent_max_seconds: float = env('AGENT_MAX_SECONDS', '90', float)
    agent_repeat_limit: int = env('AGENT_REPEAT_LIMIT', '1', int)
    model_budget_usd: Decimal = env('MODEL_BUDGET_USD_PER_SESSION', '0', Decimal)
    model_project_budget_usd: Decimal = env('MODEL_PROJECT_BUDGET_USD', '0', Decimal)
    model_input_rate: Decimal = env('MODEL_INPUT_USD_PER_MILLION', '0', Decimal)
    model_output_rate: Decimal = env('MODEL_OUTPUT_USD_PER_MILLION', '0', Decimal)
    model_api_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', ''), repr=False)
    local_model_base_url: str = env('LOCAL_MODEL_BASE_URL', '')
    local_model_api_key: str = field(default_factory=lambda: os.getenv('LOCAL_MODEL_API_KEY', ''), repr=False)
    local_model_protocol: str = env('LOCAL_MODEL_PROTOCOL', 'chat_completions')

    def __post_init__(self):
        if self.auth_mode not in {'demo','disabled'} or self.model_adapter not in {'rules','openai','local'}:
            raise ValueError('Unsupported AUTH_MODE / MODEL_ADAPTER')
        if self.local_model_protocol not in {'chat_completions','responses'}:
            raise ValueError('Unsupported LOCAL_MODEL_PROTOCOL')
        if self.model_adapter == 'local' and self.model_enabled:
            url = urlsplit(self.local_model_base_url)
            if not self.model_name or url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Enabled local model requires MODEL_NAME and an HTTP(S) LOCAL_MODEL_BASE_URL without credentials/query/fragment')
        if datetime.fromisoformat(self.reference_time).tzinfo is None:
            raise ValueError('REFERENCE_TIME must include a timezone')
        if self.result_ttl <= 0 or self.confirm_ttl <= 0 or not 0 < self.model_timeout <= 60:
            raise ValueError('Invalid TTL or model timeout')
        if not 16 <= self.model_max_output_tokens <= 8192 or not 1 <= self.model_max_calls <= 1000:
            raise ValueError('Invalid model call/output limit')
        if not 1 <= self.agent_max_model_calls <= 20 or not 1 <= self.agent_max_tool_calls <= 20 or not 0 < self.agent_max_seconds <= 180 or not 1 <= self.agent_repeat_limit <= 3:
            raise ValueError('Invalid Agent limits')
        if any(not value.is_finite() or value < 0 for value in [self.model_budget_usd,self.model_project_budget_usd,self.model_input_rate,self.model_output_rate]):
            raise ValueError('Model rates and budget must be finite and nonnegative')
        if self.model_adapter == 'openai' and self.model_enabled:
            if not self.model_api_key or not self.model_name or self.model_budget_usd <= 0 or self.model_project_budget_usd <= 0 or self.model_input_rate <= 0 or self.model_output_rate <= 0:
                raise ValueError('Enabled OpenAI calls require API key, model, budget and explicit positive token rates')

    @property
    def reference(self):
        return datetime.fromisoformat(self.reference_time).astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)
