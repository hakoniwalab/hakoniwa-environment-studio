"""The Studio in the Business Pack Workspace (tools/env_workspace.py): its data
is in the Recipe workspace under $HAKONIWA_WORK_DIR, outside the Workspace it
stops with what to run, and the repositories it uses are its Business Pack
Recipe's recipe_local_requirements, found as recipe.py finds them."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_workspace  # noqa: E402


class WorkspaceTest(unittest.TestCase):
    def test_the_data_is_in_the_recipe_workspace(self):
        with tempfile.TemporaryDirectory() as work, \
                mock.patch.dict(os.environ, {"HAKONIWA_WORKSPACE_ACTIVE": "1", "HAKONIWA_WORK_DIR": work}):
            self.assertEqual(env_workspace.work_dir(), Path(work).resolve())
            self.assertEqual(env_workspace.recipe_workspace(),
                             Path(work).resolve() / "recipes" / "environment-studio")

    def test_outside_the_workspace_it_says_what_to_run(self):
        for environment in ({"HAKONIWA_WORKSPACE_ACTIVE": "", "HAKONIWA_WORK_DIR": "/tmp/work"},
                            {"HAKONIWA_WORKSPACE_ACTIVE": "1", "HAKONIWA_WORK_DIR": ""}):
            with mock.patch.dict(os.environ, environment), \
                    self.assertRaises(env_workspace.WorkspaceError) as caught:
                env_workspace.recipe_workspace()
            message = str(caught.exception.code)
            self.assertIn("python tools/workspace.py enter", message)
            self.assertIn("recipes/business-pack/environment-studio.yaml", message)

    def test_repositories_are_the_recipes_local_requirements(self):
        recipe = yaml.safe_load(env_workspace.RECIPE.read_text(encoding="utf-8"))
        self.assertEqual(recipe["id"], env_workspace.RECIPE_ID)
        self.assertTrue((ROOT / recipe["runtime_dependencies"]["python"]["requirements"]).is_file())
        with mock.patch.dict(os.environ, {"HAKONIWA_ENVSIM_ROOT": "", "HAKONIWA_URBAN_MOBILITY_ROOT": ""}):
            self.assertEqual(env_workspace.dependency_root("hakoniwa-envsim"), (ROOT.parent / "hakoniwa-envsim").resolve())
            self.assertEqual(env_workspace.dependency_root("hakoniwa-urban-mobility"),
                             (ROOT.parent / "hakoniwa-urban-mobility").resolve())
        with tempfile.TemporaryDirectory() as elsewhere, \
                mock.patch.dict(os.environ, {"HAKONIWA_ENVSIM_ROOT": elsewhere}):
            self.assertEqual(env_workspace.dependency_root("hakoniwa-envsim"), Path(elsewhere).resolve())


if __name__ == "__main__":
    unittest.main()
