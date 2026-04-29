from __future__ import annotations

import os
from getpass import getpass
from typing import Any

from playwright.sync_api import Page

from .browser import click_selector_or_text
from .settings import Settings


def first_visible_locator(page: Page, selectors: list[str], timeout_ms: int = 800):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:
            continue
    return None


def fill_first(page: Page, selectors: list[str], value: str, label: str) -> None:
    locator = first_visible_locator(page, selectors)
    if not locator:
        raise RuntimeError(f"Could not find {label} input.")
    locator.fill(value)


def click_submit(page: Page, selectors: list[str], keywords: list[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=700)
            locator.click()
            return True
        except Exception:
            continue
    return click_selector_or_text(page, "", keywords, timeout_ms=1200)


def click_venue_card(page: Page, keywords: list[str]) -> bool:
    cards = page.locator("div[class*='home_bookingCard']")
    try:
        count = cards.count()
    except Exception:
        count = 0
    for index in range(count):
        card = cards.nth(index)
        try:
            text = card.inner_text(timeout=800)
        except Exception:
            text = ""
        if not any(keyword and keyword in text for keyword in keywords):
            continue
        try:
            card.scroll_into_view_if_needed(timeout=2000)
            card.click(timeout=2500)
            return True
        except Exception:
            continue
    return False


def click_popup_login(page: Page) -> bool:
    selectors = [
        ".adm-center-popup >> text=\u7acb\u5373\u767b\u5f55",
        ".adm-modal >> text=\u7acb\u5373\u767b\u5f55",
        "text=\u7acb\u5373\u767b\u5f55",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=1200)
            locator.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    buttons = page.locator(".adm-center-popup button, .adm-modal button, button")
    try:
        count = buttons.count()
    except Exception:
        count = 0
    for index in range(count):
        button = buttons.nth(index)
        try:
            text = button.inner_text(timeout=500)
        except Exception:
            text = ""
        if "\u7acb\u5373\u767b\u5f55" not in text and "\u767b\u5f55" not in text:
            continue
        try:
            button.click(timeout=2500, force=True)
            return True
        except Exception:
            continue
    return False


def click_unified_auth(page: Page) -> bool:
    selectors = [
        "a[class*='quickLoginBtn']",
        "text=\u7edf\u4e00\u8ba4\u8bc1\u767b\u5f55",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=3000, no_wait_after=True)
            return True
        except Exception:
            continue
    candidates = page.locator("a, button, div")
    try:
        count = min(candidates.count(), 200)
    except Exception:
        count = 0
    for index in range(count):
        locator = candidates.nth(index)
        try:
            text = locator.inner_text(timeout=300)
        except Exception:
            text = ""
        if "\u7edf\u4e00\u8ba4\u8bc1\u767b\u5f55" not in text:
            continue
        try:
            locator.click(timeout=3000, no_wait_after=True)
            return True
        except Exception:
            continue
    return False


def looks_logged_in(page: Page, success_keywords: list[str]) -> bool:
    if "type=login" in page.url.lower() or "login" in page.url.lower():
        return False
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    if (
        "\u7edf\u4e00\u8ba4\u8bc1\u767b\u5f55" in body
        or "\u6b22\u8fce\u767b\u5f55" in body
        or "\u7acb\u5373\u767b\u5f55" in body
        or "\u8bf7\u5148\u767b\u5f55" in body
    ):
        return False
    return any(keyword and keyword in body for keyword in success_keywords)


def probe_login_by_api(page: Page) -> bool:
    result = page.evaluate(
        """
        async () => {
          try {
            const token = document.cookie
              .split(';')
              .map((item) => item.trim())
              .find((item) => item.startsWith('client-access-token='))
              ?.split('=')
              .slice(1)
              .join('=') || '';
            const response = await fetch('/proxy/api/user/order/list?status=1', {
              method: 'GET',
              credentials: 'include',
              headers: token ? { 'web-x-auth-token': decodeURIComponent(token) } : {},
            });
            let payload = null;
            try {
              payload = await response.json();
            } catch (_) {}
            return {
              ok: response.ok,
              status: response.status,
              payload,
            };
          } catch (error) {
            return { ok: false, status: 0, payload: { error: String(error) } };
          }
        }
        """
    )
    if not isinstance(result, dict):
        return False

    status = int(result.get("status") or 0)
    payload = result.get("payload")
    if status in (401, 403):
        return False

    if isinstance(payload, dict):
        code = str(payload.get("code", payload.get("status", ""))).strip().lower()
        if code in ("401", "403", "-1", "error", "unauthorized"):
            return False
        status_text = str(payload.get("status", "")).strip().lower()
        if status_text in ("error", "401", "403", "unauthorized"):
            return False
        message = f"{payload.get('message', '')} {payload.get('msg', '')}".lower()
        if any(
            token in message
            for token in ["login", "unauthorized", "token", "\u672a\u767b\u5f55", "\u8bf7\u5148\u767b\u5f55"]
        ):
            return False
        if "data" in payload:
            return True
    return bool(result.get("ok"))


def session_looks_valid(page: Page, settings: Settings) -> bool:
    page.goto(settings.base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(700)
    if "type=login" in page.url.lower() or "login" in page.url.lower():
        return False
    if probe_login_by_api(page):
        return True
    login_config = dict(settings.raw.get("login", {}))
    click_venue_card(page, list(login_config.get("trigger_venue_keywords", ["\u7fbd\u6bdb\u7403"])))
    page.wait_for_timeout(600)
    return probe_login_by_api(page)


def resolve_credentials(
    settings: Settings,
    username: str = "",
    password: str = "",
    allow_prompt: bool = False,
) -> tuple[str, str]:
    login_config = dict(settings.raw.get("login", {}))
    resolved_username = (
        username.strip()
        or str(login_config.get("username", "")).strip()
        or os.environ.get("SZTU_GYM_USERNAME", "").strip()
    )
    resolved_password = password or str(login_config.get("password", "")) or os.environ.get("SZTU_GYM_PASSWORD", "")

    if allow_prompt and not resolved_username:
        resolved_username = input("Username: ").strip()
    if allow_prompt and not resolved_password:
        resolved_password = getpass("Password: ")
    return resolved_username, resolved_password


def auto_login(page: Page, settings: Settings, username: str, password: str) -> None:
    login_config = dict(settings.raw.get("login", {}))

    page.goto(settings.base_url, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=12000)
    page.wait_for_timeout(900)
    if probe_login_by_api(page):
        return

    # Direct login is more reliable than reaching the login page through a modal
    # because image/font failures can hide the modal text in headless runs.
    page.goto(settings.base_url.rstrip("/") + "/login", wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.wait_for_timeout(1500)

    body = page.locator("body").inner_text(timeout=5000)
    if page.url.rstrip("/").endswith("/login") or "\u7edf\u4e00\u8ba4\u8bc1\u767b\u5f55" in body:
        if not click_unified_auth(page):
            click_selector_or_text(page, "", ["\u7edf\u4e00\u8ba4\u8bc1\u767b\u5f55"], timeout_ms=3000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        # Some flows complete auth and jump back to home directly without showing account/password fields.
        if probe_login_by_api(page):
            return

    try:
        fill_first(
            page,
            list(
                login_config.get(
                    "username_selectors",
                    ["input[name='username']", "input[id='username']", "input[type='text']"],
                )
            ),
            username,
            "username",
        )
        fill_first(
            page,
            list(
                login_config.get(
                    "password_selectors",
                    ["input[name='password']", "input[id='password']", "input[type='password']"],
                )
            ),
            password,
            "password",
        )
    except RuntimeError:
        if probe_login_by_api(page):
            return
        raise
    if not click_submit(
        page,
        list(login_config.get("submit_selectors", ["button[type='submit']", "input[type='submit']"])),
        list(login_config.get("submit_keywords", ["\u767b\u5f55", "\u7acb\u5373\u767b\u5f55", "\u786e\u8ba4"])),
    ):
        raise RuntimeError("Could not find login submit button.")

    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.wait_for_timeout(3800)
    page.goto(settings.base_url, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=12000)
    page.wait_for_timeout(1100)
    click_venue_card(page, list(login_config.get("trigger_venue_keywords", ["\u7fbd\u6bdb\u7403"])))
    page.wait_for_timeout(1000)
    if not probe_login_by_api(page):
        raise RuntimeError("Login did not reach a valid API session.")


def ensure_logged_in(
    page: Page,
    settings: Settings,
    username: str = "",
    password: str = "",
    allow_prompt: bool = False,
    save_storage_state: bool = True,
) -> bool:
    if session_looks_valid(page, settings):
        return False

    resolved_username, resolved_password = resolve_credentials(
        settings=settings,
        username=username,
        password=password,
        allow_prompt=allow_prompt,
    )
    if not resolved_username or not resolved_password:
        raise RuntimeError(
            "Login state is invalid and no credentials are available. "
            "Set SZTU_GYM_USERNAME/SZTU_GYM_PASSWORD or login.username/login.password in config."
        )

    auto_login(page, settings, resolved_username, resolved_password)
    if save_storage_state:
        settings.storage_state.parent.mkdir(parents=True, exist_ok=True)
        page.context.storage_state(path=str(settings.storage_state))
    return True
