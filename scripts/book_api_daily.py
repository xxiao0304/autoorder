from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autoorder.auth import ensure_logged_in
from autoorder.browser import EdgeSession
from autoorder.notify import send_notification
from autoorder.settings import load_settings, with_account_profile
from autoorder.sztu_http import (
    SessionItem,
    SztuHttpClient,
    choose_target_session,
    choose_target_sessions,
    create_order_with_fallback,
    is_duplicate_order_response,
    result_unauthorized,
    try_pay_order,
)

def is_retryable_create_failure(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    status = int(payload.get("status", 0) or 0)
    code = str(payload.get("code", "")).strip()
    return status in (-3012, 9999) or code in {"TicketsSoldOut", "SystemError"}


def summarize_create_attempts(detail: dict) -> str:
    attempts = detail.get("attempts") if isinstance(detail, dict) else None
    if not isinstance(attempts, list):
        return ""
    parts: list[str] = []
    for i, item in enumerate(attempts, start=1):
        req = item.get("request") if isinstance(item, dict) else {}
        res = item.get("result") if isinstance(item, dict) else {}
        payload = res.get("payload") if isinstance(res, dict) else {}
        code = payload.get("code") if isinstance(payload, dict) else None
        status = payload.get("status") if isinstance(payload, dict) else None
        msg = payload.get("msg") if isinstance(payload, dict) else None
        parts.append(
            f"{i})payType={req.get('payType')},deduct={req.get('pointsDeduction')},code={code},status={status},msg={msg}"
        )
    return " | ".join(parts)


def extract_order_no(create_detail: dict[str, Any]) -> str:
    result_payload = ((create_detail.get("result") or {}).get("payload") or {}) if isinstance(create_detail, dict) else {}
    if isinstance(result_payload, dict):
        data = result_payload.get("data")
        if isinstance(data, dict):
            return str(data.get("orderNo", "")).strip()
    return ""


def cache_path_for_settings(settings) -> Path:
    return settings.storage_state.parent / "badminton_package_cache.json"


def session_to_cache_item(session: SessionItem) -> dict[str, Any]:
    return {
        "id": session.id,
        "site_id": session.site_id,
        "start_time": session.start_time,
        "end_time": session.end_time,
        "site_time_group": session.site_time_group,
        "ticket_price": session.ticket_price,
        "stock": session.stock,
        "appointment": session.appointment,
        "raw": session.raw,
    }


def session_from_cache_item(item: dict[str, Any]) -> SessionItem | None:
    try:
        return SessionItem(
            id=int(item.get("id", 0)),
            site_id=int(item.get("site_id", item.get("siteId", 0))),
            start_time=str(item.get("start_time", item.get("startTime", ""))),
            end_time=str(item.get("end_time", item.get("endTime", ""))),
            site_time_group=str(item.get("site_time_group", item.get("siteTimeGroup", ""))),
            ticket_price=float(item.get("ticket_price", item.get("ticketPrice", 0)) or 0),
            stock=int(item.get("stock", 0) or 0),
            appointment=bool(item.get("appointment", False)),
            raw=dict(item.get("raw", item)),
        )
    except Exception:
        return None


def load_cached_package_targets(
    *,
    settings,
    slot: str,
    venue_id: int,
    block_type: int,
    site_date_type: int,
) -> list[SessionItem]:
    path = cache_path_for_settings(settings)
    if not path.exists():
        return []
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if int(cache.get("venue_id", 0) or 0) != venue_id:
        return []
    if int(cache.get("block_type", 0) or 0) != block_type:
        return []
    if int(cache.get("site_date_type", 0) or 0) != site_date_type:
        return []
    slots = cache.get("slots") if isinstance(cache, dict) else {}
    if not isinstance(slots, dict):
        return []
    items = slots.get(slot) or []
    if not isinstance(items, list):
        return []
    sessions = [session_from_cache_item(item) for item in items if isinstance(item, dict)]
    return [s for s in sessions if s and s.stock > 0]


def ntp_offset_seconds(servers: list[str], timeout: float = 0.5) -> float:
    packet = b"\x1b" + 47 * b"\0"
    for host in servers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (host, 123))
                data, _ = s.recvfrom(48)
            if len(data) < 48:
                continue
            fields = struct.unpack("!12I", data)
            transmit = fields[10] + float(fields[11]) / 2**32
            unix_time = transmit - 2208988800
            return unix_time - time.time()
        except Exception:
            continue
    return 0.0


def precise_wait_until(clock_text: str, *, offset_seconds: float = 0.0) -> None:
    target_t = datetime.strptime(clock_text, "%H:%M:%S").time()
    now = datetime.now()
    target = datetime.combine(now.date(), target_t).timestamp()
    if target <= now.timestamp():
        return
    while True:
        remain = target - (time.time() + offset_seconds)
        if remain <= 0:
            return
        if remain > 1.2:
            time.sleep(0.3)
        elif remain > 0.2:
            time.sleep(0.02)
        elif remain > 0.02:
            time.sleep(0.002)
        else:
            while (target - (time.time() + offset_seconds)) > 0:
                pass
            return


def keepalive_until_trigger(
    *,
    client: SztuHttpClient,
    wait_until: str,
    venue_id: int,
    block_type: int,
    site_date_type: int,
    offset_seconds: float = 0.0,
) -> None:
    target_t = datetime.strptime(wait_until, "%H:%M:%S").time()
    now = datetime.now()
    target = datetime.combine(now.date(), target_t).timestamp()
    while True:
        remain = target - (time.time() + offset_seconds)
        if remain <= 0:
            return
        try:
            client.list_sessions(
                venue_id=venue_id,
                block_type=block_type,
                site_date_type=site_date_type,
                session_type=0,
            )
        except Exception:
            pass
        if remain > 30:
            time.sleep(8)
        elif remain > 8:
            time.sleep(2)
        else:
            time.sleep(0.5)


def refresh_login_state(settings) -> bool:
    with EdgeSession(settings, force_headless=True) as context:
        page = context.new_page()
        return ensure_logged_in(page, settings, save_storage_state=True)


def venue_keyword_for_id(venue_id: int) -> str:
    if venue_id == 3:
        return "羽毛球"
    if venue_id == 4:
        return "健身房"
    if venue_id == 46:
        return "体能中心"
    return ""


def add_peers_to_order(client: SztuHttpClient, order_no: str, peer_accounts: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    existing_peers: set[str] = set()
    detail = client.get_order_detail(order_no) if order_no and peer_accounts else {}
    payload = detail.get("payload") if isinstance(detail, dict) else {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    peer_order = data.get("peerOrder") if isinstance(data, dict) else []
    if isinstance(peer_order, list):
        for item in peer_order:
            if isinstance(item, dict):
                phone = str(item.get("phone", "")).strip()
                if phone:
                    existing_peers.add(phone)

    for account in peer_accounts:
        account = str(account).strip()
        if not account:
            continue
        if account in existing_peers:
            results.append({"account": account, "ok": True, "already_exists": True})
            continue
        result = client.add_order_peer(order_no, account)
        payload = result.get("payload") if isinstance(result, dict) else {}
        ok = bool(isinstance(payload, dict) and int(payload.get("status", 0) or 0) == 1)
        results.append({"account": account, "ok": ok, "result": result})
        if ok:
            existing_peers.add(account)
    return results


def order_status(client: SztuHttpClient, order_no: str) -> int | None:
    detail = client.get_order_detail(order_no)
    payload = detail.get("payload") if isinstance(detail, dict) else {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return None
    try:
        return int(data.get("status"))
    except Exception:
        return None


def try_pay_order_resilient(
    client: SztuHttpClient,
    order_no: str,
    *,
    preferred_pay_type: str | int | None = 5,
    attempts: int = 3,
) -> tuple[bool, dict[str, Any]]:
    last: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        ok, last = try_pay_order(client, order_no, preferred_pay_type=preferred_pay_type)
        if ok:
            return True, last
        status = order_status(client, order_no)
        if status in (1, 3, 4):
            return True, {"recovered_from_status": status, "last": last}
        if attempt < attempts:
            time.sleep(0.15)
    return False, last


def reconcile_slot_orders(
    *,
    client: SztuHttpClient,
    slot: str,
    site_date_type: int,
    venue_id: int,
    venue_keyword: str,
    held_orders: list[dict[str, Any]],
    peer_accounts: list[str] | None = None,
) -> dict[str, Any]:
    order_nos: list[str] = []
    for held in held_orders:
        order_no = str(held.get("order_no", "")).strip()
        if order_no and order_no not in order_nos:
            order_nos.append(order_no)

    # If create timed out after the server accepted it, the only reliable recovery
    # is to scan pending orders and take ownership of them before the 10-min expiry.
    pending_orders = client.find_orders_for_slot(
        slot=slot,
        site_date_type=site_date_type,
        venue_keyword=venue_keyword,
        venue_id=venue_id,
        statuses=(2,),
    )
    for item in pending_orders:
        order_no = str(item.get("order_no", "")).strip()
        if order_no and order_no not in order_nos:
            order_nos.append(order_no)

    if not order_nos:
        return {"ok": False, "reason": "no_recoverable_orders", "order_nos": []}

    paid_order_no = ""
    pay_result: dict[str, Any] = {}
    pay_failed: list[str] = []
    for order_no in order_nos:
        pay_ok, pay_result = try_pay_order_resilient(client, order_no, preferred_pay_type=5, attempts=3)
        if pay_ok:
            paid_order_no = order_no
            break
        pay_failed.append(order_no)

    cancelled_extra: list[str] = []
    for order_no in order_nos:
        if order_no and order_no != paid_order_no:
            cancel_result = client.cancel_order(order_no)
            cancelled_extra.append(f"{order_no}:{cancel_result.get('http_status')}")

    peer_results = add_peers_to_order(client, paid_order_no, peer_accounts or []) if paid_order_no else []
    return {
        "ok": bool(paid_order_no),
        "reason": "reconciled_slot_orders" if paid_order_no else "recovered_orders_pay_failed",
        "order_no": paid_order_no,
        "order_nos": order_nos,
        "held_count": len(order_nos),
        "cancelled_extra": cancelled_extra,
        "pay_failed": pay_failed,
        "pay_result": pay_result,
        "peer_results": peer_results,
    }


def book_slot_worker(
    *,
    settings,
    slot: str,
    venue_id: int,
    block_type: int,
    site_date_type: int,
    max_polls: int,
    poll_interval_ms: int,
    venue_keyword: str = "",
    stop_event: threading.Event | None = None,
    worker_id: int = 1,
    reauth_cb: Callable[[], bool] | None = None,
    create_retries: int = 5,
    peer_accounts: list[str] | None = None,
) -> dict[str, Any]:
    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)
    started_at = time.perf_counter()
    unauthorized_retries = 0

    for i in range(1, max_polls + 1):
        if stop_event and stop_event.is_set():
            return {"slot": slot, "ok": False, "reason": "cancelled_by_peer", "worker_id": worker_id}

        sessions, list_result = client.list_sessions(
            venue_id=venue_id,
            block_type=block_type,
            site_date_type=site_date_type,
            session_type=0,
        )
        if result_unauthorized(list_result):
            unauthorized_retries += 1
            if reauth_cb and unauthorized_retries <= 3 and reauth_cb():
                client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)
                continue
            return {"slot": slot, "ok": False, "reason": "unauthorized", "worker_id": worker_id}

        selected = choose_target_session(sessions, slot)
        if not selected:
            time.sleep(max(0.01, poll_interval_ms / 1000))
            continue

        hit_ms = int((time.perf_counter() - started_at) * 1000)
        create_ok = False
        create_detail: dict[str, Any] = {}
        for try_idx in range(1, max(1, create_retries) + 1):
            create_ok, create_detail = create_order_with_fallback(client, selected)
            if create_ok:
                break
            payload = {}
            result_inner = create_detail.get("result") if isinstance(create_detail, dict) else {}
            if isinstance(result_inner, dict):
                p = result_inner.get("payload")
                if isinstance(p, dict):
                    payload = p
            if selected.stock > 0 and is_retryable_create_failure(payload):
                if try_idx < create_retries:
                    time.sleep(0.08)
                    sessions2, list_result2 = client.list_sessions(
                        venue_id=venue_id,
                        block_type=block_type,
                        site_date_type=site_date_type,
                        session_type=0,
                    )
                    if result_unauthorized(list_result2):
                        unauthorized_retries += 1
                        if reauth_cb and unauthorized_retries <= 3 and reauth_cb():
                            client = SztuHttpClient(
                                base_url=settings.base_url,
                                storage_state=settings.storage_state,
                                timeout_seconds=1.0,
                            )
                            sessions2, _ = client.list_sessions(
                                venue_id=venue_id,
                                block_type=block_type,
                                site_date_type=site_date_type,
                                session_type=0,
                            )
                        else:
                            return {"slot": slot, "ok": False, "reason": "unauthorized", "worker_id": worker_id}
                    newer = choose_target_session(sessions2, slot)
                    if newer:
                        selected = newer
                    continue
            break
        if not create_ok:
            attempts_text = summarize_create_attempts(create_detail if isinstance(create_detail, dict) else {})
            result_payload = {}
            result_inner = create_detail.get("result") if isinstance(create_detail, dict) else {}
            if isinstance(result_inner, dict):
                payload = result_inner.get("payload")
                if isinstance(payload, dict):
                    result_payload = payload

            if is_duplicate_order_response(result_payload):
                existing = client.find_order_for_slot(
                    slot=slot,
                    site_date_type=site_date_type,
                    venue_keyword=venue_keyword,
                )
                if existing:
                    existing_order_no = str(existing.get("order_no", "")).strip()
                    detail_status = existing.get("detail_status")
                    if existing_order_no and detail_status == 2:
                        pay_ok, pay_result = try_pay_order_resilient(client, existing_order_no, preferred_pay_type=5)
                        if pay_ok:
                            peer_results = add_peers_to_order(client, existing_order_no, peer_accounts or [])
                            total_ms = int((time.perf_counter() - started_at) * 1000)
                            return {
                                "slot": slot,
                                "ok": True,
                                "session_id": selected.id,
                                "order_no": existing_order_no,
                                "polls": i,
                                "hit_ms": hit_ms,
                                "total_ms": total_ms,
                                "pay_ok": True,
                                "reason": "duplicate_then_pay_existing",
                                "peer_results": peer_results,
                                "worker_id": worker_id,
                            }
                    if detail_status in (1, 3, 4):
                        peer_results = add_peers_to_order(client, existing_order_no, peer_accounts or [])
                        total_ms = int((time.perf_counter() - started_at) * 1000)
                        return {
                            "slot": slot,
                            "ok": True,
                            "session_id": selected.id,
                            "order_no": existing_order_no,
                            "polls": i,
                            "hit_ms": hit_ms,
                            "total_ms": total_ms,
                            "pay_ok": detail_status != 2,
                            "reason": "duplicate_existing_order",
                            "peer_results": peer_results,
                            "worker_id": worker_id,
                        }

            return {
                "slot": slot,
                "ok": False,
                "reason": "create_failed",
                "session_id": selected.id,
                "polls": i,
                "hit_ms": hit_ms,
                "detail": create_detail,
                "attempts": attempts_text,
                "worker_id": worker_id,
            }

        order_no = extract_order_no(create_detail)

        pay_ok = False
        pay_result: dict[str, Any] = {}
        if order_no:
            preferred_pay_type = None
            if isinstance(create_detail, dict):
                req = create_detail.get("request")
                if isinstance(req, dict) and req.get("payType") is not None:
                    preferred_pay_type = req.get("payType")
            pay_ok, pay_result = try_pay_order_resilient(client, order_no, preferred_pay_type=preferred_pay_type)

        peer_results = add_peers_to_order(client, order_no, peer_accounts or []) if order_no and pay_ok else []
        total_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "slot": slot,
            "ok": bool(create_ok and (pay_ok or not order_no)),
            "session_id": selected.id,
            "order_no": order_no,
            "polls": i,
            "hit_ms": hit_ms,
            "total_ms": total_ms,
            "pay_ok": pay_ok,
            "pay_result": pay_result,
            "peer_results": peer_results,
            "worker_id": worker_id,
        }

    return {"slot": slot, "ok": False, "reason": "session_not_found", "worker_id": worker_id}


def create_hold_for_session(
    *,
    settings,
    session: SessionItem,
    create_retries: int,
) -> dict[str, Any]:
    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=2.5)
    last_detail: dict[str, Any] = {}
    for try_idx in range(1, max(1, create_retries) + 1):
        ok, detail = create_order_with_fallback(client, session)
        last_detail = detail
        if ok:
            return {
                "ok": True,
                "session_id": session.id,
                "site_id": session.site_id,
                "order_no": extract_order_no(detail),
                "detail": detail,
                "try_idx": try_idx,
            }
        result_payload = {}
        result_inner = detail.get("result") if isinstance(detail, dict) else {}
        if isinstance(result_inner, dict):
            payload = result_inner.get("payload")
            if isinstance(payload, dict):
                result_payload = payload
        if is_duplicate_order_response(result_payload):
            return {
                "ok": False,
                "duplicate": True,
                "session_id": session.id,
                "site_id": session.site_id,
                "detail": detail,
                "attempts": summarize_create_attempts(detail),
            }
        if not is_retryable_create_failure(result_payload):
            break
        time.sleep(0.05)
    return {
        "ok": False,
        "session_id": session.id,
        "site_id": session.site_id,
        "detail": last_detail,
        "attempts": summarize_create_attempts(last_detail),
    }


def expand_package_targets(
    *,
    client: SztuHttpClient,
    aggregates: list[SessionItem],
    venue_id: int,
    block_type: int,
    site_date_type: int,
) -> list[SessionItem]:
    expanded: list[SessionItem] = []
    seen: set[int] = set()
    for aggregate in aggregates:
        assigned, _ = client.list_assigned_sessions(
            venue_id=venue_id,
            block_type=block_type,
            site_date_type=site_date_type,
            site_id=aggregate.site_id,
            site_time_group=aggregate.site_time_group,
        )
        for item in assigned:
            if item.id in seen:
                continue
            # Package courts are represented as one-stock assigned sessions.
            if item.stock <= 0:
                continue
            seen.add(item.id)
            expanded.append(item)
    return sorted(expanded, key=lambda x: (x.stock, -x.ticket_price), reverse=True)


def book_slot_multi_session_worker(
    *,
    settings,
    slot: str,
    venue_id: int,
    block_type: int,
    site_date_type: int,
    booking_window_seconds: int,
    poll_interval_ms: int,
    venue_keyword: str,
    reauth_cb: Callable[[], bool] | None = None,
    create_retries: int = 5,
    session_workers: int = 8,
    max_holds_per_slot: int = 0,
    peer_accounts: list[str] | None = None,
) -> dict[str, Any]:
    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)
    started_at = time.perf_counter()
    deadline = started_at + max(1, booking_window_seconds)
    attempted_session_ids: set[int] = set()
    held_orders: list[dict[str, Any]] = []
    polls = 0
    unauthorized_retries = 0
    last_reconcile_at = 0.0

    while time.perf_counter() < deadline:
        polls += 1
        now_perf = time.perf_counter()
        if now_perf - last_reconcile_at >= 0.8:
            last_reconcile_at = now_perf
            reconciled = reconcile_slot_orders(
                client=client,
                slot=slot,
                site_date_type=site_date_type,
                venue_id=venue_id,
                venue_keyword=venue_keyword,
                held_orders=held_orders,
                peer_accounts=peer_accounts,
            )
            if reconciled.get("order_nos"):
                hit_ms = int((time.perf_counter() - started_at) * 1000)
                total_ms = hit_ms
                if bool(reconciled.get("ok")):
                    return {
                        "slot": slot,
                        "ok": True,
                        "reason": str(reconciled.get("reason", "reconciled_slot_orders")),
                        "order_no": reconciled.get("order_no", ""),
                        "held_count": reconciled.get("held_count", 0),
                        "cancelled_extra": reconciled.get("cancelled_extra", []),
                        "polls": polls,
                        "hit_ms": hit_ms,
                        "total_ms": total_ms,
                        "pay_ok": True,
                        "pay_result": reconciled.get("pay_result", {}),
                        "peer_results": reconciled.get("peer_results", []),
                    }
                return {
                    "slot": slot,
                    "ok": False,
                    "reason": str(reconciled.get("reason", "recovered_orders_pay_failed")),
                    "held_count": reconciled.get("held_count", 0),
                    "cancelled_extra": reconciled.get("cancelled_extra", []),
                    "pay_failed": reconciled.get("pay_failed", []),
                    "polls": polls,
                    "hit_ms": hit_ms,
                    "total_ms": total_ms,
                    "pay_result": reconciled.get("pay_result", {}),
                }

        candidate_targets: list[SessionItem] = []
        if block_type == 2:
            candidate_targets = load_cached_package_targets(
                settings=settings,
                slot=slot,
                venue_id=venue_id,
                block_type=block_type,
                site_date_type=site_date_type,
            )

        if not candidate_targets:
            sessions, list_result = client.list_sessions(
                venue_id=venue_id,
                block_type=block_type,
                site_date_type=site_date_type,
                session_type=0,
            )
            if result_unauthorized(list_result):
                unauthorized_retries += 1
                if reauth_cb and unauthorized_retries <= 3 and reauth_cb():
                    client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)
                    continue
                return {"slot": slot, "ok": False, "reason": "unauthorized", "polls": polls}

            aggregate_targets = choose_target_sessions(sessions, slot)
            if block_type == 2:
                candidate_targets = expand_package_targets(
                    client=client,
                    aggregates=aggregate_targets,
                    venue_id=venue_id,
                    block_type=block_type,
                    site_date_type=site_date_type,
                )
            else:
                candidate_targets = aggregate_targets

        targets = [s for s in candidate_targets if s.id not in attempted_session_ids]
        if max_holds_per_slot > 0:
            remaining_holds = max(0, max_holds_per_slot - len(held_orders))
            targets = targets[:remaining_holds]
        if not targets:
            time.sleep(max(0.01, poll_interval_ms / 1000))
            continue

        hit_ms = int((time.perf_counter() - started_at) * 1000)
        for target in targets:
            attempted_session_ids.add(target.id)

        with ThreadPoolExecutor(max_workers=max(1, min(session_workers, len(targets)))) as pool:
            futures = [
                pool.submit(
                    create_hold_for_session,
                    settings=settings,
                    session=target,
                    create_retries=create_retries,
                )
                for target in targets
            ]
            for future in as_completed(futures):
                result = future.result()
                if bool(result.get("ok")) and result.get("order_no"):
                    held_orders.append(result)
                elif bool(result.get("duplicate")):
                    existing = client.find_order_for_slot(
                        slot=slot,
                        site_date_type=site_date_type,
                        venue_keyword=venue_keyword,
                    )
                    existing_order_no = str((existing or {}).get("order_no", "")).strip()
                    if existing_order_no:
                        pay_ok, pay_result = try_pay_order_resilient(client, existing_order_no, preferred_pay_type=5)
                        if pay_ok:
                            peer_results = add_peers_to_order(client, existing_order_no, peer_accounts or [])
                            total_ms = int((time.perf_counter() - started_at) * 1000)
                            return {
                                "slot": slot,
                                "ok": True,
                                "reason": "multi_session_duplicate_paid_existing",
                                "order_no": existing_order_no,
                                "held_count": len(held_orders),
                                "polls": polls,
                                "hit_ms": hit_ms,
                                "total_ms": total_ms,
                                "pay_ok": True,
                                "pay_result": pay_result,
                                "peer_results": peer_results,
                            }

        reconciled = reconcile_slot_orders(
            client=client,
            slot=slot,
            site_date_type=site_date_type,
            venue_id=venue_id,
            venue_keyword=venue_keyword,
            held_orders=held_orders,
            peer_accounts=peer_accounts,
        )
        if reconciled.get("order_nos"):
            total_ms = int((time.perf_counter() - started_at) * 1000)
            if bool(reconciled.get("ok")):
                return {
                    "slot": slot,
                    "ok": True,
                    "reason": str(reconciled.get("reason", "reconciled_slot_orders")),
                    "order_no": reconciled.get("order_no", ""),
                    "held_count": reconciled.get("held_count", 0),
                    "cancelled_extra": reconciled.get("cancelled_extra", []),
                    "polls": polls,
                    "hit_ms": hit_ms,
                    "total_ms": total_ms,
                    "pay_ok": True,
                    "pay_result": reconciled.get("pay_result", {}),
                    "peer_results": reconciled.get("peer_results", []),
                }
            return {
                "slot": slot,
                "ok": False,
                "reason": str(reconciled.get("reason", "recovered_orders_pay_failed")),
                "held_count": reconciled.get("held_count", 0),
                "cancelled_extra": reconciled.get("cancelled_extra", []),
                "pay_failed": reconciled.get("pay_failed", []),
                "polls": polls,
                "hit_ms": hit_ms,
                "total_ms": total_ms,
                "pay_result": reconciled.get("pay_result", {}),
            }

        if not held_orders:
            time.sleep(max(0.01, poll_interval_ms / 1000))
            continue

        paid_order_no = ""
        pay_result: dict[str, Any] = {}
        for held in held_orders:
            order_no = str(held.get("order_no", "")).strip()
            if not order_no:
                continue
            pay_ok, pay_result = try_pay_order_resilient(client, order_no, preferred_pay_type=5)
            if pay_ok:
                paid_order_no = order_no
                break

        cancelled_extra: list[str] = []
        for held in held_orders:
            order_no = str(held.get("order_no", "")).strip()
            if order_no and order_no != paid_order_no:
                cancel_result = client.cancel_order(order_no)
                cancelled_extra.append(f"{order_no}:{cancel_result.get('http_status')}")

        total_ms = int((time.perf_counter() - started_at) * 1000)
        if paid_order_no:
            peer_results = add_peers_to_order(client, paid_order_no, peer_accounts or [])
            return {
                "slot": slot,
                "ok": True,
                "reason": "multi_session_hold_pay_one",
                "order_no": paid_order_no,
                "held_count": len(held_orders),
                "cancelled_extra": cancelled_extra,
                "polls": polls,
                "hit_ms": hit_ms,
                "total_ms": total_ms,
                "pay_ok": True,
                "pay_result": pay_result,
                "peer_results": peer_results,
            }

        existing = client.find_order_for_slot(
            slot=slot,
            site_date_type=site_date_type,
            venue_keyword=venue_keyword,
        )
        if existing:
            existing_order_no = str(existing.get("order_no", "")).strip()
            if existing_order_no:
                pay_ok, pay_result = try_pay_order_resilient(client, existing_order_no, preferred_pay_type=5)
                if pay_ok:
                    peer_results = add_peers_to_order(client, existing_order_no, peer_accounts or [])
                    return {
                        "slot": slot,
                        "ok": True,
                        "reason": "multi_session_existing_paid",
                        "order_no": existing_order_no,
                        "held_count": len(held_orders),
                        "polls": polls,
                        "hit_ms": hit_ms,
                        "total_ms": total_ms,
                        "pay_ok": True,
                        "pay_result": pay_result,
                        "peer_results": peer_results,
                    }

        return {
            "slot": slot,
            "ok": False,
            "reason": "held_but_pay_failed",
            "held_count": len(held_orders),
            "cancelled_extra": cancelled_extra,
            "polls": polls,
            "hit_ms": hit_ms,
            "total_ms": total_ms,
            "pay_result": pay_result,
        }

    return {
        "slot": slot,
        "ok": False,
        "reason": "session_not_found_in_window",
        "polls": polls,
        "window_seconds": booking_window_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast daily API booking with warmup and parallel workers.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--wait-until", default="18:00:00", help="HH:MM:SS trigger point.")
    parser.add_argument("--slots", default="19:00,20:20")
    parser.add_argument("--venue-id", type=int, default=3)
    parser.add_argument("--block-type", type=int, default=2, help="1=scatter, 2=package")
    parser.add_argument("--site-date-type", type=int, default=2, help="2=tomorrow")
    parser.add_argument("--max-polls", type=int, default=220)
    parser.add_argument("--poll-interval-ms", type=int, default=45)
    parser.add_argument("--ntp-sync", action="store_true", help="Try NTP sync before trigger.")
    parser.add_argument("--ntp-servers", default="ntp.aliyun.com,ntp.ntsc.ac.cn,time.windows.com")
    parser.add_argument("--preheat-requests", type=int, default=3)
    parser.add_argument("--force-refresh-login", action="store_true")
    parser.add_argument("--slot-workers", type=int, default=2, help="Parallel workers per slot.")
    parser.add_argument("--create-retries", type=int, default=5)
    parser.add_argument("--multi-session-hold", action="store_true", help="Hold multiple sessions per slot, pay one, cancel extras.")
    parser.add_argument("--booking-window-seconds", type=int, default=180, help="Polling window after trigger for multi-session mode.")
    parser.add_argument("--session-workers", type=int, default=8, help="Parallel create workers for multi-session mode.")
    parser.add_argument("--max-holds-per-slot", type=int, default=0, help="Limit held orders per slot before reconciliation; 0 means unlimited.")
    parser.add_argument("--peer-accounts", default="", help="Comma-separated student/work/mobile numbers to add after a paid package order.")
    parser.add_argument("--recover-pending-only", action="store_true", help="Only recover pending orders for the target slots; do not create new orders.")
    parser.add_argument("--account-profile", default="", help="Use a named account profile from config.accounts.")
    parser.add_argument("--no-notify", action="store_true", help="Skip notification after booking finishes.")
    args = parser.parse_args()

    settings = with_account_profile(load_settings(args.config), args.account_profile)
    slots = [s.strip() for s in args.slots.split(",") if s.strip()]
    peer_accounts = [s.strip() for s in str(args.peer_accounts or "").split(",") if s.strip()]
    if not slots:
        raise SystemExit("No slots provided.")

    if args.force_refresh_login:
        refresh_login_state(settings)

    main_client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)
    auth_probe = main_client.probe_auth()
    if result_unauthorized(auth_probe):
        relogin = refresh_login_state(settings)
        if relogin:
            print("Login refreshed during warmup.")
        main_client = SztuHttpClient(base_url=settings.base_url, storage_state=settings.storage_state, timeout_seconds=1.0)

    relogin_lock = threading.Lock()
    relogin_state: dict[str, float] = {"last_ts": 0.0}

    def reauth_cb() -> bool:
        with relogin_lock:
            now = time.time()
            if now - relogin_state["last_ts"] < 2.0:
                return True
            refresh_login_state(settings)
            relogin_state["last_ts"] = time.time()
            return True

    for _ in range(max(0, args.preheat_requests)):
        main_client.list_sessions(
            venue_id=args.venue_id,
            block_type=args.block_type,
            site_date_type=args.site_date_type,
            session_type=0,
        )

    offset = 0.0
    if args.ntp_sync:
        servers = [s.strip() for s in args.ntp_servers.split(",") if s.strip()]
        offset = ntp_offset_seconds(servers)
        print(f"NTP offset seconds: {offset:.6f}")

    keepalive_until_trigger(
        client=main_client,
        wait_until=args.wait_until,
        venue_id=args.venue_id,
        block_type=args.block_type,
        site_date_type=args.site_date_type,
        offset_seconds=offset,
    )
    precise_wait_until(args.wait_until, offset_seconds=offset)

    if args.recover_pending_only:
        results: list[dict[str, Any]] = []
        for slot in slots:
            reconciled = reconcile_slot_orders(
                client=main_client,
                slot=slot,
                site_date_type=args.site_date_type,
                venue_id=args.venue_id,
                venue_keyword=venue_keyword_for_id(args.venue_id),
                held_orders=[],
                peer_accounts=peer_accounts,
            )
            result = {
                "slot": slot,
                "ok": bool(reconciled.get("ok")),
                "reason": reconciled.get("reason", "no_recoverable_orders"),
                "order_no": reconciled.get("order_no", ""),
                "held_count": reconciled.get("held_count", 0),
                "cancelled_extra": reconciled.get("cancelled_extra", []),
                "pay_failed": reconciled.get("pay_failed", []),
                "pay_result": reconciled.get("pay_result", {}),
                "peer_results": reconciled.get("peer_results", []),
            }
            print(result)
            results.append(result)
        return 0 if any(bool(r.get("ok")) for r in results) else 2

    results: list[dict[str, Any]] = []
    workers = min(max(1, len(slots) * max(1, args.slot_workers)), 8)
    slot_done = {slot: threading.Event() for slot in slots}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for slot in slots:
            venue_keyword = venue_keyword_for_id(args.venue_id)

            if args.multi_session_hold:
                future = pool.submit(
                    book_slot_multi_session_worker,
                    settings=settings,
                    slot=slot,
                    venue_id=args.venue_id,
                    block_type=args.block_type,
                    site_date_type=args.site_date_type,
                    booking_window_seconds=args.booking_window_seconds,
                    poll_interval_ms=args.poll_interval_ms,
                    venue_keyword=venue_keyword,
                    reauth_cb=reauth_cb,
                    create_retries=args.create_retries,
                    session_workers=args.session_workers,
                    max_holds_per_slot=args.max_holds_per_slot,
                    peer_accounts=peer_accounts,
                )
                futures[future] = slot
                continue

            for worker_id in range(1, max(1, args.slot_workers) + 1):
                future = pool.submit(
                    book_slot_worker,
                    settings=settings,
                    slot=slot,
                    venue_id=args.venue_id,
                    block_type=args.block_type,
                    site_date_type=args.site_date_type,
                    max_polls=args.max_polls,
                    poll_interval_ms=args.poll_interval_ms,
                    venue_keyword=venue_keyword,
                    stop_event=slot_done[slot],
                    worker_id=worker_id,
                    reauth_cb=reauth_cb,
                    create_retries=args.create_retries,
                    peer_accounts=peer_accounts,
                )
                futures[future] = slot

        for future in as_completed(futures):
            result = future.result()
            slot = str(result.get("slot", ""))
            ok = bool(result.get("ok", False))
            if ok:
                if slot and not slot_done[slot].is_set():
                    slot_done[slot].set()
                    print(result)
                    results.append(result)
            else:
                if result.get("reason") not in {"cancelled_by_peer"}:
                    print(result)
                    results.append(result)

    final_by_slot: dict[str, dict[str, Any]] = {}
    for slot in slots:
        candidates = [r for r in results if str(r.get("slot", "")) == slot]
        success = next((r for r in candidates if bool(r.get("ok", False))), None)
        final_by_slot[slot] = success or (candidates[0] if candidates else {"slot": slot, "ok": False, "reason": "no_result"})

    failed = [r for r in final_by_slot.values() if not bool(r.get("ok", False))]
    lines = [
        f"time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"venue_id={args.venue_id}, block_type={args.block_type}, site_date_type={args.site_date_type}",
        f"trigger={args.wait_until}, ntp_offset={offset:.6f}",
    ]
    for slot in slots:
        r = final_by_slot[slot]
        if bool(r.get("ok", False)):
            peer_results = r.get("peer_results") if isinstance(r.get("peer_results"), list) else []
            peer_summary = ""
            if peer_results:
                ok_count = len([x for x in peer_results if isinstance(x, dict) and x.get("ok")])
                peer_summary = f" peers={ok_count}/{len(peer_results)}"
            lines.append(
                f"{slot}: SUCCESS order={r.get('order_no','-')} polls={r.get('polls','-')} "
                f"hit_ms={r.get('hit_ms','-')} total_ms={r.get('total_ms','-')} worker={r.get('worker_id','-')}"
                f"{peer_summary}"
            )
        else:
            detail_attempts = str(r.get("attempts", "") or "")
            fail_line = f"{slot}: FAILED reason={r.get('reason','unknown')}"
            if detail_attempts:
                fail_line += f" attempts={detail_attempts}"
            lines.append(fail_line)

    if args.no_notify:
        return 0 if not failed else 2

    notify_ok, notify_msg = send_notification(
        settings,
        title=f"SZTU 预约结果 {'成功' if not failed else '部分失败'}",
        lines=lines,
    )
    print({"notify_sent": notify_ok, "notify_msg": notify_msg})
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
