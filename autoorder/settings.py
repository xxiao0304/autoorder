from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    path: Path

    @property
    def base_url(self) -> str:
        return str(self.raw.get("base_url", "https://gym.sztu.edu.cn/"))

    @property
    def storage_state(self) -> Path:
        return project_path(str(self.raw.get("storage_state", "state/storage.json")))

    @property
    def headless(self) -> bool:
        return bool(self.raw.get("headless", False))

    @property
    def slow_mo_ms(self) -> int:
        return int(self.raw.get("slow_mo_ms", 50))

    @property
    def screenshots_dir(self) -> Path:
        return project_path(str(self.raw.get("screenshots_dir", "screenshots")))

    @property
    def booking(self) -> dict[str, Any]:
        return dict(self.raw.get("booking", {}))

    @property
    def cancel(self) -> dict[str, Any]:
        return dict(self.raw.get("cancel", {}))


def load_settings(config_path: str | None = None) -> Settings:
    path = project_path(config_path or "config.json")
    if not path.exists():
        example = ROOT / "config.example.json"
        raise FileNotFoundError(
            f"Missing {path}. Copy {example.name} to config.json and adjust it first."
        )
    return Settings(json.loads(path.read_text(encoding="utf-8")), path)


def with_account_profile(settings: Settings, profile_name: str = "") -> Settings:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return settings

    accounts = settings.raw.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise KeyError(f"Account profile not found: {profile_name}")
    account = accounts.get(profile_name) or {}
    if not isinstance(account, dict):
        raise KeyError(f"Account profile not found: {profile_name}")

    raw = deepcopy(settings.raw)
    login = dict(raw.get("login", {}))
    if str(account.get("username", "")).strip():
        login["username"] = str(account.get("username", "")).strip()
    if str(account.get("password", "")):
        login["password"] = str(account.get("password", ""))
    raw["login"] = login
    if str(account.get("storage_state", "")).strip():
        raw["storage_state"] = str(account.get("storage_state", "")).strip()
    return Settings(raw=raw, path=settings.path)
