"""Quota probe dispatcher — every provider probed concurrently, contained.

`probe_all` is the package's entry point and the home of the Probe Contract's
containment invariant: one provider's failure — a raised exception, an import
error, a wedged connection — becomes that provider's own `error` account and
touches nothing else. The caller (the API layer) owns the TTL and the upsert;
it never has to reason about a partial result.
"""
from __future__ import annotations

import concurrent.futures
import time

from . import PROVIDERS, account, now_iso

DEFAULT_TIMEOUT = 5.0
# The probes pass `timeout` to their own HTTP calls; this grace covers a probe
# that wedges somewhere OTHER than the socket, so a hung provider still costs
# the caller a bounded wait rather than the request.
WALL_CLOCK_GRACE = 1.0


def _error(provider: str, detail: str) -> dict:
    return account(provider=provider, probe_version="0", captured_at=now_iso(),
                   status="error", detail=detail, is_current=0)


def _run(name: str, log, timeout: float) -> list[dict]:
    try:
        mod = __import__(f"quota_probes.{name}", fromlist=[name])
    except ImportError as e:
        log(f"quota_probes: no probe for '{name}' ({e}) — skipped")
        return [_error(name, f"probe module unavailable: {e}")]
    try:
        return list(mod.probe(log, timeout))
    except Exception as e:  # plugin stance: loud, never fatal to the sweep
        # TYPE only, never the message — matching what `_error` already puts in
        # `detail`. Any exception's message can carry a token (an HTTP library
        # that renders the request headers, or http.client's own "Invalid
        # header value <token>"), and these notes are echoed into the API
        # response by the caller. The type cannot carry one. Flag #195.
        log(f"quota_probes: {name} probe failed: {type(e).__name__}")
        return [_error(name, f"probe raised {type(e).__name__}")]


def probe_all(log, timeout: float = DEFAULT_TIMEOUT,
              providers: "list[str] | None" = None) -> list[dict]:
    """Probe every provider concurrently; return their accounts in a stable
    provider order. Never raises."""
    names = list(providers or PROVIDERS)
    results: dict[str, list[dict]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(names) or 1)
    try:
        futures = {name: pool.submit(_run, name, log, timeout) for name in names}
        # ONE deadline for the whole fan-out, not one per future: waiting on
        # each in turn with its own timeout would let three slow providers cost
        # three timeouts, when they have been running concurrently all along.
        deadline = time.monotonic() + timeout + WALL_CLOCK_GRACE
        for name, future in futures.items():
            try:
                results[name] = future.result(
                    timeout=max(0.0, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError:
                log(f"quota_probes: {name} probe timed out after {timeout}s")
                results[name] = [_error(name, f"timed out after {timeout}s")]
    finally:
        # NOT a `with` block: its exit joins every worker, which would re-block
        # the caller on the exact probe this timeout exists to contain. The
        # abandoned thread ends on its own socket timeout.
        pool.shutdown(wait=False)
    return [row for name in names for row in results.get(name, [])]
