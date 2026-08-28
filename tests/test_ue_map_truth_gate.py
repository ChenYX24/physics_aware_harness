from __future__ import annotations

import ast
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_native_map_functions():
    source = ROOT / "scripts" / "native_ue_scene.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {"canonical_map_package", "current_world_package", "try_open_map"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace: dict = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class UEMapTruthGateTests(unittest.TestCase):
    def test_native_loader_rejects_a_different_loaded_world(self) -> None:
        functions = load_native_map_functions()
        state = {"world": "/Game/Maps/Other.Other", "load_requested": False}

        class World:
            def get_path_name(self) -> str:
                return state["world"]

        def load(path: str) -> bool:
            if state["load_requested"]:
                state["world"] = f"{path}.{path.rsplit('/', 1)[-1]}"
            return True

        functions["unreal"] = types.SimpleNamespace(
            EditorLevelLibrary=types.SimpleNamespace(get_editor_world=lambda: World(), load_level=load),
            EditorLoadingAndSavingUtils=types.SimpleNamespace(load_map=load),
        )

        opened, error = functions["try_open_map"]("/Game/Maps/Day")
        self.assertFalse(opened)
        self.assertIn("loaded_world_mismatch", error)

        state["load_requested"] = True
        opened, error = functions["try_open_map"]("/Game/Maps/Day")
        self.assertTrue(opened)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
