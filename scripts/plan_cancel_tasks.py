from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoorder.auth import ensure_logged_in
from autoorder.browser import EdgeSession
from autoorder.settings import load_settings, with_account_profile
from autoorder.sztu_http import SztuHttpClient, result_unauthorized


TASK_PREFIX = "SZTU Smart Cancel Order "


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def ensure_auth(settings) -> SztuHttpClient:
    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.5)
    probe = client.probe_auth()
    if result_unauthorized(probe):
        with EdgeSession(settings, force_headless=True) as context:
            page = context.new_page()
            ensure_logged_in(page, settings, save_storage_state=True)
        client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.5)
    return client


def cancel_now(client: SztuHttpClient, order_no: str) -> bool:
    result = client.cancel_order(order_no)
    payload = result.get("payload") if isinstance(result, dict) else {}
    return isinstance(payload, dict) and int(payload.get("status", 0) or 0) == 1


def parse_order_start(site_date: str, start_time: str) -> datetime | None:
    t = start_time.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{site_date} {t}", fmt)
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Hourly planner: scan today's orders and schedule exact cancel tasks.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--grace-minutes", type=int, default=61)
    args = parser.parse_args()

    base_settings = load_settings(args.config)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    python_exe = sys.executable

    planned = 0
    skipped = 0
    cancelled_now = 0
    lines: list[str] = []
    automation = base_settings.raw.get("automation", {})
    account_profiles = [""]
    if isinstance(automation, dict):
        extra_profiles = automation.get("cancel_account_profiles", [])
        if isinstance(extra_profiles, list):
            account_profiles.extend(str(x).strip() for x in extra_profiles if str(x).strip())
    seen_profiles: set[str] = set()

    for account_profile in account_profiles:
        if account_profile in seen_profiles:
            continue
        seen_profiles.add(account_profile)
        settings = with_account_profile(base_settings, account_profile)
        client = ensure_auth(settings)
        account_suffix = f" {account_profile}" if account_profile else ""

        for status in (1, 2, 3):
            result = client.list_orders(status)
            payload = result.get("payload") if isinstance(result, dict) else {}
            rows = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                order_no = str(item.get("orderNo", "")).strip()
                site_date = str(item.get("siteDate", "")).strip()
                start_time = str(item.get("startTime", "")).strip()
                if not order_no or not site_date or not start_time:
                    continue
                if site_date != today:
                    continue
                start_dt = parse_order_start(site_date, start_time)
                if not start_dt:
                    continue
                run_at = start_dt + timedelta(minutes=max(0, args.grace_minutes))
                if run_at <= now:
                    if cancel_now(client, order_no):
                        cancelled_now += 1
                    else:
                        skipped += 1
                        lines.append(f"cancel_due_failed:{order_no}")
                    continue

                task_name = f"{TASK_PREFIX}{order_no}{account_suffix}"
                at_text = run_at.strftime("%H:%M")
                account_arg = f" --account-profile {account_profile}" if account_profile else ""
                cmd = (
                    f"schtasks /Create /F /SC ONCE /TN \"{task_name}\" "
                    f"/TR \"\\\"{python_exe}\\\" .\\scripts\\cancel_order_api.py --config {args.config}{account_arg} --order-no {order_no}\" "
                    f"/ST {at_text}"
                )
                cp = run_ps(cmd)
                if cp.returncode == 0:
                    planned += 1
                else:
                    # If the one-time task is too close for schtasks, wait until due and cancel now.
                    if run_at <= datetime.now() + timedelta(minutes=1):
                        wait_seconds = max(0.0, (run_at - datetime.now()).total_seconds())
                        if wait_seconds:
                            time.sleep(min(wait_seconds, 65.0))
                        if cancel_now(client, order_no):
                            cancelled_now += 1
                            continue
                    lines.append(f"failed:{order_no}:{cp.stderr.strip()[:120]}")

    print(
        {
            "planned": planned,
            "cancelled_now": cancelled_now,
            "skipped_due_or_past": skipped,
            "errors": lines[:5],
            "planner_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
