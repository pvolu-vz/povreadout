#!/usr/bin/env python3
"""Headless screenshot worker for one named agent (dashboard / risk / governance / ...).

Reads URLs from config.json and reuses an authenticated session stored as
Playwright storage_state (produced by login.py with LOGIN_STORAGE_STATE set).

Usage:
    capture.py --agent dashboard
    capture.py --agent risk --config /path/to/config.json --out /path/to/screenshots
    capture.py --agent risk --site pov-f   # override the site folder

Exits non-zero if the storage_state file is missing or the agent has no URLs.
Each URL produces one full-page PNG named <slug>.png in <out>/<site>/<agent>/,
so captures from different sites stay isolated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright


DEFAULT_CONFIG = Path(os.path.expanduser("~/.povreadout/config.json"))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-")
    return s or "page"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"config not found: {path}")
    with path.open() as f:
        return json.load(f)


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="agent name (must match a key in config.agents)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out", default=None, help="screenshot output directory (overrides config)")
    ap.add_argument("--site", default=None, help="site folder name to nest screenshots under (overrides config.site)")
    ap.add_argument("--storage-state", default=None, help="path to Playwright storage_state JSON (overrides config)")
    ap.add_argument("--timeout-ms", type=int, default=30_000)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    agents = cfg.get("agents", {})
    if args.agent not in agents:
        sys.exit(f"agent {args.agent!r} not in config; available: {sorted(agents)}")

    urls = agents[args.agent].get("urls", [])
    if not urls:
        sys.exit(f"agent {args.agent!r} has no urls in config")

    storage_state = expand(args.storage_state or cfg.get("storage_state_path", ""))
    if not storage_state or not storage_state.exists():
        sys.exit(f"storage_state not found at {storage_state}; run login.py with LOGIN_STORAGE_STATE first")

    out_root = expand(args.out or cfg.get("screenshot_dir", "~/.povreadout/screenshots"))
    site = args.site or cfg.get("site")
    if not site:
        sys.exit("missing 'site' in config (or --site flag); needed to keep screenshots from different sites separated")
    out_dir = out_root / slugify(site) / args.agent
    out_dir.mkdir(parents=True, exist_ok=True)

    viewport = cfg.get("viewport", {"width": 1440, "height": 900})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(storage_state), viewport=viewport)
        page = ctx.new_page()

        ok = 0
        fail = 0
        for entry in urls:
            name = entry.get("name") or entry["url"]
            url = entry["url"]
            target = out_dir / f"{slugify(name)}.png"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=args.timeout_ms)
                except PWTimeout:
                    pass
                page.screenshot(path=str(target), full_page=True)
                print(f"site={site} agent={args.agent} url={url} status=ok file={target}")
                ok += 1
            except Exception as e:
                print(f"site={site} agent={args.agent} url={url} status=error err={type(e).__name__}: {e}")
                fail += 1

        browser.close()

    print(f"site={site} agent={args.agent} done ok={ok} fail={fail} out={out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
