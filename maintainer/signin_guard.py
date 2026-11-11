"""Stop a maintainer tool early when the install it drives requires sign-in.

The review server answers `/api/*` only to a signed-in browser (Feature #94).
The maintainer tools here play the operator over HTTP, or in a browser, with
no session, so a gated install refuses them: a raw 401 on the first operator
route, or a redirect to the sign-in page.

Whether a host-side tool may be given an operator session without the
password and the authenticator is the operator's decision, tracked in flag
SC-1602. Until it is made, these tools cannot run against an install that has
sign-in, and they say so up front instead of failing somewhere in the middle.

A gated install is recognised by its open route `GET /api/auth/status`, which
an engine from before sign-in does not have.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

FLAG = "SC-1602"


class SignInRequired(RuntimeError):
    """The install requires GUI sign-in and this tool has no session."""


def message(tool: str) -> str:
    return (
        f"{tool}: this install requires GUI sign-in, and the tool has no "
        "operator session to present. It cannot run until it is given one; "
        "whether and how a host-side tool gets a session is tracked in flag "
        f"{FLAG}. Nothing was changed."
    )


def requires_sign_in(base_url: str, *, opener=urllib.request.urlopen,
                     timeout: float = 10) -> bool:
    """Whether the review server at `base_url` gates its API behind sign-in.

    True when `GET /api/auth/status` answers with the sign-in status shape and
    the caller is not signed in. False for an engine that has no such route,
    and for anything that does not answer: a server that is down is some
    other step's failure to report.
    """
    try:
        with opener(base_url.rstrip("/") + "/api/auth/status",
                    timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return (isinstance(payload, dict) and "signed_in" in payload
            and "enrolled" in payload and payload.get("signed_in") is not True)


def refuse_if_sign_in_required(base_url: str, tool: str, *,
                               opener=urllib.request.urlopen) -> None:
    """Raise `SignInRequired`, with the one message, for a gated install."""
    if requires_sign_in(base_url, opener=opener):
        raise SignInRequired(message(tool))
