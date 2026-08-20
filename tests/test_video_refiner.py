from __future__ import annotations

import tempfile
import unittest
import json
import hashlib
import os
import shutil
import subprocess
import sys
import io
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from harness.core.prompt_lineage import append_prompt_stage, new_prompt_lineage, prompt_digest
from harness.refinement.video_refiner import (
    ARK_DEFAULT_MODEL,
    ARK_MODEL_ENDPOINTS,
    RefinementJob,
    build_payload,
    dry_run_plan,
    run_refinement,
    split_multiview_grid,
    splice_video,
    validate_comparison_jobs,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            body, self.body = self.body, b""
            return body
        body, self.body = self.body[:size], self.body[size:]
        return body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class VideoRefinerTests(unittest.TestCase):
    def test_comparison_contract_enforces_shared_canonical_and_refiner_prompts(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        teacher_video = temporary / "ue.mp4"
        teacher_video.write_bytes(b"validated UE teacher")
        teacher_receipt = temporary / "ue.teacher_validation.json"
        teacher_receipt.write_text(
            json.dumps(
                {
                    "schema_version": "harness_ue_teacher_validation_v1",
                    "status": "pass",
                    "input_sha256": hashlib.sha256(teacher_video.read_bytes()).hexdigest(),
                    "canonical_prompt_sha256": prompt_digest("One canonical billiards break prompt."),
                    "checks": {
                        "canonical_prompt_match": "pass",
                        "event_contract": "pass",
                        "physics_hard_gate": "pass",
                        "no_penetration": "pass",
                    },
                }
            ),
            encoding="utf-8",
        )
        lineage = new_prompt_lineage("fair-billiards", "台球开球")
        append_prompt_stage(
            lineage,
            stage_id="canonical_generation_prompt",
            stage_kind="canonical_generation",
            content="One canonical billiards break prompt.",
            producer="test",
            purpose="fair comparison",
            parent_stage_ids=("user_request",),
        )
        append_prompt_stage(
            lineage,
            stage_id="refiner_appearance_prompt",
            stage_kind="appearance_only_refinement",
            content="One shared appearance-only prompt.",
            producer="test",
            purpose="fair comparison",
            parent_stage_ids=("canonical_generation_prompt",),
        )
        lineage["canonical_stage_id"] = "canonical_generation_prompt"
        lineage["refiner_stage_id"] = "refiner_appearance_prompt"
        common = {
            "schema_version": "harness_video_refinement_job_v1",
            "input_video": str(teacher_video),
            "output_video": "/tmp/out.mp4",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
            "prompt_lineage": lineage,
        }
        direct = RefinementJob.from_dict(
            {
                **common,
                "job_id": "h3-direct",
                "provider": "h3_sglang",
                "model": "MiniMaxAI/MiniMax-H3",
                "prompt": "One canonical billiards break prompt.",
                "prompt_stage_id": "canonical_generation_prompt",
                "use_reference_video": False,
            }
        )
        refined = RefinementJob.from_dict(
            {
                **common,
                "job_id": "seedance-refined",
                "provider": "ark_seedance",
                "model": "ep-test",
                "job_role": "ue_refiner",
                "prompt": "One shared appearance-only prompt.",
                "prompt_stage_id": "refiner_appearance_prompt",
                "reference_video_uri": "https://media.example/ue.mp4",
                "use_reference_video": True,
                "teacher_validation_path": str(teacher_receipt),
            }
        )

        report = validate_comparison_jobs([direct, refined])

        self.assertEqual(report["status"], "pass")
        with self.assertRaisesRegex(ValueError, "must equal its prompt lineage stage"):
            RefinementJob.from_dict(
                {
                    **common,
                    "job_id": "unfair",
                    "provider": "ark_seedance",
                    "model": "ep-test",
                    "prompt": "Provider-specific prompt.",
                    "prompt_stage_id": "canonical_generation_prompt",
                    "use_reference_video": False,
                }
            )

        direct_repair = RefinementJob.from_dict(
            {
                **common,
                "job_id": "direct-repair",
                "provider": "ark_seedance",
                "model": "ep-test",
                "job_role": "direct_repair",
                "prompt": "One canonical billiards break prompt.",
                "prompt_stage_id": "canonical_generation_prompt",
                "reference_video_uri": "https://media.example/error.mp4",
                "use_reference_video": True,
            }
        )
        with self.assertRaisesRegex(ValueError, "different comparison workflows"):
            validate_comparison_jobs([direct, direct_repair])
        with self.assertRaisesRegex(ValueError, "require a reference video"):
            RefinementJob.from_dict(
                {
                    **common,
                    "job_id": "invalid-direct-repair",
                    "provider": "ark_seedance",
                    "model": "ep-test",
                    "job_role": "direct_repair",
                    "prompt": "One canonical billiards break prompt.",
                    "prompt_stage_id": "canonical_generation_prompt",
                    "use_reference_video": False,
                }
            )

    def test_ark_defaults_to_seedance_20_fast_and_can_switch_to_25(self) -> None:
        job = RefinementJob.from_dict(
            {
                "schema_version": "harness_video_refinement_job_v1",
                "job_id": "ark-default-model",
                "provider": "ark_seedance",
                "prompt": "repair",
                "input_video": "/tmp/input.mp4",
                "use_reference_video": False,
                "output_video": "/tmp/output.mp4",
                "duration_seconds": 4,
                "aspect_ratio": "16:9",
            }
        )
        self.assertEqual(job.model, ARK_DEFAULT_MODEL)
        self.assertEqual(job.with_model(ARK_MODEL_ENDPOINTS["2.5"]).model, "ep-20260807200104-j8rs8")

        reference_job = RefinementJob.from_dict(
            {
                "schema_version": "harness_video_refinement_job_v1",
                "job_id": "ark-reference-model-switch",
                "provider": "ark_seedance",
                "prompt": "repair",
                "input_video": "/tmp/input.mp4",
                "reference_video_uri": "https://media.example/reference.mp4",
                "use_reference_video": True,
                "output_video": "/tmp/output.mp4",
                "duration_seconds": 4,
                "aspect_ratio": "16:9",
            }
        ).with_model(ARK_MODEL_ENDPOINTS["2.5"])
        self.assertEqual((reference_job.duration_seconds, reference_job.aspect_ratio), (-1, "adaptive"))
        with self.assertRaisesRegex(ValueError, "fixed duration"):
            reference_job.with_model(ARK_MODEL_ENDPOINTS["2.0-fast"])

    def test_seedance_25_adaptive_reference_edit_payload(self) -> None:
        data = {
            "schema_version": "harness_video_refinement_job_v1",
            "job_id": "ark-adaptive-edit",
            "provider": "ark_seedance",
            "model": ARK_MODEL_ENDPOINTS["2.5"],
            "prompt": "edit from the reference video",
            "input_video": "/tmp/input.mp4",
            "reference_video_uri": "https://media.example/reference.mp4",
            "use_reference_video": True,
            "output_video": "/tmp/output.mp4",
            "duration_seconds": -1,
            "aspect_ratio": "adaptive",
        }
        job = RefinementJob.from_dict(data)
        payload = build_payload(job)
        self.assertEqual((payload["ratio"], payload["duration"]), ("adaptive", -1))
        with self.assertRaisesRegex(ValueError, "Seedance 2.5 adaptive"):
            RefinementJob.from_dict({**data, "model": ARK_DEFAULT_MODEL})

    def test_provider_reference_uri_schemes_are_fail_closed(self) -> None:
        common = {
            "schema_version": "harness_video_refinement_job_v1",
            "job_id": "unsafe-reference",
            "model": "model",
            "prompt": "repair",
            "input_video": "/tmp/input.mp4",
            "use_reference_video": True,
            "output_video": "/tmp/output.mp4",
            "duration_seconds": 4,
            "aspect_ratio": "16:9",
        }
        ark = RefinementJob.from_dict(
            {**common, "provider": "ark_seedance", "reference_video_uri": "file:///private/input.mp4"}
        )
        with self.assertRaisesRegex(ValueError, "Ark reference URIs"):
            build_payload(ark)

        h3 = RefinementJob.from_dict(
            {**common, "provider": "h3_sglang", "reference_video_uri": "ftp://example/input.mp4"}
        )
        with self.assertRaisesRegex(ValueError, "H3 reference URIs"):
            build_payload(h3)

    def test_ark_submit_error_preserves_provider_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "input.mp4"
            input_video.write_bytes(b"video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "ark-rejected",
                    "provider": "ark_seedance",
                    "model": "endpoint",
                    "prompt": "repair",
                    "input_video": str(input_video),
                    "use_reference_video": False,
                    "output_video": str(root / "output.mp4"),
                    "duration_seconds": 4,
                    "aspect_ratio": "16:9",
                }
            )

            def reject(request: object, timeout: float = 0) -> FakeResponse:
                raise HTTPError(
                    request.full_url,
                    400,
                    "bad request",
                    {},
                    io.BytesIO(b'{"error":{"code":"InvalidParameter","message":"too many references"}}'),
                )

            with patch.dict(os.environ, {"ARK_API_KEY": "runtime-only"}):
                manifest = run_refinement(
                    job,
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    manifest_path=root / "manifest.json",
                    opener=reject,
                )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(
                manifest["failure_reason"],
                "submit rejected: HTTP 400: InvalidParameter: too many references",
            )

    def test_connection_refused_is_safe_to_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "input.mp4"
            input_video.write_bytes(b"video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "not-submitted",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "repair",
                    "input_video": str(input_video),
                    "use_reference_video": False,
                    "output_video": str(root / "output.mp4"),
                    "duration_seconds": 4,
                    "aspect_ratio": "16:9",
                }
            )

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30010",
                manifest_path=root / "manifest.json",
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError(ConnectionRefusedError())),
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_reason"], "submit not sent: connection refused")
            self.assertIsNone(manifest["task_id"])

    def test_ark_payload_accepts_multiple_reference_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "input.mp4"
            input_video.write_bytes(b"input")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "dual-reference",
                    "provider": "ark_seedance",
                    "model": "ep-test",
                    "prompt": "preserve identity and follow motion",
                    "input_video": str(input_video),
                    "reference_video_uris": ["https://media.example/original.mp4", "https://media.example/ue.mp4"],
                    "use_reference_video": True,
                    "output_video": str(root / "output.mp4"),
                    "duration_seconds": 4,
                    "aspect_ratio": "16:9",
                }
            )

            content = build_payload(job)["content"]

            self.assertEqual([item["role"] for item in content[1:]], ["reference_video", "reference_video"])
            self.assertEqual(
                [item["video_url"]["url"] for item in content[1:]],
                ["https://media.example/original.mp4", "https://media.example/ue.mp4"],
            )

    def test_ark_payload_accepts_identity_images_plus_motion_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "input.mp4"
            input_video.write_bytes(b"input")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "keyframes-plus-motion",
                    "provider": "ark_seedance",
                    "model": "ep-test",
                    "prompt": "preserve identity and follow motion",
                    "input_video": str(input_video),
                    "reference_image_uris": ["https://media.example/start.jpg", "https://media.example/end.jpg"],
                    "reference_video_uri": "https://media.example/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(root / "output.mp4"),
                    "duration_seconds": 4,
                    "aspect_ratio": "16:9"
                }
            )

            content = build_payload(job)["content"]

            self.assertEqual([item["role"] for item in content[1:]], ["reference_image", "reference_image", "reference_video"])
            self.assertEqual(content[1]["image_url"]["url"], "https://media.example/start.jpg")

    def test_reference_video_toggle_changes_only_the_reference_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_video = Path(directory) / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "video-a-refine",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "Preserve the simulated turn and improve visual continuity.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(Path(directory) / "refined.mp4"),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                    "seed": 42,
                }
            )

            with_reference = build_payload(job)
            without_reference = build_payload(job.with_reference_video(False))

            self.assertEqual(with_reference["task"], "ref2va")
            self.assertEqual(with_reference["conditions"][0]["uri"], "file:///data/ue.mp4")
            self.assertEqual(without_reference["task"], "t2va")
            self.assertEqual(without_reference["conditions"], [])
            self.assertEqual(with_reference["num_inference_steps"], 50)
            self.assertEqual(with_reference["flow_shift"], 12.0)
            self.assertEqual(with_reference["audio_flow_shift"], 3.0)

            reference_plan = dry_run_plan(job, base_url="http://127.0.0.1:30011")
            reference_worker_two = dry_run_plan(job, base_url="http://127.0.0.1:30012")
            no_reference_plan = dry_run_plan(
                job.with_reference_video(False),
                base_url="http://127.0.0.1:30010",
            )
            self.assertEqual(reference_plan["submit_url"], "http://127.0.0.1:30011/v1/videos")
            self.assertEqual(reference_worker_two["submit_url"], "http://127.0.0.1:30012/v1/videos")
            self.assertEqual(no_reference_plan["payload"]["task"], "t2va")

            with self.assertRaisesRegex(ValueError, "FL2VA.*30010"):
                dry_run_plan(
                    job.with_reference_video(False),
                    base_url="http://127.0.0.1:30011",
                )
            with self.assertRaisesRegex(ValueError, "Ref2VA.*30011"):
                dry_run_plan(job, base_url="http://127.0.0.1:30010")

    def test_h3_ref2va_accepts_three_reference_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "input.mp4"
            input_video.write_bytes(b"input")
            payload = build_payload(
                RefinementJob.from_dict(
                    {
                        "schema_version": "harness_video_refinement_job_v1",
                        "job_id": "h3-three-reference",
                        "provider": "h3_sglang",
                        "model": "MiniMaxAI/MiniMax-H3",
                        "prompt": "preserve identity, scene, and simulated motion",
                        "input_video": str(input_video),
                        "reference_video_uris": ["file:///original.mp4", "file:///ue.mp4", "file:///context.mp4"],
                        "use_reference_video": True,
                        "output_video": str(root / "output.mp4"),
                        "duration_seconds": 4,
                        "aspect_ratio": "16:9",
                    }
                )
            )

            self.assertEqual([item["uri"] for item in payload["conditions"]], ["file:///original.mp4", "file:///ue.mp4", "file:///context.mp4"])

    def test_h3_submits_once_polls_and_downloads_with_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            output_video = root / "refined.mp4"
            manifest_path = root / "manifest.json"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "video-a-refine",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "Preserve the motion.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(output_video),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                    "seed": 42,
                }
            )
            calls: list[tuple[str, str]] = []
            statuses = iter(("in_progress", "completed"))
            submitted_payload: dict[str, object] = {}

            def open_request(request: object, timeout: float = 0) -> FakeResponse:
                method = request.get_method()  # type: ignore[attr-defined]
                url = request.full_url  # type: ignore[attr-defined]
                calls.append((method, url))
                if method == "POST":
                    submitted_payload.update(json.loads(request.data))  # type: ignore[attr-defined]
                    return FakeResponse(b'{"id":"h3-task-1"}')
                if url.endswith("/content"):
                    return FakeResponse(b"refined-video")
                return FakeResponse(json.dumps({"status": next(statuses)}).encode())

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=open_request,
                sleep=lambda _seconds: None,
                poll_interval=0,
            )

            self.assertEqual([method for method, _url in calls].count("POST"), 1)
            self.assertEqual(calls[0], ("POST", "http://127.0.0.1:30011/v1/videos"))
            self.assertEqual(submitted_payload["task"], "ref2va")
            self.assertEqual(output_video.read_bytes(), b"refined-video")
            self.assertEqual(manifest["task_id"], "h3-task-1")
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["input_sha256"], hashlib.sha256(b"ue-video").hexdigest())
            self.assertEqual(manifest["output_sha256"], hashlib.sha256(b"refined-video").hexdigest())
            self.assertEqual(json.loads(manifest_path.read_text()), manifest)

    def test_existing_task_manifest_resumes_polling_without_resubmitting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "resume-me",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "Preserve the motion.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(root / "refined.mp4"),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                }
            )
            manifest_path = root / "manifest.json"
            first_methods: list[str] = []

            def first_open(request: object, timeout: float = 0) -> FakeResponse:
                method = request.get_method()  # type: ignore[attr-defined]
                first_methods.append(method)
                if method == "POST":
                    return FakeResponse(b'{"id":"existing-task"}')
                return FakeResponse(b'{"status":"running"}')

            run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=first_open,
                sleep=lambda _seconds: None,
                max_polls=1,
            )
            second_methods: list[str] = []

            def second_open(request: object, timeout: float = 0) -> FakeResponse:
                method = request.get_method()  # type: ignore[attr-defined]
                second_methods.append(method)
                if request.full_url.endswith("/content"):  # type: ignore[attr-defined]
                    return FakeResponse(b"resumed-output")
                return FakeResponse(b'{"status":"completed"}')

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=second_open,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(first_methods.count("POST"), 1)
            self.assertNotIn("POST", second_methods)
            self.assertEqual(manifest["task_id"], "existing-task")
            self.assertEqual(manifest["status"], "succeeded")
            self.assertIsNone(manifest["failure_reason"])

            manifest["failure_reason"] = "stale poll timeout"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            repaired = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=lambda *_args, **_kwargs: self.fail("completed resume must not make a network call"),
            )
            self.assertIsNone(repaired["failure_reason"])

    def test_deleted_task_resume_records_failure_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "deleted-task",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "Preserve the motion.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(root / "refined.mp4"),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                }
            )
            manifest_path = root / "manifest.json"
            run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=lambda request, **_kwargs: FakeResponse(b'{"id":"gone"}') if request.get_method() == "POST" else FakeResponse(b'{"status":"queued"}'),
                sleep=lambda _seconds: None,
                max_polls=1,
            )

            def missing(request: object, timeout: float = 0) -> FakeResponse:
                raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"{}"))

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=manifest_path,
                opener=missing,
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_reason"], "poll failed: HTTP 404 task not found")

    def test_poll_connection_reset_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            output_video = root / "refined.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "poll-reset",
                    "provider": "h3_sglang",
                    "model": "MiniMax/MiniMax-H3",
                    "prompt": "Preserve motion.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(output_video),
                    "duration_seconds": 4,
                    "aspect_ratio": "16:9"
                }
            )
            polls = 0

            def opener(request: object, timeout: float = 0) -> FakeResponse:
                nonlocal polls
                if request.get_method() == "POST":  # type: ignore[attr-defined]
                    return FakeResponse(b'{"id":"reset-task"}')
                if request.full_url.endswith("/content"):  # type: ignore[attr-defined]
                    return FakeResponse(b"video")
                polls += 1
                if polls == 1:
                    raise ConnectionResetError("worker restart")
                return FakeResponse(b'{"status":"completed"}')

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=root / "manifest.json",
                opener=opener,
                sleep=lambda _seconds: None,
                poll_interval=0,
            )
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(polls, 2)

    def test_ark_submits_polls_and_downloads_without_leaking_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            output_video = root / "seedance.mp4"
            manifest_path = root / "manifest.json"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "video-b-seedance",
                    "provider": "ark_seedance",
                    "model": "ep-test-model",
                    "prompt": "Keep the bag visible through the placement action.",
                    "input_video": str(input_video),
                    "reference_video_uri": "https://media.example/ue.mp4?signature=private",
                    "use_reference_video": True,
                    "output_video": str(output_video),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                    "generate_audio": False,
                }
            )
            requests: list[object] = []

            def open_request(request: object, timeout: float = 0) -> FakeResponse:
                requests.append(request)
                method = request.get_method()  # type: ignore[attr-defined]
                url = request.full_url  # type: ignore[attr-defined]
                if method == "POST":
                    return FakeResponse(b'{"id":"ark-task-1"}')
                if url == "https://download.example/result.mp4":
                    return FakeResponse(b"seedance-output")
                return FakeResponse(
                    b'{"id":"ark-task-1","status":"succeeded","content":{"video_url":"https://download.example/result.mp4"}}'
                )

            with patch.dict("os.environ", {"ARK_API_KEY": "top-secret-key"}):
                manifest = run_refinement(
                    job,
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    manifest_path=manifest_path,
                    opener=open_request,
                    sleep=lambda _seconds: None,
                )

            submitted = json.loads(requests[0].data)  # type: ignore[attr-defined]
            self.assertEqual(submitted["content"][1]["role"], "reference_video")
            self.assertEqual(submitted["seed"], 0)
            self.assertEqual(len(build_payload(job.with_reference_video(False))["content"]), 1)
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer top-secret-key")  # type: ignore[attr-defined]
            self.assertIsNone(requests[-1].get_header("Authorization"))  # type: ignore[attr-defined]
            self.assertEqual(output_video.read_bytes(), b"seedance-output")
            self.assertEqual(manifest["status"], "succeeded")
            self.assertNotIn("top-secret-key", manifest_path.read_text())

    def test_dry_run_redacts_credentials_and_signed_url_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "safe-dry-run",
                    "provider": "ark_seedance",
                    "model": "ep-test-model",
                    "prompt": "Preserve continuity.",
                    "input_video": str(input_video),
                    "reference_video_uri": "https://media.example/ue.mp4?signature=url-secret",
                    "use_reference_video": True,
                    "output_video": str(root / "out.mp4"),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                }
            )

            with patch.dict("os.environ", {"ARK_API_KEY": "environment-secret"}):
                plan = dry_run_plan(job, base_url="https://ark.cn-beijing.volces.com/api/v3")

            rendered = json.dumps(plan)
            self.assertNotIn("environment-secret", rendered)
            self.assertNotIn("url-secret", rendered)
            self.assertEqual(plan["headers"]["Authorization"], "<redacted>")
            self.assertEqual(
                plan["payload"]["content"][1]["video_url"]["url"],
                "https://media.example/ue.mp4?<redacted>",
            )

    def test_failed_task_records_a_stable_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            job = RefinementJob.from_dict(
                {
                    "schema_version": "harness_video_refinement_job_v1",
                    "job_id": "failed-task",
                    "provider": "h3_sglang",
                    "model": "MiniMaxAI/MiniMax-H3",
                    "prompt": "Preserve continuity.",
                    "input_video": str(input_video),
                    "reference_video_uri": "file:///data/ue.mp4",
                    "use_reference_video": True,
                    "output_video": str(root / "out.mp4"),
                    "duration_seconds": 5,
                    "aspect_ratio": "16:9",
                }
            )

            def open_request(request: object, timeout: float = 0) -> FakeResponse:
                if request.get_method() == "POST":  # type: ignore[attr-defined]
                    return FakeResponse(b'{"id":"failed-1"}')
                return FakeResponse(b'{"status":"failed","error":{"code":"Moderation","message":"denied"}}')

            manifest = run_refinement(
                job,
                base_url="http://127.0.0.1:30011",
                manifest_path=root / "manifest.json",
                opener=open_request,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure_reason"], "Moderation: denied")
            self.assertFalse(job.output_video.exists())

    def test_job_and_endpoint_safety_fail_closed_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            payload = {
                "schema_version": "harness_video_refinement_job_v1",
                "job_id": "safe-job",
                "provider": "ark_seedance",
                "model": "ep-test-model",
                "prompt": "Preserve continuity.",
                "input_video": str(input_video),
                "reference_video_uri": "https://media.example/ue.mp4",
                "use_reference_video": True,
                "output_video": str(root / "out.mp4"),
                "duration_seconds": 5,
                "aspect_ratio": "16:9",
            }
            invalid = dict(payload, use_reference_video="false")
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                RefinementJob.from_dict(invalid)

            job = RefinementJob.from_dict(payload)
            with self.assertRaisesRegex(ValueError, "approved HTTPS"):
                dry_run_plan(job, base_url="https://attacker.example/api/v3")

            overwrite = RefinementJob.from_dict(dict(payload, output_video=str(input_video)))
            with self.assertRaisesRegex(ValueError, "paths must be distinct"):
                run_refinement(
                    overwrite,
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    manifest_path=root / "manifest.json",
                    opener=lambda *_args, **_kwargs: self.fail("network should not be called"),
                )

    def test_cli_dry_run_compares_reference_payloads_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_video = root / "ue.mp4"
            input_video.write_bytes(b"ue-video")
            spec_path = root / "job.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema_version": "harness_video_refinement_job_v1",
                        "job_id": "cli-dry-run",
                        "provider": "ark_seedance",
                        "model": "ep-test-model",
                        "prompt": "Preserve continuity.",
                        "input_video": str(input_video),
                        "reference_video_uri": "https://media.example/ue.mp4?signature=url-secret",
                        "use_reference_video": True,
                        "output_video": str(root / "out.mp4"),
                        "duration_seconds": 5,
                        "aspect_ratio": "16:9",
                    }
                ),
                encoding="utf-8",
            )
            environment = dict(os.environ, ARK_API_KEY="environment-secret")

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/harness_refine_video.py",
                    str(spec_path),
                    "--dry-run",
                    "--compare-reference",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("environment-secret", result.stdout + result.stderr)
            self.assertNotIn("url-secret", result.stdout + result.stderr)
            comparison = json.loads(result.stdout)
            self.assertEqual(len(comparison["with_reference"]["payload"]["content"]), 2)
            self.assertEqual(len(comparison["without_reference"]["payload"]["content"]), 1)
            self.assertFalse((root / "out.mp4.manifest.json").exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_joint_multiview_output_splits_into_frame_aligned_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "grid.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=180x80:r=5",
                    "-frames:v", "5", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
                ],
                check=True,
            )

            manifest = split_multiview_grid(
                source,
                root / "crops",
                ["front", "side", "top", "tracking", "closeup"],
            )

            self.assertEqual(manifest["grid"], {"columns": 3, "rows": 2, "cell_width": 60, "cell_height": 40})
            self.assertEqual(len(manifest["outputs"]), 5)
            self.assertEqual({item["frame_count"] for item in manifest["outputs"]}, {5})
            self.assertTrue(all(Path(item["path"]).is_file() for item in manifest["outputs"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_frame_exact_replace_rebuilds_cfr_video_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            replacement = root / "replacement.mp4"
            output = root / "spliced.mp4"
            manifest_path = root / "splice_manifest.json"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=64x64:r=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-frames:v", "10", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=5",
                    "-frames:v", "3", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(replacement),
                ],
                check=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/harness_refine_video.py",
                    "--splice",
                    "--source-video", str(source),
                    "--replacement-video", str(replacement),
                    "--output-video", str(output),
                    "--start-frame", "2",
                    "--end-frame", "5",
                    "--mode", "replace",
                    "--manifest", str(manifest_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["output"]["frame_count"], 10)
            self.assertAlmostEqual(manifest["output"]["duration_seconds"], 2.0, delta=0.05)
            self.assertTrue(manifest["output"]["has_audio"])
            self.assertEqual(manifest["audio_strategy"], "copy_original_source_audio_stream")
            audio_md5 = lambda path: subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "md5", "-"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(audio_md5(source), audio_md5(output))
            self.assertEqual(manifest["output"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(json.loads(manifest_path.read_text()), manifest)

            full_spliced = root / "full_spliced.mp4"
            full_manifest = splice_video(
                source,
                source,
                full_spliced,
                start_frame=2,
                end_frame=5,
                mode="replace",
                manifest_path=root / "full_splice_manifest.json",
            )
            self.assertEqual(full_manifest["output"]["frame_count"], 10)
            self.assertEqual(audio_md5(source), audio_md5(full_spliced))

            inserted = root / "inserted.mp4"
            inserted_manifest = splice_video(
                source,
                replacement,
                inserted,
                start_frame=4,
                end_frame=4,
                mode="insert",
                manifest_path=root / "insert_manifest.json",
            )
            self.assertEqual(inserted_manifest["output"]["frame_count"], 13)

            with self.assertRaisesRegex(ValueError, "manifest must not overwrite"):
                splice_video(
                    source,
                    replacement,
                    root / "unsafe.mp4",
                    start_frame=4,
                    end_frame=4,
                    mode="insert",
                    manifest_path=source,
                )


if __name__ == "__main__":
    unittest.main()
