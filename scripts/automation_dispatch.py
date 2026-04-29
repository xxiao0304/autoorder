from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoorder.settings import load_settings


def run_cmd(args: list[str]) -> int:
    print({"run": args})
    completed = subprocess.run(args, cwd=str(ROOT))
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified automation entry for booking and cancellation.")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="action", required=True)

    p_book = sub.add_parser("book-profile", help="Run booking profile from config.automation.booking_profiles.")
    p_book.add_argument("--profile", required=True)

    args = parser.parse_args()
    settings = load_settings(args.config)
    automation = dict(settings.raw.get("automation", {}))

    if args.action == "book-profile":
        profiles = automation.get("booking_profiles") or {}
        if not isinstance(profiles, dict):
            raise SystemExit("config automation.booking_profiles must be an object.")
        profile = profiles.get(args.profile) or {}
        if not isinstance(profile, dict):
            raise SystemExit(f"Profile not found: {args.profile}")

        slots = profile.get("slots", ["19:00", "20:20"])
        if not isinstance(slots, list) or not slots:
            raise SystemExit("profile.slots must be a non-empty list.")

        # Enforce tomorrow booking for smart profiles as requested.
        site_date_type = 2

        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "book_api_daily.py"),
            "--config",
            args.config,
            "--wait-until",
            str(profile.get("wait_until", "06:00:00")),
            "--slots",
            ",".join(str(x).strip() for x in slots if str(x).strip()),
            "--venue-id",
            str(int(profile.get("venue_id", 3))),
            "--block-type",
            str(int(profile.get("block_type", 2))),
            "--site-date-type",
            str(site_date_type),
            "--max-polls",
            str(int(profile.get("max_polls", 320))),
            "--poll-interval-ms",
            str(int(profile.get("poll_interval_ms", 25))),
            "--preheat-requests",
            str(int(profile.get("preheat_requests", 6))),
            "--slot-workers",
            str(int(profile.get("slot_workers", 2))),
            "--create-retries",
            str(int(profile.get("create_retries", 5))),
        ]
        if bool(profile.get("ntp_sync", True)):
            cmd.append("--ntp-sync")
        if bool(profile.get("force_refresh_login", False)):
            cmd.append("--force-refresh-login")
        account_profile = str(profile.get("account_profile", "")).strip()
        if account_profile:
            cmd.extend(["--account-profile", account_profile])
        if bool(profile.get("multi_session_hold", False)):
            cmd.append("--multi-session-hold")
            cmd.extend(["--booking-window-seconds", str(int(profile.get("booking_window_seconds", 180)))])
            cmd.extend(["--session-workers", str(int(profile.get("session_workers", 8)))])
            cmd.extend(["--max-holds-per-slot", str(int(profile.get("max_holds_per_slot", 0)))])
        peer_accounts = profile.get("peer_accounts", [])
        if isinstance(peer_accounts, list):
            peer_accounts_text = ",".join(str(x).strip() for x in peer_accounts if str(x).strip())
        else:
            peer_accounts_text = str(peer_accounts).strip()
        if peer_accounts_text:
            cmd.extend(["--peer-accounts", peer_accounts_text])
        return run_cmd(cmd)

    raise SystemExit(f"Unsupported action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
