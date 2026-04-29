from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter


@dataclass
class SessionItem:
    id: int
    site_id: int
    start_time: str
    end_time: str
    site_time_group: str
    ticket_price: float
    stock: int
    appointment: bool
    raw: dict[str, Any]


def _extract_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    if isinstance(payload, dict):
        return payload
    return {}


def result_unauthorized(result: dict[str, Any]) -> bool:
    http_status = int(result.get("http_status", 0) or 0)
    if http_status in (401, 403):
        return True
    payload = _extract_payload(result)
    code = str(payload.get("code", payload.get("status", ""))).strip().lower()
    if code in {"401", "403", "-1", "error", "unauthorized"}:
        return True
    message = f"{payload.get('message', '')} {payload.get('msg', '')}".lower()
    return any(t in message for t in ("login", "unauthorized", "请先登录", "未登录"))


class SztuHttpClient:
    def __init__(self, *, base_url: str, storage_state: Path, timeout_seconds: float = 1.2):
        self.base_url = base_url.rstrip("/") + "/"
        self.storage_state = storage_state
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.trust_env = False
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "lang": "zh",
                "connection": "keep-alive",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            }
        )
        self._load_storage_state()
        token = self._cookie_value("client-access-token")
        if token:
            self.session.headers["web-x-auth-token"] = token

    def _cookie_value(self, name: str) -> str:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return str(cookie.value)
        return ""

    def _load_storage_state(self) -> None:
        if not self.storage_state.exists():
            return
        content = json.loads(self.storage_state.read_text(encoding="utf-8"))
        for cookie in content.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name", "")).strip()
            value = str(cookie.get("value", ""))
            domain = str(cookie.get("domain", "")).strip()
            path = str(cookie.get("path", "/")).strip() or "/"
            if name and domain:
                self.session.cookies.set(name, value, domain=domain, path=path)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> dict[str, Any]:
        last_error = ""
        url = urljoin(self.base_url, path.lstrip("/"))
        for _ in range(max(1, retries)):
            try:
                resp = self.session.request(
                    method=method.upper(),
                    url=url,
                    json=payload if method.upper() != "GET" else None,
                    timeout=self.timeout_seconds,
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text[:300]}
                return {
                    "ok": bool(resp.ok),
                    "http_status": int(resp.status_code),
                    "payload": data if isinstance(data, dict) else {"data": data},
                }
            except Exception as exc:
                last_error = str(exc)
        return {"ok": False, "http_status": 0, "payload": {"error": last_error}}

    def probe_auth(self) -> dict[str, Any]:
        return self.request_json("GET", "/proxy/api/user/order/list?status=1", retries=1)

    def list_sessions(
        self,
        *,
        venue_id: int,
        block_type: int,
        site_date_type: int,
        session_type: int = 0,
    ) -> tuple[list[SessionItem], dict[str, Any]]:
        result = self.request_json(
            "POST",
            "/proxy/api/venue/site/session/list",
            payload={
                "venueId": venue_id,
                "blockType": block_type,
                "siteDateType": site_date_type,
                "sessionType": session_type,
                "stock": None,
            },
            retries=1,
        )
        payload = _extract_payload(result)
        rows: list[dict[str, Any]] = []
        data = payload.get("data") or {}
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    rows.extend([r for r in value if isinstance(r, dict)])

        sessions: list[SessionItem] = []
        for row in rows:
            try:
                sessions.append(
                    SessionItem(
                        id=int(row.get("id", 0)),
                        site_id=int(row.get("siteId", 0)),
                        start_time=str(row.get("startTime", "")),
                        end_time=str(row.get("endTime", "")),
                        site_time_group=str(row.get("siteTimeGroup", "")),
                        ticket_price=float(row.get("ticketPrice", 0) or 0),
                        stock=int(row.get("stock", 0) or 0),
                        appointment=bool(row.get("appointment", False)),
                        raw=row,
                    )
                )
            except Exception:
                continue
        return sessions, result

    def list_assigned_sessions(
        self,
        *,
        venue_id: int,
        block_type: int,
        site_date_type: int,
        site_id: int,
        site_time_group: str,
    ) -> tuple[list[SessionItem], dict[str, Any]]:
        result = self.request_json(
            "POST",
            "/proxy/api/venue/site/session/assign/detail",
            payload={
                "siteId": str(site_id),
                "siteTimeGroup": site_time_group,
                "siteDateType": str(site_date_type),
                "blockType": str(block_type),
                "venueId": str(venue_id),
            },
            retries=1,
        )
        payload = _extract_payload(result)
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = []

        sessions: list[SessionItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                session_id = int(row.get("sessionId", row.get("id", 0)))
                sessions.append(
                    SessionItem(
                        id=session_id,
                        site_id=int(row.get("siteId", 0)),
                        start_time=str(row.get("startTime", "")),
                        end_time=str(row.get("endTime", "")),
                        site_time_group=str(row.get("siteTimeGroup", site_time_group)),
                        ticket_price=float(row.get("ticketPrice", 0) or 0),
                        stock=int(row.get("stock", 0) or 0),
                        appointment=bool(row.get("appointment", False)),
                        raw=row,
                    )
                )
            except Exception:
                continue
        return sessions, result

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", "/proxy/api/user/order/create", payload=payload, retries=1)

    def pay_order(self, order_no: str, pay_type: str = "5") -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/proxy/api/pay/pay",
            payload={"orderNo": order_no, "payType": str(pay_type)},
            retries=1,
        )

    def list_orders(self, status: int) -> dict[str, Any]:
        return self.request_json("GET", f"/proxy/api/user/order/list?status={status}", retries=1)

    def get_order_detail(self, order_no: str) -> dict[str, Any]:
        return self.request_json("GET", f"/proxy/api/user/order/details?orderNo={order_no}", retries=1)

    def add_order_peer(self, order_no: str, peer_user_num: str) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/proxy/api/user/order/peer/add",
            payload={"orderNo": order_no, "peerUserNum": str(peer_user_num).strip()},
            retries=1,
        )

    def cancel_order(self, order_no: str) -> dict[str, Any]:
        return self.request_json("PUT", f"/proxy/api/user/order/cancel?orderNo={order_no}", retries=1)

    def find_orders_for_slot(
        self,
        *,
        slot: str,
        site_date_type: int,
        venue_keyword: str = "",
        venue_id: int | None = None,
        statuses: tuple[int, ...] = (2, 3, 1, 4, 6, 5),
    ) -> list[dict[str, Any]]:
        target_date = datetime.now().date() + timedelta(days=1 if int(site_date_type) == 2 else 0)
        expected_date = target_date.strftime("%Y-%m-%d")
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for status in statuses:
            result = self.list_orders(status)
            if result_unauthorized(result):
                return []
            payload = _extract_payload(result)
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                order_no = str(item.get("orderNo", "")).strip()
                start_time = str(item.get("startTime", "")).strip()
                site_date = str(item.get("siteDate", "")).strip()
                venue_name = str(item.get("venueName", "")).strip()
                raw_venue_id = item.get("venueId", item.get("venueID", item.get("venue_id")))
                if not order_no or order_no in seen:
                    continue
                if site_date != expected_date:
                    continue
                if not start_time.startswith(slot):
                    continue
                if venue_keyword and venue_keyword not in venue_name:
                    continue
                if venue_id is not None and raw_venue_id not in (None, ""):
                    try:
                        if int(raw_venue_id) != int(venue_id):
                            continue
                    except Exception:
                        pass

                detail_result = self.get_order_detail(order_no)
                detail_payload = _extract_payload(detail_result)
                detail_data = detail_payload.get("data") if isinstance(detail_payload, dict) else {}
                detail_status = None
                if isinstance(detail_data, dict):
                    try:
                        detail_status = int(detail_data.get("status"))
                    except Exception:
                        detail_status = None
                seen.add(order_no)
                matched.append(
                    {
                        "order_no": order_no,
                        "list_item": item,
                        "detail": detail_data if isinstance(detail_data, dict) else {},
                        "detail_status": detail_status,
                        "list_status": status,
                    }
                )
        return matched

    def find_order_for_slot(
        self,
        *,
        slot: str,
        site_date_type: int,
        venue_keyword: str = "",
    ) -> dict[str, Any] | None:
        orders = self.find_orders_for_slot(
            slot=slot,
            site_date_type=site_date_type,
            venue_keyword=venue_keyword,
        )
        return orders[0] if orders else None


def choose_target_session(sessions: list[SessionItem], slot: str) -> SessionItem | None:
    matched = choose_target_sessions(sessions, slot)
    return matched[0] if matched else None


def choose_target_sessions(sessions: list[SessionItem], slot: str) -> list[SessionItem]:
    slot_candidates = {slot, f"{slot}:00"}
    matched = [
        s
        for s in sessions
        if any(s.start_time.startswith(c) for c in slot_candidates) and s.stock > 0
    ]
    if not matched:
        return []
    return sorted(
        matched,
        key=lambda x: (int(x.appointment), int(x.stock > 0), x.stock, -x.ticket_price),
        reverse=True,
    )


def create_order_with_fallback(client: SztuHttpClient, session: SessionItem) -> tuple[bool, dict[str, Any]]:
    candidates = [
        {"siteSessionId": session.id, "pointsDeduction": int(round(session.ticket_price)), "payType": 5},
        {"siteSessionId": session.id, "pointsDeduction": 0, "payType": 5},
        {"siteSessionId": session.id, "pointsDeduction": 0, "payType": 2},
    ]
    last: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    for payload in candidates:
        result = client.create_order(payload)
        response_payload = _extract_payload(result)
        attempt = {"request": payload, "result": result}
        attempts.append(attempt)
        last = attempt
        if int(response_payload.get("status", 0) or 0) == 1:
            last["attempts"] = attempts
            return True, last
    if last:
        last["attempts"] = attempts
    return False, last


def try_pay_order(client: SztuHttpClient, order_no: str, preferred_pay_type: str | int | None = None) -> tuple[bool, dict[str, Any]]:
    candidates: list[str] = []
    if preferred_pay_type is not None:
        candidates.append(str(preferred_pay_type))
    for default in ("5", "2"):
        if default not in candidates:
            candidates.append(default)

    last: dict[str, Any] = {}
    for pay_type in candidates:
        result = client.pay_order(order_no, pay_type=pay_type)
        payload = result.get("payload") if isinstance(result, dict) else {}
        last = {"pay_type": pay_type, "result": result}
        if isinstance(payload, dict) and int(payload.get("status", 0) or 0) == 1:
            return True, last
    return False, last


def is_duplicate_order_response(result_payload: dict[str, Any]) -> bool:
    if not isinstance(result_payload, dict):
        return False
    code = str(result_payload.get("code", "")).strip()
    status = int(result_payload.get("status", 0) or 0)
    msg = str(result_payload.get("msg", "")).lower()
    if code == "PleaseDoNotPlaceDuplicateOrders" or status == -3014:
        return True
    return "duplicate" in msg
