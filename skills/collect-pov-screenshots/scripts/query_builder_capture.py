#!/usr/bin/env python3
"""Drive the Veza Query Builder UI and capture full-page screenshots.

Reuses an authenticated Playwright session (storage_state.json produced by
login.py) to navigate to /app/query-builder, select an Entity Type and a
Relates To value from the two dropdowns, wait for the result table to load,
and write a full-page PNG.

Wait condition is derived from the HAR capture of a real session: the page
fires POST /api/private/assessments/query_spec:nodes_async_create followed
by POST /api/private/assessments/query_spec:nodes_async_get; the second
response carries {"status": "DONE", ...} when the table is ready to render.
We wait for that exact response instead of relying on "networkidle", which
is fragile on a busy SPA.

Pairs are read from config.json under "query_builder.pairs"; each pair uses
the *internal* node type identifiers (e.g. AzureADLicense, AzureADUser) so
the values can be copied straight out of a DevTools HAR.

Usage:
    query_builder_capture.py                                    # all pairs
    query_builder_capture.py --pair azure-ad-license-by-user    # one pair
    query_builder_capture.py --entity-type AzureADLicense \
                             --relates-to AzureADUser \
                             --name license-by-user             # ad-hoc

Debug:
    QB_HEADED=1   launch a visible browser
    QB_DEBUG_DIR=/tmp/qb-debug   snapshot after every step
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import (
    Page,
    Locator,
    Route,
    TimeoutError as PWTimeout,
    sync_playwright,
)


DEFAULT_CONFIG = Path(os.path.expanduser("~/.povreadout/config.json"))

ASYNC_GET_PATH = "/api/private/assessments/query_spec:nodes_async_get"
ASYNC_CREATE_PATH = "/api/private/assessments/query_spec:nodes_async_create"
CONNECTIONS_SEARCH_PATH = "/graph/private/schema/connections/search"


def slugify(s: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", s.strip()).strip("-")
    return out or "page"


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"config not found: {path}")
    with path.open() as f:
        return json.load(f)


def open_dropdown(page: Page, label: str, timeout_ms: int) -> Locator:
    """Locate the combobox associated with a field label and click to open it.

    Tries accessibility-anchored selectors first (most stable across UI
    library upgrades), then falls back to text-anchored DOM walking.
    Returns the input locator so the caller can type into it.
    """
    candidates = [
        # MUI / Mantine / common patterns: input or combobox wired to a label
        lambda: page.get_by_label(label, exact=True),
        # Anchor by visible label text, then find the nearest combobox/input
        lambda: page.locator(
            f'xpath=//*[normalize-space(text())="{label}"]/'
            'following::*[self::input or @role="combobox" or @role="button"][1]'
        ),
        # Fallback: a clickable element directly after a label span
        lambda: page.locator(
            f'xpath=//*[normalize-space(text())="{label}"]/following::*[1]'
        ),
    ]
    last_err: Exception | None = None
    for build in candidates:
        try:
            loc = build().first
            loc.wait_for(state="visible", timeout=timeout_ms // len(candidates))
            loc.click()
            return loc
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"could not open dropdown for label={label!r}: {last_err}")


def to_display_name(node_type: str) -> str:
    """Convert an internal CamelCase node_type to the human label shown in the
    dropdown: insert a space before a capital that follows a lowercase letter,
    OR before a capital that follows another capital iff the next character is
    lowercase. Keeps acronyms intact:

        AzureADLicense -> Azure AD License
        AzureADUser    -> Azure AD User
        OktaUser       -> Okta User
        ActiveDirectoryManagedServiceAccount -> Active Directory Managed Service Account
    """
    out = []
    for i, ch in enumerate(node_type):
        if i == 0:
            out.append(ch)
            continue
        prev = node_type[i - 1]
        nxt = node_type[i + 1] if i + 1 < len(node_type) else ""
        if ch.isupper() and (prev.islower() or (prev.isupper() and nxt.islower())):
            out.append(" ")
        out.append(ch)
    return "".join(out)


def pick_option(page: Page, node_type: str, timeout_ms: int) -> None:
    """Pick an option from an open Veza dropdown.

    Veza uses internal identifiers (e.g. AzureADLicense) in the API payload
    but the dropdown's typeahead searches against display labels (e.g.
    "Azure AD License"); typing the raw internal id returns "No results
    found". Also, Veza renders options as plain divs (not role="option"),
    so ARIA-based selectors miss. Strategy: type the display name, give the
    typeahead a beat to filter, then press ArrowDown + Enter to commit the
    first match. Falls back to clicking the text inside the popup.
    """
    display = to_display_name(node_type)
    page.keyboard.type(display, delay=20)
    # Let the typeahead filter and render the popup.
    page.wait_for_timeout(400)

    # Primary strategy: keyboard navigation. Works regardless of DOM/ARIA.
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(100)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)

    # If the keyboard path did not commit (popup still visible), fall back
    # to clicking the text in the popup. Scope to visible elements to skip
    # the option text that lives inside the input field itself.
    try:
        popup_options = page.get_by_text(display, exact=True)
        # If any of these are still visible after Enter, the commit failed.
        if popup_options.count() > 0:
            for i in range(popup_options.count()):
                el = popup_options.nth(i)
                try:
                    if el.is_visible(timeout=200):
                        bbox = el.bounding_box()
                        # Skip the input field itself (it sits near the top
                        # of the label section; clickable options live below
                        # it in the popup).
                        if bbox and bbox["y"] > 50:
                            el.click()
                            return
                except Exception:
                    continue
    except Exception:
        pass


def clear_existing_chips(page: Page, section_label: str) -> None:
    """If the 'Relates To' field already has selections, click their X buttons.

    The Veza chip uses a small 'x' button per chip; clicking each one before
    we pick a new value avoids ending up with multiple Relates To types.
    Failures here are non-fatal — if the field is already empty we just
    proceed.
    """
    try:
        section = page.locator(
            f'xpath=//*[normalize-space(text())="{section_label}"]/following::*[1]'
        ).first
        # Each chip typically has a removal button with aria-label like
        # "remove" or an X icon button. Click any/all of them.
        for sel in [
            'button[aria-label*="remove" i]',
            'button[aria-label*="clear" i]',
            '[data-testid*="remove" i]',
            'svg[role="button"]',
        ]:
            buttons = section.locator(sel)
            n = buttons.count()
            for i in range(n):
                try:
                    buttons.nth(0).click(timeout=500)
                except Exception:
                    break
    except Exception:
        pass


def install_filter_injector(page: Page, relates_to_conditions: dict | None) -> None:
    """Install a network route that rewrites `nodes_async_create` requests
    to inject a `condition_expression` onto the Relates To node.

    The UI itself doesn't know about the injected filter, so the left-panel
    Filters section will not show chips — but the result table on the right
    renders the filtered rows correctly. Use this when you want the result
    payload without writing UI selectors for the filter dialog.

    `relates_to_conditions` is the raw `condition_expression` object copied
    out of a HAR (or hand-written). When None, this function is a no-op.
    """
    if not relates_to_conditions:
        return

    def handler(route: Route) -> None:
        req = route.request
        if req.method != "POST" or "nodes_async_create" not in req.url:
            route.continue_()
            return
        try:
            body = json.loads(req.post_data or "{}")
            spec = body["cached_result_if_available"]["request"]["body"]
            nodes = spec.get("relates_to_exp", {}).get("specs", [{}])[0].get(
                "node_types", {}
            ).get("nodes", [])
            if not nodes:
                # No Relates To set yet (the UI fires an unfiltered create
                # for entity-only views too); leave it alone.
                route.continue_()
                return
            nodes[0]["condition_expression"] = relates_to_conditions
            route.continue_(post_data=json.dumps(body))
        except Exception as e:
            print(f"filter_injector: skipping rewrite err={type(e).__name__}: {e}")
            route.continue_()

    page.route(
        "**/api/private/assessments/query_spec:nodes_async_create",
        handler,
    )


def setup_pair_query(
    page: Page,
    url: str,
    entity_type: str,
    relates_to: str,
    timeout_ms: int,
    snap,
    relates_to_conditions: dict | None = None,
) -> None:
    """Navigate to the Query Builder, set Entity Type + Relates To, and wait
    for the result table to load. Leaves the page on the loaded results view
    so the caller can decide what to do next (screenshot, click Open in
    Graph, etc).
    """
    install_filter_injector(page, relates_to_conditions)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        pass
    snap("01-loaded")

    # Step 1: Entity Type. Wait for the schema/connections/search response
    # afterwards because that means the "Relates To" options have been
    # populated by the backend — clicking before that races the UI.
    with page.expect_response(
        lambda r: CONNECTIONS_SEARCH_PATH in r.url and r.status == 200,
        timeout=timeout_ms,
    ):
        open_dropdown(page, "Entity Type", timeout_ms)
        pick_option(page, entity_type, timeout_ms)
    snap("02-entity-type-set")

    # Step 2: Relates To. Drop any pre-existing chip first.
    clear_existing_chips(page, "Relates To")
    snap("03-relates-cleared")

    # Picking Relates To triggers nodes_async_create -> nodes_async_get.
    # When a filter is injected via page.route, the *first* async_get often
    # returns the cached unfiltered result (status DONE) and then the page
    # polls again ~3s later for the freshly-computed filtered result. So we
    # accumulate every async_get response and report the latest one we saw.
    seen_responses: list[dict] = []

    def on_response(resp):
        if ASYNC_GET_PATH not in resp.url or resp.status != 200:
            return
        try:
            seen_responses.append(resp.json())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        with page.expect_response(
            lambda r: ASYNC_GET_PATH in r.url and r.status == 200,
            timeout=timeout_ms * 2,
        ):
            open_dropdown(page, "Relates To", timeout_ms)
            pick_option(page, relates_to, timeout_ms)
        # Give the page time to issue any follow-up poll for the real result.
        page.wait_for_timeout(3500)
    finally:
        page.remove_listener("response", on_response)

    if seen_responses:
        latest = seen_responses[-1]
        rows = latest.get("progress_status", {}).get("row_count", "?")
        print(f"query_status={latest.get('status')} row_count={rows} polls={len(seen_responses)}")
    snap("04-results-loaded")


def capture_pair(
    page: Page,
    url: str,
    entity_type: str,
    relates_to: str,
    out_path: Path,
    timeout_ms: int,
    snap,
    relates_to_conditions: dict | None = None,
) -> None:
    setup_pair_query(
        page, url, entity_type, relates_to, timeout_ms, snap, relates_to_conditions
    )

    # Close any lingering dropdown popup so it doesn't appear in the shot.
    page.keyboard.press("Escape")
    # Move focus to a neutral area so the input doesn't show focus styling.
    try:
        page.mouse.click(viewport_center_x(page), 5)
    except Exception:
        pass
    # Small settle delay so chart/table animations finish before the shot.
    page.wait_for_timeout(800)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out_path), full_page=True)
    print(f"screenshot={out_path}")


def viewport_center_x(page: Page) -> int:
    vp = page.viewport_size or {"width": 1440}
    return vp["width"] // 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--pair", help="run a single named pair from config.query_builder.pairs")
    ap.add_argument("--entity-type", help="internal node type for the Entity Type field (e.g. AzureADLicense); requires --relates-to")
    ap.add_argument("--relates-to", help="internal node type for the Relates To field (e.g. AzureADUser); requires --entity-type")
    ap.add_argument("--name", help="output basename when using --entity-type/--relates-to (default: <entity>-by-<relates>)")
    ap.add_argument("--url", help="override the query-builder URL")
    ap.add_argument("--out", help="override screenshot output directory")
    ap.add_argument("--site", help="override site folder name")
    ap.add_argument("--storage-state", help="override path to storage_state.json")
    ap.add_argument("--timeout-ms", type=int, default=30_000)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    qb = cfg.get("query_builder", {})
    url = args.url or qb.get("url")
    if not url:
        sys.exit("missing query_builder.url in config (or pass --url)")

    if args.entity_type and args.relates_to:
        name = args.name or f"{slugify(args.entity_type)}-by-{slugify(args.relates_to)}"
        pairs = [{"name": name, "entity_type": args.entity_type, "relates_to": args.relates_to}]
    elif args.entity_type or args.relates_to:
        sys.exit("--entity-type and --relates-to must be passed together")
    else:
        pairs = qb.get("pairs", [])
        if args.pair:
            pairs = [p for p in pairs if p.get("name") == args.pair]
            if not pairs:
                sys.exit(f"pair {args.pair!r} not found in config.query_builder.pairs")
        if not pairs:
            sys.exit("no pairs to run; populate config.query_builder.pairs or pass --entity-type/--relates-to")

    storage_state = expand(args.storage_state or cfg.get("storage_state_path", ""))
    if not storage_state or not storage_state.exists():
        sys.exit(f"storage_state not found at {storage_state}; run login.py with LOGIN_STORAGE_STATE first")

    out_root = expand(args.out or cfg.get("screenshot_dir", "~/.povreadout/screenshots"))
    site = args.site or cfg.get("site")
    if not site:
        sys.exit("missing 'site' in config (or --site)")
    out_dir = out_root / slugify(site) / "query-builder"

    viewport = cfg.get("viewport", {"width": 1440, "height": 900})
    headed = os.environ.get("QB_HEADED") == "1"
    debug_dir = os.environ.get("QB_DEBUG_DIR")
    if debug_dir:
        debug_dir = os.path.expanduser(debug_dir)
        os.makedirs(debug_dir, exist_ok=True)

    ok = 0
    fail = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(storage_state=str(storage_state), viewport=viewport)

        for pair in pairs:
            # Fresh page per pair so installed page.route handlers from a
            # filtered pair do not leak onto a later unfiltered pair.
            page = ctx.new_page()
            name = pair["name"]
            entity = pair["entity_type"]
            relates = pair["relates_to"]
            target = out_dir / f"{slugify(name)}.png"

            def snap(stage: str, _name=name) -> None:
                if not debug_dir:
                    return
                try:
                    page.screenshot(
                        path=os.path.join(debug_dir, f"{slugify(_name)}-{stage}.png"),
                        full_page=True,
                    )
                except Exception as e:
                    print(f"debug_snap pair={_name} stage={stage} err={type(e).__name__}: {e}")

            try:
                capture_pair(
                    page, url, entity, relates, target, args.timeout_ms, snap,
                    relates_to_conditions=pair.get("relates_to_conditions"),
                )
                print(f"site={site} pair={name} entity={entity} relates_to={relates} status=ok file={target}")
                ok += 1
            except Exception as e:
                print(f"site={site} pair={name} entity={entity} relates_to={relates} status=error err={type(e).__name__}: {e}")
                snap("fail")
                fail += 1
            finally:
                page.close()

        browser.close()

    print(f"site={site} query-builder done ok={ok} fail={fail} out={out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
