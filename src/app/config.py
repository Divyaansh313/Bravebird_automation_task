"""Settings from the environment and per-run inputs from a JSON file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

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


class RunInput(BaseModel):
    """Everything that varies between runs. Changing these needs no code change."""

    model_config = ConfigDict(extra="forbid")

    portal: str = "github"
    base_url: str = "https://github.com"
    repo: str = Field(description="owner/name of the repository to work in")
    form: FormInput
    document: DocumentInput
    headless: bool = True
    state_dir: Path = Path("state")
    output_dir: Path = Path("runs")

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
