#!/usr/bin/env python3
"""Capture the real disposable documentation GUI with the existing visual-QA seam.

Provision and seed the dedicated dos-app instance first. This reads live UI;
it never replaces API responses or fabricates conversation/PR evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.super-coder' / 'scripts'))
from visual_qa import PlaywrightCapture, capture_gallery, write_gallery


class DocumentationCapture(PlaywrightCapture):
    """Live Chats has an SSE stream, so wait for the UI instead of network idle."""

    def capture(self, url, viewport, output, *, settle_ms, timeout_ms):
        page = self._browser.new_page(
            viewport={"width": viewport["width"], "height": viewport["height"]}
        )
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            tab = url.split("#", 1)[1].split("-", 1)[0]
            page.locator(f'nav button[data-tab="{tab}"].active').wait_for(timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=True)
            return {
                "ok": response is not None and response.status == 200,
                "status": response.status if response else None,
                "error": None,
                "image_width": viewport["width"],
                "image_height": page.evaluate("document.documentElement.scrollHeight"),
                "image_written": True,
            }
        finally:
            page.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base = f'http://127.0.0.1:{args.port}'
    with urllib.request.urlopen(base + '/api/health', timeout=10) as response:
        health = json.load(response)
    if health.get('repo') != 'subfloor-docs-f73-dos-app':
        parser.error('capture requires the dedicated disposable documentation instance')
    config = {
        'routes': ['/#roadmap', '/#roadmap-flow', '/#worktrees', '/#interface', '/#sprints'],
        'viewports': [{'name': 'desktop', 'width': 1440, 'height': 1000}],
        'settle_ms': 1800,
        'ready_timeout_s': 30,
    }
    summary = capture_gallery(config, base, args.output, capture_factory=DocumentationCapture)
    write_gallery(args.output, summary)
    (args.output / 'capture.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f"Captured {summary['routes_total']} routes; {summary['routes_failed']} failed")
    return 1 if summary['routes_failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
