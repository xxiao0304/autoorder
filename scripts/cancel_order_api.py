from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoorder.auth import ensure_logged_in
from autoorder.browser import EdgeSession
from autoorder.notify import send_notification
from autoorder.settings import load_settings, with_account_profile
from autoorder.sztu_http import SztuHttpClient, result_unauthorized


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast API-only cancellation by order number.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--order-no", required=True)
    parser.add_argument("--account-profile", default="", help="Use a named account profile from config.accounts.")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    settings = with_account_profile(load_settings(args.config), args.account_profile)
    started = time.perf_counter()
    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.5)

    probe = client.probe_auth()
    if result_unauthorized(probe):
        with EdgeSession(settings, force_headless=True) as context:
            page = context.new_page()
            ensure_logged_in(page, settings, save_storage_state=True)
        client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.5)

    result = client.cancel_order(args.order_no)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = result.get("payload") if isinstance(result, dict) else {}
    ok = isinstance(payload, dict) and int(payload.get("status", 0) or 0) == 1

    if args.notify:
        send_notification(
            settings,
            title=f"SZTU 快速取消{'成功' if ok else '失败'}",
            lines=[
                f"time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"order_no={args.order_no}",
                f"elapsed_ms={elapsed_ms}",
                f"payload={payload}",
            ],
        )

    print({"ok": ok, "elapsed_ms": elapsed_ms, "result": result})
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
