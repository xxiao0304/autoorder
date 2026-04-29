from __future__ import annotations

import json
from typing import Any
from urllib import parse, request

from autoorder.settings import Settings


def _notify_config(settings: Settings) -> dict[str, Any]:
    return dict(settings.raw.get("notify", {}))


def is_notify_enabled(settings: Settings) -> bool:
    cfg = _notify_config(settings)
    return bool(cfg.get("enabled", False))


def _timeout_seconds(cfg: dict[str, Any]) -> float:
    try:
        return float(cfg.get("timeout_seconds", 8))
    except Exception:
        return 8.0


def _post_form(url: str, data: dict[str, str], timeout: float) -> tuple[bool, str]:
    body = parse.urlencode(data).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="ignore")
        return True, payload[:300]
    except Exception as exc:
        return False, str(exc)


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            result = resp.read().decode("utf-8", errors="ignore")
        return True, result[:300]
    except Exception as exc:
        return False, str(exc)


def send_notification(settings: Settings, *, title: str, lines: list[str]) -> tuple[bool, str]:
    cfg = _notify_config(settings)
    if not bool(cfg.get("enabled", False)):
        return False, "notify.disabled"

    provider = str(cfg.get("provider", "serverchan")).strip().lower()
    timeout = _timeout_seconds(cfg)
    text = "\n".join([line for line in lines if line]).strip()
    if not text:
        text = title

    if provider in {"serverchan", "server-chan", "sct"}:
        sendkey = str(cfg.get("serverchan_sendkey", "")).strip()
        if not sendkey:
            return False, "notify.serverchan_sendkey_missing"
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        return _post_form(url, {"title": title, "desp": text}, timeout)

    if provider in {"wechat_webhook", "wechat-work", "wecom"}:
        webhook_url = str(cfg.get("wechat_webhook_url", "")).strip()
        if not webhook_url:
            return False, "notify.wechat_webhook_url_missing"
        payload: dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": f"{title}\n{text}"},
        }
        if bool(cfg.get("mention_all", False)):
            payload["text"]["mentioned_list"] = ["@all"]
        return _post_json(webhook_url, payload, timeout)

    return False, f"notify.unsupported_provider:{provider}"

