from __future__ import annotations

import ast
import copy
import email.utils
import hashlib
import json
import logging
import os
import re
import secrets
import string
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai
from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_BASE_URLS: dict[str, str] = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

LEGACY_DEFAULT_MODELS: dict[str, str] = {
    "openrouter": "nvidia/nemotron-3-ultra-550b-a55b:free",
}

DEFAULT_HEADERS: dict[str, dict[str, str]] = {
    "openrouter": {
        "HTTP-Referer": "https://github.com/netauto-project",
        "X-Title": "NetAuto AgenticOps v2",
    },
}

TRANSIENT_STATUS_CODES = frozenset({402, 429, 500, 502, 503, 504})

FATAL_STATUS_HINTS: dict[int, str] = {
    400: "bad request - probably a bug in the request (messages/tools format) "
         "or the model does not support tool calling",
    401: "authentication failed - check {prefix}_API_KEY",
    403: "access denied - the API key is not allowed to use this model",
    404: "not found - check {prefix}_MODEL and {prefix}_BASE_URL",
}

DEFAULT_CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / "credentials.json"

REDACTED = "<REDACTED>"


class ProviderConfigError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    def __init__(self, provider: str, model: str, status_code: int | None, detail: str) -> None:
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"[{provider}/{model}] HTTP {status_code}: {detail}")


class AllProvidersUnavailable(RuntimeError):
    def __init__(
        self,
        errors: Mapping[str, str],
        next_available_at: float | None,
        next_provider: str | None,
    ) -> None:
        self.errors = dict(errors)
        self.next_available_at = next_available_at
        self.next_provider = next_provider
        super().__init__(self._format())

    def _format(self) -> str:
        lines = ["All LLM providers are unavailable:"]
        lines += [f"  - {name}: {error}" for name, error in self.errors.items()]
        if self.next_available_at is not None and self.next_provider:
            wait = max(0, int(self.next_available_at - time.time()))
            at = time.strftime("%H:%M:%S", time.localtime(self.next_available_at))
            lines.append(f"Next available: {self.next_provider} at {at} (in ~{wait}s)")
        return "\n".join(lines)


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    cooldown_until: float = 0.0
    last_error: str | None = None
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=0,
            )

    @property
    def label(self) -> str:
        return f"{self.name}/{self.model}"

    def is_available(self, now: float) -> bool:
        return now >= self.cooldown_until


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    error: str | None = None


@dataclass
class ChatResult:
    message: dict[str, Any]
    tool_calls: list[ToolCall]
    provider: str
    model: str
    finish_reason: str | None = None



def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ProviderConfigError(f"{name} must be a number, got {raw!r}") from e


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_providers_from_env(env: Mapping[str, str] | None = None) -> list[Provider]:
    env = os.environ if env is None else env
    names: list[str] = []
    for raw_name in env.get("LLM_PROVIDERS", "openrouter").split(","):
        name = raw_name.strip().lower()
        if name and name not in names:
            names.append(name)

    timeout = _env_float(env, "LLM_TIMEOUT", 60.0)
    providers: list[Provider] = []
    for name in names:
        prefix = re.sub(r"[^A-Z0-9]", "_", name.upper())
        api_key = env.get(f"{prefix}_API_KEY", "").strip()
        if not api_key:
            logger.warning("LLM provider '%s' skipped: %s_API_KEY is not set", name, prefix)
            continue
        base_url = env.get(f"{prefix}_BASE_URL", "").strip() or DEFAULT_BASE_URLS.get(name)
        if not base_url:
            logger.warning("LLM provider '%s' skipped: %s_BASE_URL is not set", name, prefix)
            continue
        model = env.get(f"{prefix}_MODEL", "").strip() or LEGACY_DEFAULT_MODELS.get(name)
        if not model:
            logger.warning("LLM provider '%s' skipped: %s_MODEL is not set", name, prefix)
            continue
        providers.append(Provider(
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            extra_headers=dict(DEFAULT_HEADERS.get(name, {})),
        ))
    return providers



def _retry_after_seconds(headers: Mapping[str, str] | None, now: float) -> float | None:
    if not headers:
        return None
    raw_ms = headers.get("retry-after-ms")
    if raw_ms:
        try:
            return max(0.0, float(raw_ms) / 1000)
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
                return max(0.0, parsed.timestamp() - now)
            except (TypeError, ValueError):
                pass
    raw_reset = headers.get("x-ratelimit-reset")
    if raw_reset:
        try:
            value = float(raw_reset)
        except ValueError:
            return None
        if value > 1e12:
            return max(0.0, value / 1000 - now)
        if value > 1e9:
            return max(0.0, value - now)
        return max(0.0, value)
    return None


class LLMRouter:
    def __init__(
        self,
        providers: Iterable[Provider],
        *,
        default_cooldown: float = 60.0,
        network_cooldown: float = 30.0,
        max_tokens: int = 4096,
        redact_secrets: bool = True,
        credentials_file: Path | None = DEFAULT_CREDENTIALS_FILE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.providers = list(providers)
        if not self.providers:
            raise ProviderConfigError("No LLM provider configured")
        self.default_cooldown = default_cooldown
        self.network_cooldown = network_cooldown
        self.max_tokens = max_tokens
        self.redact_secrets = redact_secrets
        self.credentials_file = credentials_file
        self._clock = clock

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMRouter:
        env = os.environ if env is None else env
        providers = load_providers_from_env(env)
        if not providers:
            raise ProviderConfigError(
                "No LLM provider configured. Set LLM_PROVIDERS and at least one "
                "<NAME>_API_KEY (e.g. OPENROUTER_API_KEY) in the environment or in .env"
            )
        return cls(
            providers,
            default_cooldown=_env_float(env, "LLM_COOLDOWN_SECONDS", 60.0),
            network_cooldown=_env_float(env, "LLM_NETWORK_COOLDOWN_SECONDS", 30.0),
            max_tokens=int(_env_float(env, "LLM_MAX_TOKENS", 4096)),
            redact_secrets=_env_bool(env, "LLM_REDACT_SECRETS", True),
        )

    def describe(self) -> str:
        return " -> ".join(p.label for p in self.providers)

    def status(self) -> list[str]:
        now = self._clock()
        lines = []
        for p in self.providers:
            if p.is_available(now):
                lines.append(f"{p.label}: available")
            else:
                lines.append(f"{p.label}: cooldown {int(p.cooldown_until - now)}s ({p.last_error})")
        return lines

    def _safe_error(self, provider: Provider, error: Exception) -> str:
        text = str(error).replace(provider.api_key, "***")
        text = redact(text)
        return text if len(text) <= 300 else text[:300] + "..."

    def _cool_down(self, provider: Provider, seconds: float, reason: str) -> None:
        provider.cooldown_until = self._clock() + seconds
        provider.last_error = reason
        logger.warning("LLM provider %s failed (%s); cooling down for %.0fs",
                       provider.label, reason, seconds)

    def prepare_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        prepared = [_message_to_dict(m) for m in messages]
        if self.redact_secrets:
            known = load_credential_secrets(self.credentials_file)
            prepared = [redact_message(m, known) for m in prepared]
        return prepared

    def chat(self, messages: list[Any], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        payload = self.prepare_messages(messages)
        errors: dict[str, str] = {}

        for provider in self.providers:
            now = self._clock()
            if not provider.is_available(now):
                errors[provider.name] = (
                    f"cooling down for {int(provider.cooldown_until - now)}s "
                    f"(last error: {provider.last_error})"
                )
                continue

            kwargs: dict[str, Any] = {
                "model": provider.model,
                "messages": payload,
                "max_tokens": self.max_tokens,
            }
            if tools:
                kwargs["tools"] = tools
            if provider.extra_headers:
                kwargs["extra_headers"] = provider.extra_headers

            try:
                response = provider.client.chat.completions.create(**kwargs)
            except openai.APIStatusError as e:
                status = e.status_code
                detail = self._safe_error(provider, e)
                if status in TRANSIENT_STATUS_CODES or status >= 500:
                    wait = None
                    if status in (429, 503):
                        wait = _retry_after_seconds(e.response.headers, now)
                    reason = f"HTTP {status}"
                    self._cool_down(provider, wait if wait is not None else self.default_cooldown, reason)
                    errors[provider.name] = f"{reason}: {detail}"
                    continue
                hint = FATAL_STATUS_HINTS.get(status, "client error").format(
                    prefix=re.sub(r"[^A-Z0-9]", "_", provider.name.upper()))
                logger.error("LLM provider %s returned HTTP %s (%s), not falling back",
                             provider.label, status, hint)
                raise ProviderRequestError(provider.name, provider.model, status,
                                           f"{hint}. {detail}") from None
            except openai.APIConnectionError as e:
                reason = "timeout" if isinstance(e, openai.APITimeoutError) else "connection error"
                self._cool_down(provider, self.network_cooldown, reason)
                errors[provider.name] = reason
                continue

            choices = getattr(response, "choices", None)
            if not choices:
                reason = "empty response (no choices)"
                self._cool_down(provider, self.default_cooldown, reason)
                errors[provider.name] = reason
                continue

            choice = choices[0]
            message, tool_calls = normalize_message(getattr(choice, "message", None) or {})
            provider.last_error = None
            logger.info("LLM response from %s", provider.label)
            return ChatResult(
                message=message,
                tool_calls=tool_calls,
                provider=provider.name,
                model=provider.model,
                finish_reason=getattr(choice, "finish_reason", None),
            )

        upcoming = [p for p in self.providers if p.cooldown_until > self._clock()]
        nxt = min(upcoming, key=lambda p: p.cooldown_until) if upcoming else None
        raise AllProvidersUnavailable(
            errors,
            nxt.cooldown_until if nxt else None,
            nxt.name if nxt else None,
        )



_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9]{9}$")
_ID_ALPHABET = string.ascii_letters + string.digits
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return copy.deepcopy(message)
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return dict(message)


def normalize_tool_call_id(raw_id: str | None, used: set[str] | None = None) -> str:
    used = used if used is not None else set()
    if raw_id and _TOOL_ID_RE.match(raw_id):
        candidate = raw_id
    elif raw_id:
        digest = int(hashlib.sha256(raw_id.encode()).hexdigest(), 16)
        chars = []
        for _ in range(9):
            digest, idx = divmod(digest, len(_ID_ALPHABET))
            chars.append(_ID_ALPHABET[idx])
        candidate = "".join(chars)
    else:
        candidate = ""
    while not candidate or candidate in used:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(9))
    used.add(candidate)
    return candidate


def repair_json(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if not candidate:
        return {}
    fence = _FENCE_RE.match(candidate)
    if fence:
        candidate = fence.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        candidate = candidate[start:end + 1]

    attempts = [candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)]
    for attempt in attempts:
        try:
            value = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    for attempt in attempts:
        try:
            value = ast.literal_eval(attempt)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        return value if isinstance(value, dict) else None
    return None


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, f"unexpected arguments type {type(raw).__name__}"
    try:
        value = json.loads(raw) if raw.strip() else {}
        if isinstance(value, dict):
            return value, None
    except json.JSONDecodeError:
        pass
    repaired = repair_json(raw)
    if repaired is not None:
        logger.info("Repaired malformed tool call arguments")
        return repaired, None
    return None, "arguments are not valid JSON"


def normalize_message(raw: Any) -> tuple[dict[str, Any], list[ToolCall]]:
    data = _message_to_dict(raw) if raw else {}
    content = data.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    content = _THINK_RE.sub("", content)
    if "</think>" in content:
        content = content.split("</think>")[-1]
    content = content.strip()

    message: dict[str, Any] = {"role": "assistant", "content": content}
    tool_calls: list[ToolCall] = []
    history_calls: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for tc in data.get("tool_calls") or []:
        tc = _message_to_dict(tc)
        function = tc.get("function") or {}
        name = function.get("name") or ""
        arguments, error = parse_tool_arguments(function.get("arguments"))
        call_id = normalize_tool_call_id(tc.get("id"), used_ids)
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments, error=error))
        history_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments if arguments is not None else {},
                                        ensure_ascii=False),
            },
        })

    if history_calls:
        message["tool_calls"] = history_calls
    return message, tool_calls



_VALUE = r"(?:[^\s\"]+?)(?=\s|\\n|\\r|\"|$)"
_TYPE = r"(?:\d{1,2}\s+)?"
_LINE_START = r"(?:^|(?<=\n)|(?<=\\n)|(?<=\"))"

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"(\benable\s+(?:secret|password)\s+(?:level\s+\d+\s+)?{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"(\busername\s+\S+(?:\s+privilege\s+\d+)?\s+(?:secret|password)\s+{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"({_LINE_START}[ \t]*(?:password|secret)\s+{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"(\bsnmp-server\s+community\s+)({_VALUE})", re.MULTILINE),
    re.compile(rf"(\bkey-string\s+{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"(\b(?:tacacs|radius)-server\b[^\n\"]*?\bkey\s+{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"({_LINE_START}[ \t]+key\s+\d\s+)({_VALUE})", re.MULTILINE),
    re.compile(rf"(\bcrypto\s+isakmp\s+key\s+{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(rf"(\bpre-shared-key\s+(?:local\s+|remote\s+)?{_TYPE})({_VALUE})", re.MULTILINE),
    re.compile(r"(\"(?:password|secret|api_key|key)\"\s*:\s*\")((?:[^\"\\]|\\.)*)(?=\")", re.IGNORECASE),
]

_HASH_RE = re.compile(r"\$(?:1|5|6|8|9|14)\$[^\s\"\\]+")

_MIN_KNOWN_SECRET_LEN = 4
_credentials_cache: dict[Path, tuple[float, frozenset[str]]] = {}


def load_credential_secrets(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset()
    cached = _credentials_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    values: set[str] = set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Could not read credentials file for redaction")
        data = {}
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                for key in ("password", "secret"):
                    value = entry.get(key)
                    if isinstance(value, str) and len(value) >= _MIN_KNOWN_SECRET_LEN:
                        values.add(value)
    result = frozenset(values)
    _credentials_cache[path] = (mtime, result)
    return result


def redact(text: str, known_secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HASH_RE.sub(REDACTED, text)
    for value in sorted(set(known_secrets), key=len, reverse=True):
        if len(value) < _MIN_KNOWN_SECRET_LEN:
            continue
        text = re.sub(rf"(?<![\w]){re.escape(value)}(?![\w])", REDACTED, text)
    return text


def redact_message(message: dict[str, Any], known_secrets: Iterable[str] = ()) -> dict[str, Any]:
    if message.get("role") == "user":
        return message
    known = tuple(known_secrets)
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = redact(content, known)
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") if isinstance(tc, dict) else None
        if function and isinstance(function.get("arguments"), str):
            function["arguments"] = redact(function["arguments"], known)
    return message
