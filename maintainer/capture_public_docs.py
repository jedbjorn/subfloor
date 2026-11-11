#!/usr/bin/env python3
"""Capture the real disposable documentation GUI with the existing visual-QA seam.

Provision and seed the dedicated dos-app instance first. This reads live UI;
it never replaces API responses or fabricates conversation/PR evidence.

Sign-in (Feature #94): an instance with GUI sign-in hands a browser with no
session the sign-in page for every route. The capture therefore mints a
labelled operator session in the documentation instance's own checkout
(`--install`, through `tool_session.py`), gives the cookie to the capture
browser, and revokes the session when it ends, however it ends. The instance
is disposable and may have no GUI user: `--enrol-user <engine user>` lets the
tool create one there. The cookie is never written to the gallery.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAME = 'capture_public_docs'
DOCS_REPO = 'subfloor-docs-f73-dos-app'
# Where `public-documentation-refresh.md` provisions the instance.
DEFAULT_INSTALL = Path('/tmp') / DOCS_REPO
sys.path.insert(0, str(ROOT / '.super-coder' / 'scripts'))
sys.path.insert(0, str(ROOT / 'maintainer'))
import tool_session  # noqa: E402  (the operator session of a maintainer tool)
from visual_qa import PlaywrightCapture, capture_gallery, write_gallery


class DocumentationCapture(PlaywrightCapture):
    """Live Chats has an SSE stream, so wait for the UI instead of network idle.

    With a session, each page is opened in a browser context that holds its
    cookie. The page's own requests are same-origin, as the GUI's are.
    """

    def __init__(self, session=None):
        super().__init__()
        self._session = session

    def capture(self, url, viewport, output, *, settle_ms, timeout_ms):
        try:
            return self._capture(url, viewport, output, settle_ms=settle_ms,
                                 timeout_ms=timeout_ms)
        except Exception as exc:
            # The message is written into the gallery: never with the cookie.
            if self._session is None:
                raise
            raise RuntimeError(self._session.scrub(str(exc))) from None

    def _capture(self, url, viewport, output, *, settle_ms, timeout_ms):
        context = self._browser.new_context(
            viewport={"width": viewport["width"], "height": viewport["height"]}
        )
        try:
            cookies = self._session.browser_cookies() if self._session else []
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            tab = url.split("#", 1)[1].split("-", 1)[0]
            page.locator(f'nav button[data-tab="{tab}"].active').wait_for(timeout=timeout_ms)
            if tab == 'worktrees':
                page.locator('.wt-summary').wait_for(timeout=timeout_ms)
            elif url.endswith('#roadmap'):
                page.locator('.feature-title').filter(has_text='Reading list').click()
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
            context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument(
        '--install', type=Path, default=DEFAULT_INSTALL,
        help='checkout of the documentation instance; its ./sc mints the '
             'capture session when the instance has sign-in '
             f'(default: {DEFAULT_INSTALL})')
    parser.add_argument(
        '--enrol-user', metavar='ENGINE_USER',
        help='create the GUI user for this engine user when the instance '
             'has none (it is disposable); without it, a missing user is an error')
    args = parser.parse_args()
    base = f'http://127.0.0.1:{args.port}'
    with urllib.request.urlopen(base + '/api/health', timeout=10) as response:
        health = json.load(response)
    if health.get('repo') != DOCS_REPO:
        parser.error('capture requires the dedicated disposable documentation instance')
    try:
        with tool_session.operator_session(
                args.install, base, TOOL_NAME, enrol=args.enrol_user) as session:
            return capture(base, args.output, session)
    except tool_session.SessionUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2


def capture(base: str, output: Path, session) -> int:
    config = {
        'routes': ['/#roadmap', '/#roadmap-flow', '/#worktrees', '/#interface', '/#sprints'],
        'viewports': [{'name': 'desktop', 'width': 1440, 'height': 1000}],
        'settle_ms': 1800,
        'ready_timeout_s': 30,
    }
    summary = capture_gallery(
        config, base, output,
        capture_factory=lambda: DocumentationCapture(session))
    write_gallery(output, summary)
    (output / 'capture.json').write_text(
        session.scrub(json.dumps(summary, indent=2)) + '\n')
    print(f"Captured {summary['routes_total']} routes; {summary['routes_failed']} failed")
    return 1 if summary['routes_failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
