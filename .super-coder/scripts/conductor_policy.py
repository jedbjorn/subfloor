"""Non-negotiable launch policy for the ephemeral Conductor shell."""

CONDUCTOR_FLAVOR = "conductor"
CONDUCTOR_HARNESS = "opencode"
DEFAULT_CONDUCTOR_MODEL = "openai/gpt-5.6-luna"


def require_harness(flavor: "str | None", harness: str) -> None:
    """Reject a Conductor launch outside its required OpenCode runtime."""
    if flavor == CONDUCTOR_FLAVOR and harness != CONDUCTOR_HARNESS:
        raise ValueError(
            f"conductor requires harness '{CONDUCTOR_HARNESS}'; "
            f"'{harness}' is unsupported"
        )
