"""Exact visual QA reader from released updater 8ff49ada096e."""
from pathlib import Path
import re
import shutil

REPO_ROOT = Path(".")
ENGINE = REPO_ROOT / ".super-coder"

_VISUAL_QA_MARKER_RE = re.compile(r"^# managed-by: subfloor — visual-qa shim v(?P<version>\d+)$")

def _workflow_version(text: str) -> int | None:
    first_line = text.splitlines()[0] if text else ""
    match = _VISUAL_QA_MARKER_RE.fullmatch(first_line)
    return int(match.group("version")) if match else None


def ensure_workflows(
    repo_root: Path = REPO_ROOT,
    template_root: Path | None = None,
    *,
    source_repo: bool | None = None,
) -> tuple[str, list[Path]]:
    """Reconcile the fork-tracked Visual QA shim and example config.

    Returns ``(action, changed_paths)`` where action is one of ``source``,
    ``seeded``, ``updated``, ``unmanaged``, or ``current``.
    """
    source = source_repo if source_repo is not None else is_source_repo()
    if source:
        return "source", []

    templates = template_root or ENGINE / "templates" / "fork"
    workflow_template = templates / "subfloor-visual-qa.yml"
    current = workflow_template.read_text()
    current_version = _workflow_version(current)
    if current_version is None:
        raise ValueError(f"Visual QA workflow template has no managed marker: {workflow_template}")

    changed: list[Path] = []
    workflow_relative = install_mod.VISUAL_QA_TEMPLATE_TARGETS[
        "subfloor-visual-qa.yml"
    ]
    workflow = repo_root / workflow_relative
    if not workflow.exists():
        workflow.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workflow_template, workflow)
        changed.append(workflow_relative)
        action = "seeded"
    else:
        installed_version = _workflow_version(workflow.read_text())
        if installed_version is None:
            action = "unmanaged"
        elif installed_version < current_version:
            shutil.copy2(workflow_template, workflow)
            changed.append(workflow_relative)
            action = "updated"
        else:
            action = "current"

    example_relative = install_mod.VISUAL_QA_TEMPLATE_TARGETS[
        "visual-qa.example.json"
    ]
    example = repo_root / example_relative
    if not example.exists():
        example.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / "visual-qa.example.json", example)
        changed.append(example_relative)

    return action, changed
