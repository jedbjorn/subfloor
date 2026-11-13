"""Per-harness compaction settings: the resolver and the saved row.

Feature #95, spec #275 "Settings". Each browser-chat harness that can be
compacted (``claude``, ``codex``, ``kimi``, ``opencode``) has two settings:

* **Auto compaction**: ``auto_enabled`` and ``trigger_tokens``. The engine
  compacts a chat at, or a little below, the trigger.
* **Idle compaction**: ``idle_after_minutes`` (``None`` is Off) and
  ``idle_min_tokens``. A large chat that has gone quiet is compacted before
  its provider's cache expires.

Precedence, per value: a saved row wins; with no saved row, the
``SC_CHAT_COMPACT_WINDOW_CLAUDE`` / ``_CODEX`` environment value supplies the
trigger (``off`` turns auto compaction off) for that harness only; otherwise
the engine default applies. The environment never touches idle compaction
or any other harness. Each resolved value names its source: ``saved``,
``environment`` or ``default``.

A save writes the whole row. The first save for a harness copies every value
it does not change from what was in effect (an environment-sourced trigger
included), so from then on the row alone decides and the environment is
ignored for that harness.

Nothing here runs a harness or asks for a compaction; the broker, the
reaper's idle step and the operator routes read these values.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import conversation_compaction
import db_driver

LOG = logging.getLogger(__name__)

TABLE = "chat_compaction_settings"
HARNESSES = ("claude", "codex", "kimi", "opencode")

SOURCE_SAVED = "saved"
SOURCE_ENVIRONMENT = "environment"
SOURCE_DEFAULT = "default"

FIELDS = (
    "auto_enabled",
    "trigger_tokens",
    "idle_after_minutes",
    "idle_min_tokens",
)

# Smallest trigger and idle size the operator may save: a lower value would
# compact a chat on nearly every turn.
MIN_TOKENS = 10_000

DEFAULT_TRIGGER_TOKENS = conversation_compaction.DEFAULT_COMPACT_WINDOW
DEFAULT_IDLE_MIN_TOKENS = 125_000
# FnB rulings, 2026-11-13: Claude 50, Codex 25, Kimi 25; OpenCode off.
DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "claude": {
        "auto_enabled": True,
        "trigger_tokens": DEFAULT_TRIGGER_TOKENS,
        "idle_after_minutes": 50,
        "idle_min_tokens": DEFAULT_IDLE_MIN_TOKENS,
    },
    "codex": {
        "auto_enabled": True,
        "trigger_tokens": DEFAULT_TRIGGER_TOKENS,
        "idle_after_minutes": 25,
        "idle_min_tokens": DEFAULT_IDLE_MIN_TOKENS,
    },
    "kimi": {
        "auto_enabled": True,
        "trigger_tokens": DEFAULT_TRIGGER_TOKENS,
        "idle_after_minutes": 25,
        "idle_min_tokens": DEFAULT_IDLE_MIN_TOKENS,
    },
    "opencode": {
        "auto_enabled": False,
        "trigger_tokens": DEFAULT_TRIGGER_TOKENS,
        "idle_after_minutes": None,
        "idle_min_tokens": DEFAULT_IDLE_MIN_TOKENS,
    },
}

# The provider cache life the idle wait must stay under, in minutes: a fixed
# engine value per harness from this host's measurements (spec #275). Shown
# on the card as a fact, never a setting. OpenCode fronts many providers and
# was not measured.
CACHE_LIFE_MINUTES: Mapping[str, int] = {
    "claude": 60,
    "codex": 45,
    "kimi": 30,
    "opencode": 60,
}
CACHE_LIFE_MEASURED: Mapping[str, bool] = {
    "claude": True,
    "codex": True,
    "kimi": True,
    "opencode": False,
}

# Dropdown waits. Each harness offers only the waits shorter than its cache
# life (Claude and OpenCode to 55, Codex to 40, Kimi to 25), plus Off.
IDLE_WAIT_STEPS = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)


def idle_choices(harness: str) -> tuple[int, ...]:
    """The idle waits ``harness`` may save, in minutes; Off is ``None``."""
    life = CACHE_LIFE_MINUTES.get(harness)
    if life is None:
        return ()
    return tuple(step for step in IDLE_WAIT_STEPS if step < life)


class SettingsError(ValueError):
    """A refused setting. ``code`` and ``field`` are for the API to report."""

    def __init__(self, code: str, message: str, field_name: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field_name


@dataclass(frozen=True)
class HarnessCompactionSettings:
    """One harness's settings as they apply now, and where each came from."""

    harness: str
    auto_enabled: bool
    trigger_tokens: int
    idle_after_minutes: int | None
    idle_min_tokens: int
    sources: Mapping[str, str] = field(default_factory=dict)
    saved: bool = False
    updated_at: str | None = None

    @property
    def effective_trigger(self) -> int | None:
        """The trigger in effect: the trigger with auto compaction on, else
        None. A model with a smaller window is the caller's to check."""
        return self.trigger_tokens if self.auto_enabled else None

    @property
    def idle_enabled(self) -> bool:
        return self.idle_after_minutes is not None

    @property
    def cache_life_minutes(self) -> int:
        return CACHE_LIFE_MINUTES[self.harness]

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape the Harness cards read (task #925)."""
        return {
            "harness": self.harness,
            "auto_enabled": self.auto_enabled,
            "trigger_tokens": self.trigger_tokens,
            "idle_after_minutes": self.idle_after_minutes,
            "idle_min_tokens": self.idle_min_tokens,
            "sources": dict(self.sources),
            "saved": self.saved,
            "updated_at": self.updated_at,
            "effective_trigger": self.effective_trigger,
            "cache_life_minutes": CACHE_LIFE_MINUTES[self.harness],
            "cache_life_measured": CACHE_LIFE_MEASURED[self.harness],
            "idle_choices": list(idle_choices(self.harness)),
            "min_tokens": MIN_TOKENS,
            "environment_variable": conversation_compaction.COMPACT_WINDOW_ENV.get(
                self.harness
            ),
        }


def _environment_trigger(
    harness: str,
    env: Mapping[str, str] | None,
) -> tuple[bool, int | None] | None:
    """What the environment says for ``harness``'s auto compaction.

    None when it says nothing (no variable for this harness, unset, or a
    value that is not a token count or ``off``). Otherwise ``(enabled,
    trigger)``: ``(False, None)`` for ``off``, ``(True, n)`` for a count.
    """
    name = conversation_compaction.COMPACT_WINDOW_ENV.get(harness)
    if name is None:
        return None
    raw = (os.environ if env is None else env).get(name, "").strip()
    if not raw:
        return None
    if raw.lower() == conversation_compaction.COMPACT_WINDOW_OFF:
        return (False, None)
    try:
        value = int(raw.replace("_", "").replace(",", ""))
    except ValueError:
        value = 0
    if value <= 0:
        LOG.warning(
            "%s=%r is not a token count or 'off'; using the default", name, raw
        )
        return None
    return (True, value)


def _saved_row(con, harness: str):
    try:
        return con.execute(
            f"SELECT {','.join(SAVED_COLUMNS)} FROM {TABLE} WHERE harness=?",
            (harness,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # A database not yet migrated to 0289 has no saved rows; the
        # environment and defaults still apply.
        if "no such table" in str(exc):
            return None
        raise


def resolve(
    con,
    harness: str,
    env: Mapping[str, str] | None = None,
) -> HarnessCompactionSettings | None:
    """``harness``'s settings now: saved, then environment, then default.

    None for a harness with no compaction settings (Vibe, a test fake). The
    row is read on every call, so a save applies to the next turn and the
    next idle heartbeat with no restart.
    """
    if harness not in DEFAULTS:
        return None
    row = _saved_row(con, harness) if con is not None else None
    return from_saved(harness, row, env)


# The saved columns, in the order ``from_saved`` reads them. A caller that
# already joins the table (the transcript projection, whose read budget is
# fixed) selects these and passes the row instead of reading it again.
SAVED_COLUMNS = (
    "auto_enabled",
    "trigger_tokens",
    "idle_after_minutes",
    "idle_min_tokens",
    "updated_at",
)


def from_saved(
    harness: str,
    row: Any,
    env: Mapping[str, str] | None = None,
) -> HarnessCompactionSettings | None:
    """``resolve`` over a saved row the caller already read (``SAVED_COLUMNS``
    in order), or None for no saved row."""
    if harness not in DEFAULTS:
        return None
    if row is not None and row[1] is None:
        # A LEFT JOIN with no saved row for this harness.
        row = None
    if row is not None:
        return HarnessCompactionSettings(
            harness=harness,
            auto_enabled=bool(row[0]),
            trigger_tokens=int(row[1]),
            idle_after_minutes=None if row[2] is None else int(row[2]),
            idle_min_tokens=int(row[3]),
            sources={name: SOURCE_SAVED for name in FIELDS},
            saved=True,
            updated_at=row[4],
        )
    defaults = DEFAULTS[harness]
    sources = {name: SOURCE_DEFAULT for name in FIELDS}
    auto_enabled = bool(defaults["auto_enabled"])
    trigger = int(defaults["trigger_tokens"])
    environment = _environment_trigger(harness, env)
    if environment is not None:
        enabled, value = environment
        auto_enabled = enabled
        sources["auto_enabled"] = SOURCE_ENVIRONMENT
        if value is not None:
            trigger = value
            sources["trigger_tokens"] = SOURCE_ENVIRONMENT
    return HarnessCompactionSettings(
        harness=harness,
        auto_enabled=auto_enabled,
        trigger_tokens=trigger,
        idle_after_minutes=defaults["idle_after_minutes"],
        idle_min_tokens=int(defaults["idle_min_tokens"]),
        sources=sources,
    )


def resolve_all(
    con,
    env: Mapping[str, str] | None = None,
) -> list[HarnessCompactionSettings]:
    """Every harness's settings, in card order."""
    return [resolve(con, harness, env) for harness in HARNESSES]  # type: ignore[misc]


def effective_trigger(
    con,
    harness: str,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """The trigger in effect for ``harness`` now, or None (off, or none)."""
    settings = resolve(con, harness, env)
    return settings.effective_trigger if settings is not None else None


def _whole_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_changes(harness: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    """The changes, checked; raises SettingsError on the first refusal.

    Trigger and idle size are whole numbers of at least 10,000; the wait is
    one of the harness's dropdown values or ``None`` (Off); auto compaction
    is a boolean. An unknown harness or field is refused.
    """
    if harness not in DEFAULTS:
        raise SettingsError(
            "COMPACTION_HARNESS_UNKNOWN",
            f"no compaction settings for harness: {harness}",
        )
    if not isinstance(changes, Mapping) or not changes:
        raise SettingsError(
            "COMPACTION_SETTINGS_EMPTY", "name at least one setting to change"
        )
    checked: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in FIELDS:
            raise SettingsError(
                "COMPACTION_SETTING_UNKNOWN", f"unknown setting: {name}", name
            )
        if name == "auto_enabled":
            if not isinstance(value, bool):
                raise SettingsError(
                    "COMPACTION_SETTING_INVALID",
                    "auto_enabled must be true or false",
                    name,
                )
        elif name in ("trigger_tokens", "idle_min_tokens"):
            if not _whole_number(value) or value < MIN_TOKENS:
                raise SettingsError(
                    "COMPACTION_SETTING_INVALID",
                    f"{name} must be a whole number of at least {MIN_TOKENS:,}",
                    name,
                )
        elif name == "idle_after_minutes":
            choices = idle_choices(harness)
            if value is not None and (
                not _whole_number(value) or value not in choices
            ):
                raise SettingsError(
                    "COMPACTION_SETTING_INVALID",
                    "idle_after_minutes must be Off (null) or one of "
                    + ", ".join(str(choice) for choice in choices),
                    name,
                )
        checked[name] = value
    return checked


def save(
    con,
    harness: str,
    changes: Mapping[str, Any],
    *,
    user_id: int | None = None,
    env: Mapping[str, str] | None = None,
) -> HarnessCompactionSettings:
    """Save ``changes`` for ``harness`` and return the settings now in effect.

    The row is written whole, in one write transaction: values not named in
    ``changes`` are copied from what was in effect, so the first save for a
    harness carries an environment-sourced trigger (or ``off``) into the
    row, after which the environment is ignored for that harness. A refused
    change raises SettingsError before anything is written, so the saved
    value stays as it was. Last save wins.
    """
    checked = validate_changes(harness, changes)
    with db_driver.write_transaction(con, "compaction.settings.save"):
        current = resolve(con, harness, env)
        assert current is not None  # validate_changes refused unknown ones
        merged = {
            "auto_enabled": current.auto_enabled,
            "trigger_tokens": current.trigger_tokens,
            "idle_after_minutes": current.idle_after_minutes,
            "idle_min_tokens": current.idle_min_tokens,
            **checked,
        }
        con.execute(
            f"INSERT INTO {TABLE} (harness,auto_enabled,trigger_tokens,"
            "idle_after_minutes,idle_min_tokens,updated_by_user_id,updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(harness) DO UPDATE SET "
            "auto_enabled=excluded.auto_enabled,"
            "trigger_tokens=excluded.trigger_tokens,"
            "idle_after_minutes=excluded.idle_after_minutes,"
            "idle_min_tokens=excluded.idle_min_tokens,"
            "updated_by_user_id=excluded.updated_by_user_id,"
            "updated_at=excluded.updated_at",
            (
                harness,
                int(bool(merged["auto_enabled"])),
                int(merged["trigger_tokens"]),
                merged["idle_after_minutes"],
                int(merged["idle_min_tokens"]),
                user_id,
            ),
        )
    saved = resolve(con, harness, env)
    assert saved is not None
    return saved


def harnesses_with_idle(
    con,
    harnesses: Iterable[str] = HARNESSES,
    env: Mapping[str, str] | None = None,
) -> dict[str, HarnessCompactionSettings]:
    """The harnesses whose idle compaction is on, with their settings."""
    resolved: dict[str, HarnessCompactionSettings] = {}
    for harness in harnesses:
        settings = resolve(con, harness, env)
        if settings is not None and settings.idle_enabled:
            resolved[harness] = settings
    return resolved
