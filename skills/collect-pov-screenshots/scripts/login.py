#!/usr/bin/env python3
"""Drive a website login with Playwright using credentials passed via env.

Reads these environment variables (the caller is responsible for setting them
without exposing them to other processes):

    LOGIN_URL             page to log in to
    LOGIN_USERNAME        value for the email/username field
    LOGIN_PASSWORD        value for the password field
    LOGIN_TOTP            current 6-digit TOTP code
    LOGIN_STORAGE_STATE   (optional) path to write Playwright storage_state JSON
                          after successful login, so headless workers can reuse
                          the authenticated session
    LOGIN_HEADED          (optional) set to "1" to launch a visible browser for
                          debugging; default is headless
    LOGIN_DEBUG_DIR       (optional) path to a directory; when set, a debug
                          screenshot is captured after every step so the
                          flow can be reconstructed without watching it live

Never prints these values. On exit, prints only the final URL for confirmation.

Heuristics handle both single-page and multi-step (username -> password ->
TOTP) login flows. If a field cannot be located: in headed mode the browser
pauses on page.pause() so the operator can finish manually; in headless mode
the script exits non-zero so the failure is loud rather than silent.

The script also verifies success by waiting for the URL to leave the login
page after TOTP submission; if it stays on a /login* path the run fails so
the storage_state is not silently overwritten with an unauthenticated session.
"""

from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright


USERNAME_SELECTORS = [
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
    'input[name*="user" i]',
    'input[id*="user" i]',
    'input[type="text"]:visible',
]

PASSWORD_SELECTOR = 'input[type="password"]'

TOTP_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="totp" i]',
    'input[id*="totp" i]',
    'input[name*="mfa" i]',
    'input[id*="mfa" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[inputmode="numeric"]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]:visible',
    'input[type="submit"]:visible',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Verify")',
    'button:has-text("Submit")',
]


def find_and_fill(page: Page, selectors: list[str], value: str, *, timeout_ms: int) -> str | None:
    """Try each selector in order; fill the first visible match. Returns the selector or None."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.is_visible(timeout=200):
                    loc.fill(value)
                    return sel
            except PWTimeout:
                continue
            except Exception:
                continue
        time.sleep(0.25)
    return None


def submit(page: Page) -> str:
    for sel in SUBMIT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=200):
                loc.click()
                return sel
        except Exception:
            continue
    page.keyboard.press("Enter")
    return "Enter"


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env: {name}")
    return v


def main() -> int:
    url = require_env("LOGIN_URL")
    username = require_env("LOGIN_USERNAME")
    password = require_env("LOGIN_PASSWORD")
    totp = require_env("LOGIN_TOTP")

    headed = os.environ.get("LOGIN_HEADED") == "1"
    debug_dir = os.environ.get("LOGIN_DEBUG_DIR")
    if debug_dir:
        debug_dir = os.path.expanduser(debug_dir)
        os.makedirs(debug_dir, exist_ok=True)

    def snap(stage: str) -> None:
        if not debug_dir:
            return
        try:
            page.screenshot(path=os.path.join(debug_dir, f"{stage}.png"), full_page=True)
        except Exception as e:
            print(f"debug_snap stage={stage} err={type(e).__name__}: {e}")

    def stuck(stage: str, msg: str) -> None:
        """Fail loudly in headless; pause for manual completion in headed mode."""
        print(f"step={stage} status={msg} url={page.url}")
        snap(f"fail-{stage}")
        if headed:
            page.pause()
        else:
            browser.close()
            sys.exit(f"login aborted: {stage} {msg}; set LOGIN_DEBUG_DIR=<dir> for screenshots, or LOGIN_HEADED=1 to debug interactively")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        print(f"step=loaded url={page.url}")
        snap("01-loaded")

        sel = find_and_fill(page, USERNAME_SELECTORS, username, timeout_ms=10_000)
        if not sel:
            stuck("username", "not_found")
        else:
            print(f"step=username status=filled selector={sel}")

        # If password field is not already visible (i.e. multi-step), submit username first.
        try:
            page.locator(PASSWORD_SELECTOR).first.wait_for(state="visible", timeout=1500)
        except PWTimeout:
            used = submit(page)
            print(f"step=username_submit via={used} url={page.url}")
            snap("02-after-username-submit")
            try:
                page.locator(PASSWORD_SELECTOR).first.wait_for(state="visible", timeout=15_000)
            except PWTimeout:
                stuck("password", "field_never_appeared")

        page.locator(PASSWORD_SELECTOR).first.fill(password)
        print("step=password status=filled")
        used = submit(page)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        print(f"step=password_submit via={used} url={page.url}")
        snap("03-after-password-submit")

        sel = find_and_fill(page, TOTP_SELECTORS, totp, timeout_ms=15_000)
        if not sel:
            stuck("totp", "not_found")
        else:
            print(f"step=totp status=filled selector={sel}")
            used = submit(page)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass
            print(f"step=totp_submit via={used} url={page.url}")
            snap("04-after-totp-submit")

        # Verify success by waiting for navigation off the login page. If the
        # URL still contains "/login" after a generous timeout, the flow did
        # not actually authenticate (form rejected, MFA enrollment screen,
        # IdP redirect that stalled, etc.) — fail loudly instead of saving
        # an unauthenticated storage_state.
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
        except PWTimeout:
            print(f"step=verify status=still_on_login url={page.url}")
            snap("05-verify-failed")
            browser.close()
            sys.exit(
                "login verification failed: still on a /login* URL after TOTP submission. "
                "Set LOGIN_DEBUG_DIR=<dir> and rerun to capture per-step screenshots, "
                "or LOGIN_HEADED=1 to watch the flow."
            )
        print(f"step=verify status=ok url={page.url}")
        snap("05-verified")

        print(f"final_url={page.url}")

        storage_path = os.environ.get("LOGIN_STORAGE_STATE")
        if storage_path:
            storage_path = os.path.expanduser(storage_path)
            os.makedirs(os.path.dirname(storage_path) or ".", exist_ok=True)
            ctx.storage_state(path=storage_path)
            os.chmod(storage_path, 0o600)
            print(f"storage_state=saved path={storage_path}")

        print("login flow complete")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
