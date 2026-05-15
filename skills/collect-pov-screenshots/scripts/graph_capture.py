#!/usr/bin/env python3
"""Drive Veza Query Builder, click 'Open in Graph', screenshot the new tab.

Reuses the dropdown-select pipeline from query_builder_capture.py:
  - pick Entity Type
  - pick Relates To (optionally with network-level filter injection)
  - wait for the table results

Then clicks the [data-testid="cqb-open-in-pg"] button, switches to the new
tab Veza opens for the graph view, waits for the graph to settle, and
writes a full-page PNG.

Pairs are read from config.json under "query_builder.graph_pairs" (same
shape as "query_builder.pairs"). Keeping them in a separate list lets the
graph-capture worker run alongside the table-capture worker without
duplicating screenshots.

Usage:
    graph_capture.py                                  # all graph_pairs
    graph_capture.py --pair azure-ad-license-by-user  # one pair
    graph_capture.py --entity-type AzureADLicense \
                     --relates-to AzureADUser \
                     --name license-by-user           # ad-hoc

Debug:
    QB_HEADED=1   launch a visible browser
    QB_DEBUG_DIR=/tmp/qb-debug   snapshot after every step
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import (
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

# Reuse helpers from the sibling table-capture script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from query_builder_capture import (  # noqa: E402
    ASYNC_GET_PATH,
    DEFAULT_CONFIG,
    expand,
    load_config,
    setup_pair_query,
    slugify,
    viewport_center_x,
)


OPEN_IN_GRAPH_SELECTOR = '[data-testid="cqb-open-in-pg"]'


def await_graph_ready(page: Page, timeout_ms: int, snap) -> None:
    """Wait until the graph view on the new tab looks done rendering.

    No confirmed API endpoint signals 'graph data ready' (the HAR for a
    real run showed only generic page-load calls — providers/system/
    telemetry — no query_spec or graph data fetch). So we use a layered
    approach: opportunistically catch any nodes_async_get that fires,
    fall through to networkidle, then wait for a canvas/svg/graph
    container to appear, then a small settle delay so the layout
    stabilises before the screenshot.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PWTimeout:
        pass
    snap("05-graph-dom-ready")

    # Opportunistic: if the new tab does fire a query_spec poll, give it a
    # chance to complete. Short timeout so we don't block when the graph
    # is hydrated from cached state instead.
    try:
        page.wait_for_event(
            "response",
            predicate=lambda r: ASYNC_GET_PATH in r.url and r.status == 200,
            timeout=min(timeout_ms, 8_000),
        )
    except PWTimeout:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        pass

    # Graph libraries (Cytoscape / D3 / visx) render into canvas or svg.
    # Try a sequence of likely targets; first hit wins, missing all of
    # them is non-fatal — the settle delay below still buys time.
    candidates = [
        '[data-testid*="graph" i] canvas',
        '[data-testid*="graph" i] svg',
        '[class*="cytoscape" i] canvas',
        '[class*="graph" i] canvas',
        '[class*="graph" i] svg',
        'main canvas',
        'main svg',
    ]
    per_timeout = max(500, timeout_ms // (len(candidates) * 2))
    for sel in candidates:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_timeout)
            break
        except PWTimeout:
            continue

    # Final settle: graph layouts often animate for a beat after first paint.
    page.wait_for_timeout(2500)
    snap("06-graph-settled")


def capture_graph_pair(
    ctx,
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
        page, url, entity_type, relates_to, timeout_ms, snap,
        relates_to_conditions=relates_to_conditions,
    )

    # Close any lingering dropdown popup so it doesn't cover the click target.
    page.keyboard.press("Escape")
    try:
        page.mouse.click(viewport_center_x(page), 5)
    except Exception:
        pass
    page.wait_for_timeout(400)

    # The 'Open in Graph' button opens a new browser tab. Use the context's
    # expect_page so we catch the popup regardless of target="_blank" vs
    # window.open() vs router.push to a new tab.
    button = page.locator(OPEN_IN_GRAPH_SELECTOR).first
    button.wait_for(state="visible", timeout=timeout_ms)
    with ctx.expect_page(timeout=timeout_ms) as new_page_info:
        button.click()
    graph_page = new_page_info.value
    graph_page.bring_to_front()

    def graph_snap(stage: str) -> None:
        # snap() in the caller is bound to the query-builder page, so we
        # capture the graph tab directly here. Only fires when QB_DEBUG_DIR
        # is set.
        debug_dir = os.environ.get("QB_DEBUG_DIR")
        if not debug_dir:
            return
        try:
            graph_page.screenshot(
                path=os.path.join(
                    os.path.expanduser(debug_dir),
                    f"graph-{stage}.png",
                ),
                full_page=True,
            )
        except Exception as e:
            print(f"graph_debug_snap stage={stage} err={type(e).__name__}: {e}")

    await_graph_ready(graph_page, timeout_ms, graph_snap)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    graph_page.screenshot(path=str(out_path), full_page=True)
    print(f"screenshot={out_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument(
        "--pair",
        help="run a single named pair from config.query_builder.graph_pairs",
    )
    ap.add_argument("--entity-type", help="internal node type for Entity Type")
    ap.add_argument("--relates-to", help="internal node type for Relates To")
    ap.add_argument(
        "--name",
        help="output basename when using --entity-type/--relates-to "
        "(default: <entity>-by-<relates>)",
    )
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
        pairs = [
            {
                "name": name,
                "entity_type": args.entity_type,
                "relates_to": args.relates_to,
            }
        ]
    elif args.entity_type or args.relates_to:
        sys.exit("--entity-type and --relates-to must be passed together")
    else:
        pairs = qb.get("graph_pairs", [])
        if args.pair:
            pairs = [p for p in pairs if p.get("name") == args.pair]
            if not pairs:
                sys.exit(
                    f"pair {args.pair!r} not found in config.query_builder.graph_pairs"
                )
        if not pairs:
            sys.exit(
                "no pairs to run; populate config.query_builder.graph_pairs "
                "or pass --entity-type/--relates-to"
            )

    storage_state = expand(args.storage_state or cfg.get("storage_state_path", ""))
    if not storage_state or not storage_state.exists():
        sys.exit(
            f"storage_state not found at {storage_state}; "
            "run login.py with LOGIN_STORAGE_STATE first"
        )

    out_root = expand(
        args.out
        or cfg.get(
            "screenshot_dir",
            "~/.povreadout/screenshots",
        )
    )
    site = args.site or cfg.get("site")
    if not site:
        sys.exit("missing 'site' in config (or --site)")
    out_dir = out_root / slugify(site) / "query-builder-graph"

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
                        path=os.path.join(
                            debug_dir, f"graph-{slugify(_name)}-{stage}.png"
                        ),
                        full_page=True,
                    )
                except Exception as e:
                    print(
                        f"debug_snap pair={_name} stage={stage} "
                        f"err={type(e).__name__}: {e}"
                    )

            try:
                capture_graph_pair(
                    ctx, page, url, entity, relates, target, args.timeout_ms, snap,
                    relates_to_conditions=pair.get("relates_to_conditions"),
                )
                print(
                    f"site={site} graph_pair={name} entity={entity} "
                    f"relates_to={relates} status=ok file={target}"
                )
                ok += 1
            except Exception as e:
                print(
                    f"site={site} graph_pair={name} entity={entity} "
                    f"relates_to={relates} status=error "
                    f"err={type(e).__name__}: {e}"
                )
                snap("fail")
                fail += 1
            finally:
                # Close every page in the context (the qb page + the graph
                # tab opened by 'Open in Graph') so the next pair starts
                # from a clean slate.
                for pg in list(ctx.pages):
                    try:
                        pg.close()
                    except Exception:
                        pass

        browser.close()

    print(f"site={site} query-builder-graph done ok={ok} fail={fail} out={out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
