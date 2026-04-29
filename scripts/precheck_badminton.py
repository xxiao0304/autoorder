from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoorder.auth import ensure_logged_in
from autoorder.browser import EdgeSession
from autoorder.notify import send_notification
from autoorder.settings import load_settings, with_account_profile
from autoorder.sztu_http import SztuHttpClient, choose_target_sessions, result_unauthorized
from scripts.book_api_daily import expand_package_targets, session_to_cache_item


def main() -> int:
    parser = argparse.ArgumentParser(description="Precheck badminton booking auth and tomorrow session API.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--venue-id", type=int, default=3)
    parser.add_argument("--block-type", type=int, default=2)
    parser.add_argument("--site-date-type", type=int, default=2)
    parser.add_argument("--slots", default="19:00,20:20")
    parser.add_argument("--account-profile", default="", help="Use a named account profile from config.accounts.")
    args = parser.parse_args()

    base_settings = load_settings(args.config)
    default_profile = ""
    automation = base_settings.raw.get("automation", {})
    if isinstance(automation, dict) and int(args.venue_id) == 3:
        default_profile = str(automation.get("badminton_account_profile", "")).strip()
    settings = with_account_profile(base_settings, args.account_profile or default_profile)
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with EdgeSession(settings, force_headless=True) as context:
        page = context.new_page()
        ensure_logged_in(page, settings, save_storage_state=True)

    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=2.0)
    sessions, result = client.list_sessions(
        venue_id=args.venue_id,
        block_type=args.block_type,
        site_date_type=args.site_date_type,
        session_type=0,
    )
    unauthorized = result_unauthorized(result)
    slots = [s.strip() for s in args.slots.split(",") if s.strip()]
    present_slots = sorted({s.start_time[:5] for s in sessions})
    target_present = [slot for slot in slots if slot in present_slots]
    expanded_by_slot: dict[str, list[dict]] = {}
    for slot in slots:
        aggregates = choose_target_sessions(sessions, slot)
        expanded = expand_package_targets(
            client=client,
            aggregates=aggregates,
            venue_id=args.venue_id,
            block_type=args.block_type,
            site_date_type=args.site_date_type,
        )
        expanded_by_slot[slot] = [session_to_cache_item(item) for item in expanded]

    cache = {
        "cached_at": checked_at,
        "venue_id": args.venue_id,
        "block_type": args.block_type,
        "site_date_type": args.site_date_type,
        "slots": expanded_by_slot,
    }
    cache_path = settings.storage_state.parent / "badminton_package_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"time={checked_at}",
        f"venue_id={args.venue_id}, block_type={args.block_type}, site_date_type={args.site_date_type}",
        f"http_status={result.get('http_status')}",
        f"session_count={len(sessions)}",
        f"present_slots={','.join(present_slots) or '-'}",
        f"target_present={','.join(target_present) or '-'}",
        "expanded_counts=" + ",".join(f"{slot}:{len(expanded_by_slot.get(slot, []))}" for slot in slots),
        f"cache_path={cache_path}",
        f"unauthorized={unauthorized}",
    ]
    send_notification(
        settings,
        title="SZTU 羽毛球抢票预检",
        lines=lines,
    )
    print({
        "unauthorized": unauthorized,
        "session_count": len(sessions),
        "present_slots": present_slots,
        "expanded_counts": {slot: len(expanded_by_slot.get(slot, [])) for slot in slots},
        "cache_path": str(cache_path),
    })
    return 2 if unauthorized else 0


if __name__ == "__main__":
    raise SystemExit(main())
