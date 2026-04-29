from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError, sync_playwright

from .settings import Settings


class EdgeSession:
    def __init__(self, settings: Settings, force_headless: bool | None = None):
        self.settings = settings
        self.force_headless = force_headless
        self._playwright: Any = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> BrowserContext:
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            channel="msedge",
            headless=self.settings.headless if self.force_headless is None else self.force_headless,
            slow_mo=self.settings.slow_mo_ms,
            args=list(
                self.settings.raw.get(
                    "edge_args",
                    [
                        "--proxy-bypass-list=*.sztu.edu.cn;10.*",
                        "--disable-background-networking",
                        "--disable-extensions",
                        "--disable-sync",
                        "--no-first-run",
                    ],
                )
            ),
        )
        kwargs: dict[str, Any] = {
            "viewport": dict(
                self.settings.raw.get(
                    "viewport",
                    {
                        "width": 430,
                        "height": 932,
                    },
                )
            ),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        if self.settings.storage_state.exists():
            kwargs["storage_state"] = str(self.settings.storage_state)
        self.context = self.browser.new_context(**kwargs)
        self.context.set_default_timeout(int(self.settings.raw.get("action_timeout_ms", 1200)))
        self.context.set_default_navigation_timeout(int(self.settings.raw.get("navigation_timeout_ms", 8000)))

        blocked_types = set(self.settings.raw.get("block_resource_types", []))
        if blocked_types:
            self.context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in blocked_types
                else route.continue_(),
            )
        return self.context

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()


def save_screenshot(page: Page, directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    page.screenshot(path=str(path), full_page=True, timeout=8000)
    return path


def first_visible_text(page: Page, texts: list[str], timeout_ms: int = 1200):
    for text in texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:
            continue
    return None


def first_visible_overlay_text(page: Page, texts: list[str], timeout_ms: int = 250):
    overlay_roots = [
        ".adm-center-popup",
        ".adm-center-popup-wrap",
        ".adm-popup",
        ".adm-popup-body",
        ".adm-modal",
        ".adm-picker",
        ".adm-action-sheet",
    ]
    for root in overlay_roots:
        container = page.locator(root).filter(has=page.locator(":visible")).last
        try:
            container.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            continue
        for text in texts:
            locator = container.get_by_text(text, exact=False).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
    return None


def has_active_overlay(page: Page) -> bool:
    try:
        return page.locator(".adm-mask:visible").count() > 0
    except Exception:
        return False


def dismiss_toast(page: Page) -> None:
    try:
        page.locator(".adm-toast-mask:visible, .adm-toast-wrap:visible").first.wait_for(
            state="hidden", timeout=600
        )
    except Exception:
        pass


def click_selector_or_text(
    page: Page,
    selector: str | None,
    texts: list[str],
    timeout_ms: int = 1500,
) -> bool:
    dismiss_toast(page)
    if selector:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click()
            return True
        except Exception:
            return False

    if has_active_overlay(page):
        locator = first_visible_overlay_text(page, texts)
        if locator:
            try:
                locator.click(timeout=timeout_ms)
                return True
            except TimeoutError:
                try:
                    locator.click(timeout=timeout_ms, force=True)
                    return True
                except Exception:
                    return False

    locator = first_visible_text(page, texts, timeout_ms)
    if not locator:
        return False
    try:
        locator.click(timeout=timeout_ms)
        return True
    except TimeoutError:
        overlay_locator = first_visible_overlay_text(page, texts)
        if overlay_locator:
            try:
                overlay_locator.click(timeout=timeout_ms)
                return True
            except Exception:
                return False
        try:
            locator.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            return False


def page_contains_any(page: Page, texts: list[str]) -> bool:
    content = page.locator("body").inner_text(timeout=3000)
    return any(text in content for text in texts)
