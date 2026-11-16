"""Load and validate skill-bridge manifests (YAML or JSON)."""

from __future__ import annotations

import json
import keyword
import math
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def validate_input_field_name(name: str) -> None:
    """Reject manifest input keys that ``inspect.Parameter`` or ``create_model`` cannot use."""
    if (
        not isinstance(name, str)
        or not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("_")
        or name.startswith("model_")
    ):
        raise ValueError(f"input field {name!r} is not a valid parameter name")


def _positive_finite_seconds(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError("timeout/interval must be a positive finite number of seconds")
    return float(v)


class FieldSpec(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["string", "integer", "number", "boolean", "object", "array"] = "string"
    required: bool = False
    description: str | None = None
    items: dict[str, Any] | None = None  # for array


class ToolSpec(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    # http_rpc: value for dispatch_field (e.g. api_name)
    dispatch: str | None = None
    # json_cli: operation type for run wrapper, or raw cli subcommand
    operation: str | None = None
    # Single token (`search`) or argv prefix (`["folder", "list"]`) before flags.
    cli: str | list[str] | None = None
    input: dict[str, FieldSpec] = Field(default_factory=dict)

    @field_validator("input")
    @classmethod
    def _input_field_names(cls, v: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        for name in v:
            validate_input_field_name(name)
        return v


class SkillAuthActions(BaseModel):
    """CLI subcommands for platform-owned Connect login (not agent MCP)."""

    model_config = {"extra": "forbid"}

    login: str = "login"
    status: str = "login-status"
    logout: str = "logout"
    relogin: str = "relogin"
    refresh: str = "refresh-login"


# Back-compat alias for imports/tests.
DeviceQrActions = SkillAuthActions


class AuthSpec(BaseModel):
    model_config = {"extra": "forbid"}

    # device_qr: CLI returns QR path; Connect shows QR and polls status.
    # oauth_link: CLI returns auth URL in msg; Connect shows link + paste code.
    # external_session: deprecated alias of device_qr.
    type: Literal[
        "bearer_env", "header_env", "device_qr", "oauth_link", "external_session", "none"
    ] = "none"
    env: str | None = None
    header: str = "Authorization"
    prefix: str = "Bearer "

    @field_validator("header")
    @classmethod
    def _auth_header(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("auth.header must be a non-empty header name")
        if "\n" in v or "\r" in v or ":" in v:
            raise ValueError(f"invalid header name {v!r}")
        return v.strip()

    # Injected only for skill_exec auth (e.g. LOGIN_MODE=device, OPENCLAW_CLI=1).
    login_env: dict[str, str] = Field(default_factory=dict)
    actions: SkillAuthActions = Field(default_factory=SkillAuthActions)
    # device_qr: key under result.data for QR PNG path.
    qr_path_field: str = "qr_code_path"
    # oauth_link: CLI flag for pasted authorization code (login --token …).
    token_flag: str = "--token"
    # oauth_link / numeric CLI codes: result.code == success_code ⇒ logged in.
    success_code: int = 0
    pending_codes: list[int] = Field(default_factory=lambda: [-114])
    # oauth_link: shim xdg-open, return auth_url+job_id, keep CLI polling page_code.
    browser_capture: bool = False
    # oauth_link + browser_capture: how long the runner keeps the login process alive.
    capture_timeout_s: float = 90.0
    # Connect UI: seconds between status polls while waiting for scan/confirm.
    poll_interval_s: float = 2.0
    # oauth_link: show paste-code fallback (login --token) in Connect.
    token_paste: bool = True
    # Optional Connect copy under QR / authorize link (vendor-specific instructions).
    connect_hint: str | None = None
    # Additional HTTP headers from vault env (header name → env var), e.g. IMA Client ID.
    extra_headers_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("login_env")
    @classmethod
    def _login_env_str(cls, v: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, val in v.items():
            if not isinstance(k, str) or not k.strip() or not isinstance(val, str):
                raise ValueError("login_env must map non-empty strings to strings")
            out[k.strip()] = val
        return out

    @field_validator("extra_headers_env")
    @classmethod
    def _extra_headers_env(cls, v: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for header, env_name in v.items():
            if not isinstance(header, str) or not header.strip():
                raise ValueError("extra_headers_env keys must be non-empty header names")
            if not isinstance(env_name, str) or not env_name.strip():
                raise ValueError("extra_headers_env values must be non-empty env names")
            if "\n" in header or "\r" in header or ":" in header:
                raise ValueError(f"invalid header name {header!r}")
            out[header.strip()] = env_name.strip()
        if len(out) > 16:
            raise ValueError("extra_headers_env supports at most 16 headers")
        return out

    @field_validator("qr_path_field")
    @classmethod
    def _qr_field(cls, v: str) -> str:
        name = (v or "").strip()
        if not name or "/" in name or name.startswith("."):
            raise ValueError("qr_path_field must be a simple result.data key")
        return name

    @field_validator("token_flag")
    @classmethod
    def _token_flag(cls, v: str) -> str:
        flag = (v or "").strip()
        if not flag.startswith("-"):
            raise ValueError("token_flag must be a CLI flag like --token")
        return flag

    @field_validator("capture_timeout_s", "poll_interval_s")
    @classmethod
    def _positive_seconds(cls, v: float) -> float:
        return _positive_finite_seconds(v)

    @field_validator("connect_hint")
    @classmethod
    def _connect_hint(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        if not text:
            return None
        if len(text) > 500:
            raise ValueError("connect_hint must be at most 500 characters")
        return text

    def is_device_qr(self) -> bool:
        return self.type in ("device_qr", "external_session")

    def is_oauth_link(self) -> bool:
        return self.type == "oauth_link"

    def is_platform_auth(self) -> bool:
        return self.is_device_qr() or self.is_oauth_link()

    def wants_browser_capture(self) -> bool:
        return self.is_oauth_link() and self.browser_capture


class HttpSpec(BaseModel):
    model_config = {"extra": "forbid"}

    base_url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("base_url must be a non-empty absolute http(s) URL")
        url = v.strip()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be a non-empty absolute http(s) URL")
        return url

    const: dict[str, Any] = Field(default_factory=dict)
    # Fixed request headers (non-secret), e.g. ima-openapi-ctx.
    headers: dict[str, str] = Field(default_factory=dict)
    # body_field: WeRead-style — put tool.dispatch into JSON under dispatch_field.
    # url_path: IMA-style — POST to base_url + "/" + tool.dispatch with arguments as body.
    dispatch_mode: Literal["body_field", "url_path"] = "body_field"
    dispatch_field: str = "api_name"
    timeout_s: float = 60.0

    @field_validator("timeout_s")
    @classmethod
    def _timeout_s(cls, v: float) -> float:
        return _positive_finite_seconds(v)

    @field_validator("headers")
    @classmethod
    def _http_headers(cls, v: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, val in v.items():
            if not isinstance(k, str) or not k.strip() or not isinstance(val, str):
                raise ValueError("http.headers must map strings to strings")
            if "\n" in k or "\r" in k or ":" in k:
                raise ValueError(f"invalid header name {k!r}")
            if any(c in val for c in "\r\n\x00"):
                raise ValueError(f"invalid header value for {k!r}")
            out[k.strip()] = val
        if len(out) > 32:
            raise ValueError("http.headers supports at most 32 entries")
        return out


class CliIdentity(BaseModel):
    model_config = {"extra": "forbid"}

    # Omit unset flags — Quark-style CLIs reject unknown --agent-name options.
    agent_name: str | None = None
    agent_profile_dir: str | None = None
    agent_instance_id: str | None = None


class CliSpec(BaseModel):
    model_config = {"extra": "forbid"}

    command: list[str] = Field(min_length=1)
    cwd: str | None = None
    identity: CliIdentity = Field(default_factory=CliIdentity)
    # "run_operation" wraps one MCP tool call as CLI `run` with a single operation
    run_wrapper: Literal["run_operation", "raw"] = "run_operation"


class Manifest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=128)
    version: str = "0.0.0"
    backend: Literal["http_rpc", "json_cli"]
    auth: AuthSpec = Field(default_factory=AuthSpec)
    http: HttpSpec | None = None
    cli: CliSpec | None = None
    tools: list[ToolSpec] = Field(min_length=1)

    @field_validator("tools")
    @classmethod
    def _unique_tool_names(cls, tools: list[ToolSpec]) -> list[ToolSpec]:
        names = [t.name for t in tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return tools

    @model_validator(mode="after")
    def _backend_shape(self) -> Manifest:
        if self.backend == "http_rpc":
            if self.http is None:
                raise ValueError("http_rpc backend requires http:")
            for t in self.tools:
                if not t.dispatch:
                    raise ValueError(f"tool {t.name!r} needs dispatch for http_rpc")
        if self.backend == "json_cli" and self.cli is None:
            raise ValueError("json_cli backend requires cli:")
        if self.auth.is_platform_auth() and self.backend != "json_cli":
            raise ValueError(f"{self.auth.type} auth requires json_cli backend")
        if self.auth.type in ("bearer_env", "header_env") and not self.auth.env:
            raise ValueError(f"auth.type={self.auth.type} requires auth.env")
        return self


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path).expanduser().resolve()
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(raw)
    elif p.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        # Try YAML first, then JSON.
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a mapping")
    return Manifest.model_validate(data)
