from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent.job_controller import AgentJobController
from harness.core.harness_config import HarnessConfigError, load_harness_config
from harness.planning.case_generation import OpenAICompatibleJSONClient, build_case_request


def config_document(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "harness_config_v1",
        "planning_llm": {
            "base_url": "https://planner.example/v1",
            "model": "planner-model",
            "image_capability": "unknown",
            "api_key_env": "PLANNER_TEST_KEY",
        },
        "meshy": {"api_key_env": "MESHY_TEST_KEY"},
        "codex_reviewer": {"executable": None},
        "ue_asset_importer": {"command": ["/bin/sh", "tools/importer.py"]},
        "ue_runtime": {
            "map_package": "",
            "actor_class": "",
            "asset_registry": None,
            "contact_export": False,
            "runner_command": None,
        },
        "paths": {
            "workspace": "external/workspace",
            "catalog": None,
            "ue_project": None,
            "ue_executable": "tools/UnrealEditor-Cmd",
        },
        "safety": {
            "default_publication_tier": "reference",
            "external_calls_default_allow": False,
        },
    }
    value.update(overrides)
    return value


class HarnessConfigTests(unittest.TestCase):
    def write_config(self, root: Path, value: dict[str, object]) -> Path:
        path = root / "config" / "harness.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_schema_and_every_nested_level_reject_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_root = config_document(unexpected=True)
            path = self.write_config(root, invalid_root)
            with self.assertRaisesRegex(HarnessConfigError, "unknown Harness config fields"):
                load_harness_config(config_path=path, repo_root=root, env={})

            invalid_nested = config_document()
            invalid_nested["planning_llm"]["token"] = "must-not-be-accepted"  # type: ignore[index]
            path.write_text(json.dumps(invalid_nested), encoding="utf-8")
            with self.assertRaisesRegex(HarnessConfigError, "unknown planning_llm fields"):
                load_harness_config(config_path=path, repo_root=root, env={})

    def test_safety_config_cannot_grant_external_calls_or_lower_publication_tier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for safety in (
                {"default_publication_tier": "local_preview", "external_calls_default_allow": False},
                {"default_publication_tier": "reference", "external_calls_default_allow": True},
            ):
                document = config_document()
                document["safety"] = safety
                path = self.write_config(root, document)
                with self.assertRaisesRegex(HarnessConfigError, "safety defaults are fixed"):
                    load_harness_config(config_path=path, repo_root=root, env={})

    def test_relative_paths_use_repo_root_while_catalog_defaults_from_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.write_config(root, config_document())
            config = load_harness_config(config_path=path, repo_root=root, env={})

        self.assertEqual(config.workspace, root / "external" / "workspace")
        self.assertEqual(config.catalog, config.workspace / "catalog" / "assets" / "catalog.sqlite")
        self.assertNotEqual(config.catalog, root / "catalog" / "assets" / "catalog.sqlite")
        self.assertEqual(config.ue_executable, root / "tools" / "UnrealEditor-Cmd")

    def test_cli_environment_config_default_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["planning_llm"]["model"] = "from-config"  # type: ignore[index]
            path = self.write_config(root, document)
            config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_HARNESS_LLM_MODEL": "from-env"},
                cli_overrides={"planning_llm.model": "from-cli"},
            )
            env_config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_HARNESS_LLM_MODEL": "from-env"},
            )
            file_config = load_harness_config(config_path=path, repo_root=root, env={})
            default_document = config_document()
            del default_document["planning_llm"]["model"]  # type: ignore[index]
            default_path = self.write_config(root / "defaults", default_document)
            default_config = load_harness_config(config_path=default_path, repo_root=root, env={})

        self.assertEqual(config.planning_model, "from-cli")
        self.assertEqual(config.sources["planning_llm.model"]["layer"], "cli")
        self.assertEqual(env_config.planning_model, "from-env")
        self.assertEqual(env_config.sources["planning_llm.model"]["key"], "SIM_HARNESS_LLM_MODEL")
        self.assertEqual(file_config.planning_model, "from-config")
        self.assertEqual(default_config.planning_model, "")
        self.assertEqual(default_config.sources["planning_llm.model"]["layer"], "default")

    def test_ue_importer_command_is_strict_argv_with_environment_and_cli_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            importer = root / "tools" / "importer.py"
            importer.parent.mkdir(parents=True)
            importer.write_text("# fixture importer\n", encoding="utf-8")
            path = self.write_config(root, config_document())
            file_config = load_harness_config(config_path=path, repo_root=root, env={})
            env_config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_HARNESS_UE_ASSET_IMPORTER_CMD": "/bin/echo from-env"},
            )
            cli_config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_HARNESS_UE_ASSET_IMPORTER_CMD": "/bin/echo from-env"},
                cli_overrides={"ue_asset_importer.command": "/bin/echo from-cli"},
            )
            file_available = file_config.inspect({})["providers"]["ue_asset_importer"]["available"]

        self.assertEqual(file_config.ue_asset_importer_command, ("/bin/sh", str(importer)))
        self.assertEqual(env_config.ue_asset_importer_command, ("/bin/echo", "from-env"))
        self.assertEqual(cli_config.ue_asset_importer_command, ("/bin/echo", "from-cli"))
        self.assertTrue(file_available)
        self.assertEqual(env_config.sources["ue_asset_importer.command"]["layer"], "environment")
        self.assertEqual(cli_config.sources["ue_asset_importer.command"]["layer"], "cli")

    def test_ue_importer_command_is_unavailable_when_configured_script_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.write_config(root, config_document())
            config = load_harness_config(config_path=path, repo_root=root, env={})

            inspection = config.inspect({})

        self.assertFalse(inspection["providers"]["ue_asset_importer"]["available"])

    def test_ue_importer_command_rejects_empty_or_non_string_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for command in ([], ["/bin/sh", 7]):
                document = config_document()
                document["ue_asset_importer"] = {"command": command}
                path = self.write_config(root, document)
                with self.assertRaisesRegex(HarnessConfigError, "ue_asset_importer.command"):
                    load_harness_config(config_path=path, repo_root=root, env={})

    def test_ue_runtime_is_strict_and_environment_or_cli_overrides_case_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["ue_runtime"] = {
                "map_package": "/Game/Config/Default.Default",
                "actor_class": "/Script/Engine.StaticMeshActor",
                "asset_registry": "runtime/registry.json",
                "contact_export": True,
                "runner_command": ["/bin/sh", "runtime/runner.py"],
            }
            path = self.write_config(root, document)
            case = {"scene": {"map_package": "/Game/Case/Chosen.Chosen"}}
            file_config = load_harness_config(config_path=path, repo_root=root, env={})
            env_config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_STUDIO_UE_MAP": "/Game/Env/Override.Override"},
            )
            cli_config = load_harness_config(
                config_path=path,
                repo_root=root,
                env={"SIM_STUDIO_UE_MAP": "/Game/Env/Override.Override"},
                cli_overrides={"ue_runtime.map_package": "/Game/Cli/Override.Override"},
            )
            runtime_only_document = json.loads(json.dumps(document))
            runtime_only_document["ue_runtime"]["actor_class"] = "/Script/Engine.Actor"  # type: ignore[index]
            runtime_only_document["ue_runtime"]["runner_command"] = ["/bin/echo", "runtime-only"]  # type: ignore[index]
            runtime_only_path = self.write_config(root / "runtime-only", runtime_only_document)
            runtime_only_config = load_harness_config(
                config_path=runtime_only_path,
                repo_root=root,
                env={},
            )

            self.assertEqual(file_config.ue_map_package_for_case(case), "/Game/Case/Chosen.Chosen")
            self.assertEqual(env_config.ue_map_package_for_case(case), "/Game/Env/Override.Override")
            self.assertEqual(cli_config.ue_map_package_for_case(case), "/Game/Cli/Override.Override")
            self.assertTrue(file_config.ue_contact_export)
            self.assertEqual(file_config.ue_runner_command, ("/bin/sh", str(root / "runtime" / "runner.py")))
            self.assertEqual(file_config.ue_compile_identity(case), runtime_only_config.ue_compile_identity(case))
            self.assertNotEqual(file_config.ue_execution_identity(case), runtime_only_config.ue_execution_identity(case))

            invalid = config_document()
            invalid["ue_runtime"] = {**document["ue_runtime"], "contact_export": "1"}  # type: ignore[arg-type]
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(HarnessConfigError, "ue_runtime.contact_export must be a boolean"):
                load_harness_config(config_path=path, repo_root=root, env={})

    def test_ue_runtime_inspect_checks_map_registry_and_runner_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "ue" / "SimulatorWorkspace.uproject"
            map_file = project.parent / "Content" / "Harness" / "Configured.umap"
            registry = root / "runtime" / "registry.json"
            runner = root / "runtime" / "runner.py"
            for path in (project, map_file, registry, runner):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            document = config_document()
            document["paths"] = {
                "workspace": str(root / "workspace"),
                "catalog": None,
                "ue_project": str(project),
                "ue_executable": None,
            }
            document["ue_runtime"] = {
                "map_package": "/Game/Harness/Configured.Configured",
                "actor_class": "/Script/Engine.StaticMeshActor",
                "asset_registry": str(registry),
                "contact_export": True,
                "runner_command": ["/bin/sh", str(runner)],
            }
            config = load_harness_config(
                config_path=self.write_config(root, document),
                repo_root=root,
                env={},
            )

            runtime = config.inspect({})["ue_runtime"]

        self.assertTrue(runtime["map_package"]["exists"])
        self.assertTrue(runtime["asset_registry"]["exists"])
        self.assertTrue(runtime["runner_command"]["available"])

    def test_controller_projects_configured_ue_importer_command_without_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["paths"] = {
                "workspace": str(root / "workspace"),
                "catalog": None,
                "ue_project": None,
                "ue_executable": None,
            }
            path = self.write_config(root, document)
            config = load_harness_config(config_path=path, repo_root=root, env={})
            controller = AgentJobController(config=config)
            with patch.dict(os.environ, {}, clear=True):
                with controller._effective_environment():
                    projected = os.environ["SIM_HARNESS_UE_ASSET_IMPORTER_CMD"]

        self.assertEqual(projected, f"/bin/sh {root / 'tools' / 'importer.py'}")

    def test_legacy_openai_fallbacks_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["planning_llm"]["api_key_env"] = "SIM_HARNESS_LLM_API_KEY"  # type: ignore[index]
            path = self.write_config(root, document)
            env = {
                "OPENAI_BASE_URL": "https://compat.example/v1",
                "OPENAI_MODEL": "compat-model",
                "OPENAI_API_KEY": "compat-secret",
            }
            config = load_harness_config(config_path=path, repo_root=root, env=env)

        self.assertEqual(config.planning_base_url, "https://compat.example/v1")
        self.assertEqual(config.planning_model, "compat-model")
        self.assertEqual(config.planning_secret_env_name(env), "OPENAI_API_KEY")
        self.assertEqual(config.planning_api_key(env), "compat-secret")

    def test_effective_config_does_not_fall_back_to_an_unselected_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.write_config(root, config_document())
            config = load_harness_config(config_path=path, repo_root=root, env={})
            with patch.dict(
                "os.environ",
                {
                    "SIM_HARNESS_LLM_API_KEY": "legacy-secret",
                    "OPENAI_API_KEY": "other-legacy-secret",
                },
                clear=True,
            ):
                client = OpenAICompatibleJSONClient(effective_config=config)

        self.assertIsNone(client.api_key)
        self.assertEqual(client.model, "planner-model")

    def test_endpoint_rejects_secret_carrying_url_components_without_echoing_values(self) -> None:
        secret = "do-not-echo-this-token"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["planning_llm"]["base_url"] = f"https://user:{secret}@planner.example/v1?token={secret}#x"  # type: ignore[index]
            path = self.write_config(root, document)
            with self.assertRaises(HarnessConfigError) as captured:
                load_harness_config(config_path=path, repo_root=root, env={})

        self.assertNotIn(secret, str(captured.exception))

    def test_secret_values_do_not_affect_or_appear_in_inspection_or_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.write_config(root, config_document())
            first_env = {"PLANNER_TEST_KEY": "alpha-secret", "MESHY_TEST_KEY": "meshy-secret"}
            second_env = {"PLANNER_TEST_KEY": "beta-secret", "MESHY_TEST_KEY": "other-secret"}
            first = load_harness_config(config_path=path, repo_root=root, env=first_env)
            second = load_harness_config(config_path=path, repo_root=root, env=second_env)
            rendered = json.dumps(first.inspect(first_env), sort_keys=True)

        self.assertEqual(first.digest, second.digest)
        for secret in (*first_env.values(), *second_env.values()):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, json.dumps(first.identity(), sort_keys=True))

    def test_controller_and_agent_inspection_share_digest_without_granting_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            document = config_document()
            document["paths"] = {
                "workspace": str(root / "workspace"),
                "catalog": None,
                "ue_project": None,
                "ue_executable": None,
            }
            path = self.write_config(root, document)
            config = load_harness_config(config_path=path, repo_root=root, env={})
            controller = AgentJobController(config=config)
            created = controller.create(
                build_case_request(case_id="config_digest", text="drop a ball"),
                job_id="job_config_digest",
            )

        self.assertEqual(created["effective_config_digest"], config.inspect({})["effective_config_digest"])
        self.assertEqual(created["job"]["target"]["publication_tier"], "reference")
        self.assertFalse(created["job"]["authorizations"]["external_provider"])
        self.assertFalse(created["job"]["authorizations"]["paid_provider_submission"])
        self.assertFalse(created["job"]["authorizations"]["planning_llm_upload"])


if __name__ == "__main__":
    unittest.main()
