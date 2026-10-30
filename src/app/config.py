"""Settings from the environment and per-run inputs from a JSON file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .redact import redactor


class Credentials(BaseModel):
    username: SecretStr
    password: SecretStr
    totp_secret: SecretStr


class ConfigError(Exception):
    """Missing or invalid configuration. Never retried."""


def load_credentials(env_file: str | Path = ".env") -> Credentials:
    load_dotenv(env_file)
    missing = [k for k in ("PORTAL_USERNAME", "PORTAL_PASSWORD", "TOTP_SECRET") if not os.getenv(k)]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    creds = Credentials(
        username=os.environ["PORTAL_USERNAME"].strip(),
        password=os.environ["PORTAL_PASSWORD"],
        totp_secret=os.environ["TOTP_SECRET"].replace(" ", "").strip(),
    )
    redactor.add(
        creds.username.get_secret_value(),
        creds.password.get_secret_value(),
        creds.totp_secret.get_secret_value(),
    )
    return creds


class FormInput(BaseModel):
    """The "Draft a new release" form."""

    model_config = ConfigDict(extra="forbid")

    tag: str = Field(description="Tag for the draft. A new tag makes the Target field appear")
    target: str | None = Field(
        default=None, description="Branch for a new tag (the dependent field)"
    )
    title: str
    notes: str = ""
    label: Literal["None", "Pre-release", "Latest"] = "Pre-release"
    keep_draft: bool = Field(default=False, description="Leave the draft instead of deleting it")


class DocumentInput(BaseModel):
    """Which release asset to capture."""

    model_config = ConfigDict(extra="forbid")

    release_tag: str
    asset_name: str


class ProxyConfig(BaseModel):
    """Where this run's browser traffic goes. Credentials are redacted."""

    model_config = ConfigDict(extra="forbid")

    server: str = Field(description="e.g. http://proxy.example:8080 or socks5://host:1080")
    username: str | None = None
    password: SecretStr | None = None
    bypass: str | None = Field(default=None, description="Comma-separated hosts to skip")

    def playwright(self) -> dict:
        out: dict = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password.get_secret_value()
        if self.bypass:
            out["bypass"] = self.bypass
        return out

    @classmethod
    def from_url(cls, url: str) -> ProxyConfig:
        """http://user:pass@host:port -> ProxyConfig (credentials split out)."""
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            raise ConfigError("Proxy URL must look like scheme://[user:pass@]host:port")
        server = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        return cls(server=server, username=unquote(parts.username) if parts.username else None,
                   password=unquote(parts.password) if parts.password else None)


class RunInput(BaseModel):
    """Everything that varies between runs. Changing these needs no code change."""

    model_config = ConfigDict(extra="forbid")

    portal: Literal["github"] = "github"  # also names the state file, so never free text
    # The run types the account's password and a 2FA code into {base_url}/login,
    # so it must be an https origin (no path, no plain http).
    base_url: str = Field(default="https://github.com",
                          pattern=r"^https://[A-Za-z0-9.-]+(:\d{1,5})?/?$")
    repo: str = Field(description="owner/name of the repository to work in",
                      pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
    form: FormInput
    document: DocumentInput
    headless: bool = True
    state_dir: Path = Path("state")
    output_dir: Path = Path("runs")
    proxy: ProxyConfig | None = Field(
        default=None, description="Per-run proxy (CLI only if it has a password)")
    proxy_route: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_]{1,32}$",
        description="Named route: uses env BRAVEBIRD_PROXY_<ROUTE>, keeping credentials "
                    "out of input files")
    fingerprint: Literal["hardened", "default"] = "hardened"
    timezone: str | None = Field(default=None, description="IANA zone, e.g. Asia/Kolkata; "
                                 "match it to the proxy's exit region")
    captcha_wait_seconds: int = Field(
        default=0, ge=0, le=900,
        description="Headed runs only: wait this long for a human to solve a CAPTCHA")

    def effective_proxy(self) -> ProxyConfig | None:
        """Routing order: explicit proxy > named route > BRAVEBIRD_PROXY > direct."""
        if self.proxy:
            return self.proxy
        if self.proxy_route:
            var = f"BRAVEBIRD_PROXY_{self.proxy_route.upper()}"
            url = os.getenv(var, "").strip()
            if not url:
                raise ConfigError(f"proxy_route {self.proxy_route!r} needs env var {var}")
            return ProxyConfig.from_url(url)
        env = os.getenv("BRAVEBIRD_PROXY", "").strip()
        return ProxyConfig.from_url(env) if env else None

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"{self.portal}.storage.json"

    @property
    def repo_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.repo}"


def load_input(path: str | Path) -> RunInput:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read input file {path}: {exc}") from exc
    try:
        return RunInput.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid input file {path}:\n{exc}") from exc
