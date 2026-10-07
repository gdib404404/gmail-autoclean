"""Loads config.yaml plus secrets from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    timezone: str = "America/Chicago"
    digest_hours: list[int] = field(default_factory=lambda: [9, 14, 20])
    send_empty_digest: bool = False
    model: str = "claude-haiku-4-5"
    max_emails_per_run: int = 150
    body_chars: int = 1500
    labels: dict = field(default_factory=dict)
    star_priority: bool = True
    archive_low: bool = False
    archive_junk: bool = False
    about_me: str = ""
    auto_clean: bool = False
    auto_clean_min_certainty: int = 90
    sweep_max_body_checks: int = 4000
    dormant_years: float = 3
    never_auto_delete: list[str] = field(default_factory=list)

    # secrets / env
    sheet_id: str = ""
    anthropic_api_key: str = ""

    def label(self, key: str) -> str:
        defaults = {
            "priority": "!Priority",
            "worth_a_look": "Digest/Worth a look",
            "low": "Digest/Low",
            "junk": "Digest/Probably junk",
            "seen": "Digest/Seen",
        }
        return self.labels.get(key) or defaults[key]


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    cfg = Config(**known)
    cfg.sheet_id = os.environ.get("SHEET_ID", "").strip()
    cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return cfg
