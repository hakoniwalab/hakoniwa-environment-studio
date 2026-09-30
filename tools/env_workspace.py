"""Where Environment Studio keeps its work and finds the repositories it uses:
the Hakoniwa Business Pack Workspace (hakoniwa-business-pack
docs/hakoniwa-workspace-environment-ja.md).

The Studio runs inside the Workspace (`python tools/workspace.py enter`, or
`python tools/workspace.py run -- <command>`) with the Foundation Python that
its Business Pack Recipe prepares:

    python tools/recipe.py configure --recipe ../hakoniwa-environment-studio/recipes/business-pack/environment-studio.yaml

- Its data is in the Recipe workspace, $HAKONIWA_WORK_DIR/recipes/environment-studio.
- The Business Pack work directory ($HAKONIWA_WORK_DIR) is where other Recipes'
  City Worlds and the shared PLATEAU cache are.
- The repositories it uses (hakoniwa-envsim, hakoniwa-urban-mobility) are the
  Recipe's recipe_local_requirements, found as the Business Pack recipe.py
  finds them: the override variable, else default_path from this repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RECIPE_ID = "environment-studio"
RECIPE = ROOT / "recipes/business-pack" / f"{RECIPE_ID}.yaml"
ENTER = "python tools/workspace.py enter"
CONFIGURE = f"python tools/recipe.py configure --recipe ../{ROOT.name}/recipes/business-pack/{RECIPE_ID}.yaml"


class WorkspaceError(SystemExit):
    """Run outside the Workspace: a message and a non-zero exit, not a traceback."""

    def __init__(self, message: str):
        super().__init__(f"{message}\nIn hakoniwa-business-pack: {ENTER}, then {CONFIGURE}")


def work_dir() -> Path:
    """The Business Pack work directory ($HAKONIWA_WORK_DIR)."""
    configured = os.environ.get("HAKONIWA_WORK_DIR", "").strip()
    if os.environ.get("HAKONIWA_WORKSPACE_ACTIVE") != "1" or not configured:
        raise WorkspaceError("Environment Studio runs in the Hakoniwa Business Pack Workspace, which is not active.")
    return Path(configured).expanduser().resolve()


def recipe_workspace() -> Path:
    """This Recipe's workspace: $HAKONIWA_WORK_DIR/recipes/environment-studio."""
    return work_dir() / "recipes" / RECIPE_ID


def dependency_root(dependency_id: str) -> Path:
    """A repository of the Recipe's recipe_local_requirements."""
    recipe = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    root = recipe["recipe_local_requirements"][dependency_id]["root"]
    override = os.environ.get(root["override_env"], "").strip()
    selected = Path(override).expanduser() if override else Path(root["default_path"])
    return (selected if selected.is_absolute() else ROOT / selected).resolve()
