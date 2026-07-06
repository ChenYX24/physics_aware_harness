from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]


PUBLIC_SOURCE_PATHS = [
    "README.md",
    "docs/HARNESS_ARCHITECTURE.md",
    "docs/AGENT_USAGE.md",
    "docs/CASE_SPEC_SCHEMA.md",
    "docs/ARTIFACT_SCHEMA.md",
    "docs/CAPABILITY_AUTHORING.md",
    "docs/OPTIONAL_VIEWER.md",
    "docs/PHYSICS_AWARE_HARNESS.md",
    "capabilities/prompt_case_capability_planning.json",
    "capabilities/explicit_physics_control_surface.json",
    "capabilities/physics_verifier_truth_gate.json",
    "capabilities/canonical_signal_capture.json",
    "capabilities/dataset_artifact_packaging.json",
    "capabilities/rigid_body_contact_causality.json",
    "capabilities/billiard_causality_compiler.json",
    "capabilities/rigid_body_gravity_collision.json",
    "capabilities/sequential_contact_propagation.json",
    "capabilities/ramp_sliding_friction.json",
    "capabilities/projectile_gravity_motion.json",
    "capabilities/bounce_restitution_ball.json",
    "capabilities/rolling_friction_ball.json",
    "capabilities/sliding_crate_friction.json",
    "capabilities/force_field_wind_drift.json",
    "capabilities/mass_ratio_momentum_transfer.json",
    "capabilities/angular_damping_spin_decay.json",
    "capabilities/agent_rigidbody_action_coupling.json",
    "capabilities/constraint_distance_pendulum_motion.json",
    "capabilities/capability_runtime_artifact_bridge.json",
    "capabilities/asset_intent_resolution.json",
    "capabilities/asset_runtime_binding_invocation.json",
    "capabilities/static_scene_placement.json",
    "capabilities/physics_property_constraint_validation.json",
    "capabilities/pipeline_stage_orchestration.json",
    "capabilities/scene_spec_compilation.json",
    "tools/causal_motion.py",
    "tools/capability_planner.py",
    "tools/capability_verifier.py",
    "tools/capability_closed_loop.py",
    "tools/capability_runtime_adapter.py",
    "tools/failure_taxonomy.py",
    "harness/core/capability.py",
    "harness/core/case_spec.py",
    "harness/assets/asset_intent.py",
    "harness/assets/asset_resolver.py",
    "harness/runtime/fallback_backend.py",
    "harness/runtime/artifact_collector.py",
    "harness/verification/physics_verifier.py",
    "harness/verification/billiards_verifier.py",
    "harness/verification/domino_verifier.py",
    "harness/verification/falling_verifier.py",
    "harness/verification/constraint_verifier.py",
    "tools/run_contract.py",
    "tools/draft_builder.py",
    "tools/dataset_protocol.py",
    "tools/case_verifier.py",
    "tests/test_run_contract.py",
    "tests/test_llm_object_graph_compiler.py",
    "tests/test_dataset_protocol.py",
    "tests/test_capability_closed_loop.py",
    "tests/test_capability_runtime_adapter.py",
    "tests/test_harness_capability_schema.py",
    "tests/test_harness_case_spec_schema.py",
    "tests/test_harness_billiards_verifier.py",
    "tests/test_harness_domino_verifier.py",
    "tests/test_harness_falling_verifier.py",
    "scripts/harness_list_capabilities.py",
    "scripts/harness_run_case.py",
    "scripts/harness_verify_run.py",
    "scripts/harness_smoke.py",
    "scripts/verify_capability_run.py",
    "config/frontend_vetted_cases.json",
    "config/case_registry.yaml",
]


LOCAL_SOURCE_PATHS = [
    "MEMORY.md",
    "ACTIVE_CONTEXT.md",
    "docs/PHYSICS_AWARE_HARNESS.md",
    "tools/causal_motion.py",
    "tools/capability_planner.py",
    "tools/capability_verifier.py",
    "tools/capability_closed_loop.py",
    "tools/capability_runtime_adapter.py",
    "tools/failure_taxonomy.py",
    "tools/run_contract.py",
    "tools/draft_builder.py",
    "tools/dataset_protocol.py",
    "tools/case_verifier.py",
    "tests/test_run_contract.py",
    "tests/test_llm_object_graph_compiler.py",
    "tests/test_dataset_protocol.py",
    "tests/test_capability_closed_loop.py",
    "tests/test_capability_runtime_adapter.py",
    "scripts/verify_capability_run.py",
    "config/frontend_vetted_cases.json",
    "config/case_registry.yaml",
    "agent-docs/check_report/清理与因果约束修复报告_2026-06-29-05-05.md",
    "agent-docs/check_report/GPT对象图台球开球Pipeline验证_2026-06-29-03-22.md",
    "agent-docs/check_report/项目完整汇报_2026-06-24_v2.md",
    "agent-docs/check_report/仓库完整使用报告_2026-06-30-04-00.md",
]


PRIVATE_PATH_PREFIXES = ("agent-docs/", "runs/", "outputs/", "_local" + "_inputs/", "MEMORY.md", "ACTIVE_CONTEXT.md")


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    title: str
    pattern_type: str
    stage_ids: tuple[str, ...]
    keywords: tuple[str, ...]
    description: str
    prompt_moves: tuple[str, ...]
    runtime_contract: tuple[str, ...]
    verifier_checks: tuple[str, ...]
    failure_modes: tuple[str, ...]
    iteration_moves: tuple[str, ...]


CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        capability_id="prompt_case_capability_planning",
        title="Prompt To Object-Level Scene Compiler",
        pattern_type="FLOW",
        stage_ids=("planner", "scene_spec", "asset_resolution"),
        keywords=(
            "object graph",
            "对象",
            "object-level",
            "scene blueprint",
            "asset intent",
            "scene_spec",
            "runtime_scene_request",
            "Prompt expansion",
        ),
        description="Compile natural language into explicit objects, roles, asset intents, camera needs, lighting, and runtime scene requests instead of a free-form video description.",
        prompt_moves=(
            "Ask for object roles and relationships, not only visual style.",
            "Separate active drivers, passive receivers, support surfaces, cameras, and lights.",
            "Require object ids that can survive asset binding, runtime execution, and verifier checks.",
        ),
        runtime_contract=(
            "Write object graph into spec.json, scene_spec.json, and runtime_scene_request.json.",
            "Preserve stable object ids from planning through trajectory and contact logs.",
        ),
        verifier_checks=(
            "All physics-critical object ids appear in runtime artifacts.",
            "Asset selections and scene graph entries can be traced back to planner intent.",
        ),
        failure_modes=(
            "Prompt remains visual-only and cannot drive physics.",
            "Object ids drift between planner, runtime, and verifier.",
            "A scene layout hides the physical event from all cameras.",
        ),
        iteration_moves=(
            "Add missing object roles to prompt expansion.",
            "Inspect asset_requests.json and runtime_scene_request.json before rerendering.",
            "Rerun planner with stricter object-id and role constraints.",
        ),
    ),
    CapabilitySpec(
        capability_id="rigid_body_contact_causality",
        title="Generic Rigid-Body Contact Causality",
        pattern_type="FLOW",
        stage_ids=("capability_planning", "case_spec_compilation", "scene_spec_compilation", "runtime_artifact_collection", "physics_verification"),
        keywords=(
            "台球",
            "billiard",
            "pool",
            "cue",
            "cue_ball",
            "ball collision",
            "collision",
            "contact graph",
            "target balls",
            "passive",
            "被动",
            "自走",
            "first active contact",
            "第一次",
            "causality",
            "初始速度",
            "initial velocity",
            "contact events",
        ),
        description="Compile active-to-passive rigid-body contact scenes into causal simulation programs where passive bodies move only after runtime contact evidence exists.",
        prompt_moves=(
            "Identify active bodies, passive bodies, collision graph, initial velocities, and expected contact propagation.",
            "Let the planner infer mass, shape, friction, and restitution, but keep passive initial velocity at zero unless explicitly active.",
            "Use speed wording as control intent while preserving contact-driven passive motion.",
        ),
        runtime_contract=(
            "Active bodies may receive initial velocity or impulse.",
            "Passive bodies start with zero unexplained linear and angular velocity.",
            "Collision geometry must not overlap at t=0.",
            "Trajectory and contact events are the only accepted source of passive post-contact motion.",
        ),
        verifier_checks=(
            "First active-to-passive contact exists.",
            "Passive pre-contact speed and displacement stay below threshold.",
            "Post-contact passive motion is explainable by the contact graph.",
            "Contact events include expected active-passive and optional secondary pairs.",
        ),
        failure_modes=(
            "Passive bodies move before contact because of initial velocity leakage.",
            "Passive bodies move before contact because colliders overlap at t=0.",
            "LLM scale is interpreted as radius in one stage and diameter in another.",
            "A visually good render is accepted without trajectory causality evidence.",
        ),
        iteration_moves=(
            "Inspect physics.json for passive initial velocities.",
            "Inspect first trajectory frames for target speed before first active contact.",
            "Adjust radius/extent and initial spacing before increasing speed.",
            "Only then sweep driver speed, restitution, and friction to tune spread.",
        ),
    ),
    CapabilitySpec(
        capability_id="asset_physics_binding",
        title="Typed Asset Physics Binding",
        pattern_type="HOW",
        stage_ids=("asset_resolution", "scene_spec", "physics_control", "verifier"),
        keywords=(
            "asset",
            "Asset",
            "资产",
            "physics-critical",
            "collider",
            "collision",
            "rigid body",
            "visual-only",
            "skeletal",
            "asset_selection",
            "asset_physics_index",
        ),
        description="Classify assets by physical role before simulation so meshes, colliders, rigid bodies, maps, skeletal assets, and visual-only materials are handled differently.",
        prompt_moves=(
            "Ask for asset needs per object role, not one global scene keyword.",
            "Retrieve top-k candidates and let model or policy select from typed candidates.",
            "Allow generated or proxy assets only with explicit replacement reasons.",
        ),
        runtime_contract=(
            "Physics-critical assets must bind collider, rigid body, mass, and collision graph entries.",
            "Visual-only assets may vary without changing physics.",
            "Skeletal and logic assets must declare whether they affect trajectory.",
        ),
        verifier_checks=(
            "Physics-critical assets appear in physics_control and trajectory/contact events.",
            "Visual-only assets are excluded from physics graph.",
            "Missing collider, wrong mass, wrong constraint, and missing binding are counted separately.",
        ),
        failure_modes=(
            "A pretty static mesh is used as if it had stable collision.",
            "A visual-only material silently changes physics semantics.",
            "Asset retrieval returns many candidates but no spawnable physics-critical option.",
        ),
        iteration_moves=(
            "Rebuild asset_physics_index.json after changing asset sources.",
            "Audit asset_candidates.json and asset_selection.json before runtime.",
            "Add role profiles for repeated physical object families.",
        ),
    ),
    CapabilitySpec(
        capability_id="rigid_body_gravity_collision",
        title="Rigid Body Gravity Collision",
        pattern_type="FLOW",
        stage_ids=("planner", "physics_control", "runtime", "verifier"),
        keywords=(
            "falling",
            "falling block",
            "falling_body",
            "stacking",
            "gravity",
            "ground",
            "collision enabled",
            "falling_crate_collision",
            "下落",
            "坠落",
            "重力",
            "堆叠",
        ),
        description="Compile falling or stacking prompts into gravity-driven rigid-body traces where objects descend under gravity and collide with a support surface instead of being visually animated.",
        prompt_moves=(
            "Identify falling bodies, support surfaces, and optional stacked receivers.",
            "Infer gravity, mass, collision shape, and collision enabled state explicitly.",
            "Avoid visual-only downward animation as a substitute for runtime trajectory evidence.",
        ),
        runtime_contract=(
            "Falling bodies start above support surfaces with gravity and collision enabled.",
            "Runtime trajectory shows decreasing height before contact.",
            "Contact events include ground, floor, support, or another rigid body.",
        ),
        verifier_checks=(
            "Gravity is enabled and non-zero.",
            "Falling body collision is enabled.",
            "Trajectory z decreases before first support contact.",
            "Ground/support contact is recorded.",
        ),
        failure_modes=(
            "Object is moved downward by visual keyframes without physics evidence.",
            "Gravity or collision is disabled on a falling body.",
            "Trajectory lacks support contact or passes through the floor.",
        ),
        iteration_moves=(
            "Fix initial height and support plane before changing material properties.",
            "Check trajectory z-series and contact pairs.",
            "Tune mass, restitution, damping, and solver substeps after gravity/contact validity passes.",
        ),
    ),
    CapabilitySpec(
        capability_id="sequential_contact_propagation",
        title="Sequential Contact Propagation",
        pattern_type="FLOW",
        stage_ids=("planner", "physics_control", "runtime", "verifier"),
        keywords=(
            "domino",
            "dominoes",
            "bottle_domino_chain",
            "chain reaction",
            "sequential contact",
            "contact propagation",
            "delayed causality",
            "ordered",
            "多米诺",
            "连锁反应",
            "链式碰撞",
            "依次",
        ),
        description="Compile domino or chain-reaction prompts into ordered contact propagation where only the first body is actively triggered and later bodies move after upstream contact.",
        prompt_moves=(
            "Define an ordered chain with stable ids.",
            "Allow only the first object to receive active trigger motion.",
            "Require later objects to remain passive until contact propagation reaches them.",
        ),
        runtime_contract=(
            "First chain object may have impulse or angular velocity.",
            "Subsequent chain objects start with zero linear and angular velocity.",
            "Trajectory records increasing tip/motion start times and adjacent contact pairs.",
        ),
        verifier_checks=(
            "Non-first objects have no initial motion.",
            "Tip or motion start times are non-decreasing along the chain.",
            "Adjacent contact pairs are present in order.",
        ),
        failure_modes=(
            "Later chain objects are pre-animated or pre-rotated.",
            "Contact pairs are missing even though objects appear to fall.",
            "Tip order is reversed or simultaneous beyond tolerance.",
        ),
        iteration_moves=(
            "Check initial angular velocity on all non-first objects.",
            "Inspect adjacent contact pair coverage.",
            "Adjust spacing, mass, friction, and angular damping after sequence validity passes.",
        ),
    ),
    CapabilitySpec(
        capability_id="explicit_physics_control_surface",
        title="Explicit Physics Control Surface",
        pattern_type="HOW",
        stage_ids=("planner", "physics_control", "benchmark"),
        keywords=(
            "physics_control",
            "physics controls",
            "material",
            "rigid_body",
            "friction",
            "restitution",
            "mass",
            "gravity",
            "solver",
            "parameter",
            "物理参数",
        ),
        description="Represent gravity, material, rigid-body, constraint, force, time, agent, and render-physics bridge controls as typed fields that can be replayed and swept.",
        prompt_moves=(
            "Use prompt wording to infer control intent, then write typed values with source/confidence.",
            "Do not bury physical assumptions in natural language notes.",
            "Lock camera, action, and initial state for parameter sensitivity tests.",
        ),
        runtime_contract=(
            "physics.json and physics_control carry deterministic values.",
            "Runtime reads controls directly rather than reinterpreting the prompt.",
            "Matched replay changes one control at a time.",
        ),
        verifier_checks=(
            "Schema-valid physics_control is present.",
            "Runtime echo of controls matches planner output.",
            "Single-parameter changes produce predictable trajectory or contact changes.",
        ),
        failure_modes=(
            "Implicit physics looks plausible once but cannot be reproduced.",
            "LLM changes multiple physical variables during a sensitivity test.",
            "Verifier can only check schema because trajectory evidence is absent.",
        ),
        iteration_moves=(
            "Run baseline, implicit, and structured-control variants.",
            "Sweep one parameter across fixed seeds.",
            "Compare reproduction variance and verifier pass rate.",
        ),
    ),
    CapabilitySpec(
        capability_id="canonical_signal_capture",
        title="Canonical Multi-Signal Capture",
        pattern_type="FLOW",
        stage_ids=("runtime", "signals", "dataset"),
        keywords=(
            "trajectory",
            "contact events",
            "camera trajectory",
            "render_pass_manifest",
            "depth",
            "normal",
            "audio",
            "RGB",
            "signals",
            "timebase",
            "同步",
            "五机位",
        ),
        description="Capture video and aligned evidence streams on one timebase: RGB, trajectory, contacts, camera path, depth proxy, normal proxy, audio, engine states, and semantic labels.",
        prompt_moves=(
            "Specify which evidence streams are needed for the case.",
            "Prefer fixed multi-view cameras for comparison and moving cameras for inspection.",
            "Treat render passes as synchronized artifacts, not UI decorations.",
        ),
        runtime_contract=(
            "Write render_pass_manifest.json and camera_trajectories.json.",
            "Align video frames, trajectory frames, contact events, and audio duration.",
            "Mark derived proxies separately from native UE render passes.",
        ),
        verifier_checks=(
            "Frame counts and timebase are consistent.",
            "Missing signals are classified instead of ignored.",
            "Each camera view has expected RGB and sidecar metadata.",
        ),
        failure_modes=(
            "A video exists but has no trajectory or contacts.",
            "Depth/normal proxies are mistaken for native GBuffer.",
            "Different views cannot be compared because camera/action changed.",
        ),
        iteration_moves=(
            "Run pass quality report before dataset packaging.",
            "Add missing view or signal manifests before rerendering a full suite.",
            "Keep camera/action/initial_state fixed for control sensitivity runs.",
        ),
    ),
    CapabilitySpec(
        capability_id="physics_verifier_truth_gate",
        title="Verifier As Runtime Truth Gate",
        pattern_type="HOW",
        stage_ids=("verifier", "dataset"),
        keywords=(
            "verifier",
            "reference_ready",
            "readiness",
            "truth gate",
            "physics_ready",
            "visual_ready",
            "failed_checks",
            "case_verification",
            "run_readiness",
        ),
        description="Use verifier output, not UI preview or successful rendering, as the final readiness decision for reference-ready samples.",
        prompt_moves=(
            "Ask for expected physical evidence when requesting a video.",
            "Keep human visual inspection separate from machine readiness.",
            "Treat preview filenames as legacy until verifier promotion.",
        ),
        runtime_contract=(
            "Write case_verification.json and run_readiness.json for every run.",
            "Store failed stage, failure reason, and machine-readable failed_checks.",
            "Dataset packager filters by verifier status.",
        ),
        verifier_checks=(
            "Video exists and is readable.",
            "Trajectory source and contact events are valid.",
            "Physics, visual, asset, and signal gates all pass.",
        ),
        failure_modes=(
            "Rendered output is accepted despite failed physics evidence.",
            "Schema passes but runtime behavior diverges.",
            "Legacy preview is mistaken for final video.",
        ),
        iteration_moves=(
            "Use verifier failure type to decide whether to replan, rebind assets, or rerun runtime.",
            "Promote only passing artifacts into dataset shards.",
            "Track verifier pass rate across seeds and prompt variants.",
        ),
    ),
    CapabilitySpec(
        capability_id="capability_runtime_artifact_bridge",
        title="Capability Runtime Artifact Bridge",
        pattern_type="BRIDGE",
        stage_ids=("runtime", "signals", "verifier"),
        keywords=(
            "capability_execution_trace_v1",
            "CapabilityRuntimeAdapter",
            "verify_capability_run",
            "ue_output",
            "debug_preview",
            "trajectory.json",
            "summary.json",
            "run_readiness.json",
            "render_pass_manifest.json",
            "source_type",
        ),
        description="Convert existing UE or fallback run artifacts into the capability verifier trace contract so real runtime evidence can be checked with the same causality rules as deterministic dry runs.",
        prompt_moves=(
            "Keep prompt-to-capability matching independent from backend artifact shape.",
            "Treat runtime output as evidence to adapt, not as a new planning schema.",
            "Preserve the original run id and output directory in every bridge report.",
        ),
        runtime_contract=(
            "Prefer ue_output over debug_preview when both exist.",
            "Read spec.json, summary.json, trajectory.json, run_readiness.json, and render_pass_manifest.json.",
            "Write capability_plan.json, capability_execution_trace.json, capability_verifier.json, and capability_diagnosis.md.",
        ),
        verifier_checks=(
            "Runtime source type is classified as UE, FALLBACK, SIM_PROXY, or UNKNOWN.",
            "Trajectory states are normalized to position_m, velocity_m_s, rotation_deg, and contacts.",
            "Capability verifier can reject real pre-contact passive motion, not only simulated trace failures.",
        ),
        failure_modes=(
            "A native UE render exists but cannot be checked for capability causality.",
            "Fallback and UE artifacts are mixed without source labels.",
            "Legacy preview video is mistaken for verifier-ready physical evidence.",
        ),
        iteration_moves=(
            "Run scripts/verify_capability_run.py on an existing run.",
            "Inspect capability_verifier.json before changing prompt or scene parameters.",
            "Use capability_diagnosis.md to decide whether the issue is planning, runtime, evidence, or verifier.",
        ),
    ),
    CapabilitySpec(
        capability_id="trajectory_benchmark_iteration",
        title="Trajectory-Based Benchmark Iteration",
        pattern_type="FLOW",
        stage_ids=("benchmark", "runtime", "verifier"),
        keywords=(
            "benchmark",
            "seed",
            "variance",
            "trajectory-based",
            "canonical trajectory",
            "stability",
            "MSE",
            "velocity drift",
            "failure clustering",
            "regression",
        ),
        description="Evaluate real runtime trajectories across seed sweeps and variants instead of dry control signals or mixed-backend stability numbers.",
        prompt_moves=(
            "Define scenario class and expected physical signal before running a benchmark.",
            "Keep seed, camera, action, and initial state stable for reproduction studies.",
            "Compare vanilla, implicit, and structured physics-control planners.",
        ),
        runtime_contract=(
            "Canonicalize trajectory source type before metrics.",
            "Split runtime, observability, and physics coverage.",
            "Calculate stability on UE-only subset and report weighted full-trajectory stability separately.",
        ),
        verifier_checks=(
            "Constraint violations are deduplicated.",
            "Missing timesteps are classified as observability gaps.",
            "Regression gate catches stability drop, constraint noise, invalid JSON, and test failures.",
        ),
        failure_modes=(
            "Fallback and UE are merged into one stability metric.",
            "Coverage is confused with runtime success.",
            "Constraint events are overcounted.",
        ),
        iteration_moves=(
            "Run full pipeline with seed sweep.",
            "Inspect coverage, signal purity, and stability reports.",
            "Cluster failures before changing planner or schema.",
        ),
    ),
    CapabilitySpec(
        capability_id="angular_damping_spin_decay",
        title="Angular Damping Spin Decay",
        pattern_type="physics_constraint",
        stage_ids=("physics_control", "runtime_artifact_collection", "physics_verification"),
        keywords=(
            "angular damping",
            "angular velocity",
            "rotation trace",
            "spin decay",
            "spinning body",
            "rotational damping",
            "角阻尼",
            "角速度",
            "旋转衰减",
            "自转",
        ),
        description="Validate rotational damping for spinning rigid bodies using explicit angular velocity, angular damping labels, rotation trace, and monotonic spin-decay evidence.",
        prompt_moves=(
            "Represent spin as angular velocity and damping controls, not visual-only rotation.",
            "Declare spin axis, initial angular velocity, angular damping, and expected decay envelope.",
            "Use seed/template generation to sweep damping and initial spin without changing object identity.",
        ),
        runtime_contract=(
            "Trajectory includes angular_velocity_deg_s and rotation_deg for the spinning body.",
            "Case spec includes expected_physics.initial_angular_speed_deg_s and expected_physics.angular_damping.",
            "Runtime does not allow angular speed gain without an explicit external torque label.",
        ),
        verifier_checks=(
            "Initial angular velocity and angular damping labels exist.",
            "Angular speed decays by the declared minimum amount.",
            "Final angular speed stays below the expected upper bound.",
            "Rotation delta proves the body actually spun.",
        ),
        failure_modes=(
            "Spin is only a visual rotation and no angular_velocity trace exists.",
            "Angular damping is declared but speed does not decay.",
            "Angular speed increases without external torque.",
        ),
        iteration_moves=(
            "Move spin controls into initial_angular_velocity_deg_s and expected_physics.angular_damping.",
            "Bind spinning bodies as physics-critical assets with inertia/damping metadata.",
            "Add negative cases for missing labels, no decay, and unexplained spin gain.",
        ),
    ),
    CapabilitySpec(
        capability_id="agent_rigidbody_action_coupling",
        title="Agent Action To Rigid-Body Coupling",
        pattern_type="physics_constraint",
        stage_ids=("action_trace_planning", "physics_control", "runtime_artifact_collection", "physics_verification"),
        keywords=(
            "agent pushes",
            "robot pushes",
            "character throws",
            "action trace",
            "agent-to-rigidbody",
            "推箱子",
            "智能体推",
            "机器人推",
            "动作轨迹",
        ),
        description="Validate that agent or controller actions cause rigid-body motion through explicit action trace, contact or impulse evidence, and post-action trajectory response.",
        prompt_moves=(
            "Represent agent intent as structured action_trace events with actor_id, target_id, action_type, frame, and time.",
            "Keep the target rigid body still before the action frame.",
            "Use contact evidence for push actions and impulse/release metadata for throw actions.",
        ),
        runtime_contract=(
            "Trajectory includes the agent and target rigid body with stable ids.",
            "Action trace is exported as action_trace.json and frame-level actions.",
            "Target post-action velocity or displacement exceeds the expected response threshold.",
        ),
        verifier_checks=(
            "Action trace exists and references known actor/target ids.",
            "Target initial/pre-action velocity stays below epsilon.",
            "Push actions have agent-target contact evidence.",
            "Throw actions have release or impulse metadata.",
            "Target moves after the action above the expected threshold.",
        ),
        failure_modes=(
            "Target has hidden pre-action motion.",
            "Motion is keyframed without structured action trace.",
            "Push action lacks contact evidence.",
            "Action trace exists but target never responds.",
        ),
        iteration_moves=(
            "Add action_trace to expected_physics or runtime artifacts.",
            "Bind the target as a physics-critical rigid body and the agent as an action-producing actor.",
            "Use negative cases for pre-action motion, missing action trace, and no post-action response.",
        ),
    ),
    CapabilitySpec(
        capability_id="constraint_distance_pendulum_motion",
        title="Distance Constraint Motion Validation",
        pattern_type="physics_constraint",
        stage_ids=("case_spec_compilation", "physics_control", "runtime_artifact_collection", "physics_verification"),
        keywords=(
            "pendulum",
            "distance constraint",
            "fixed length",
            "constraint length",
            "joint constraint",
            "rope constraint",
            "单摆",
            "摆锤",
            "距离约束",
            "固定长度",
            "约束长度",
        ),
        description="Validate fixed-distance or joint-constrained rigid-body motion using anchor/body trajectory, constraint length labels, constraint trace, and continuity checks.",
        prompt_moves=(
            "Represent the physical constraint as anchor/body ids, constraint_length_m, tolerance, and release condition.",
            "Use visual rope or chain assets only after the distance constraint is present in the physics graph.",
            "Generate positive and negative cases by sweeping length, release angle, tolerance, and solver drift.",
        ),
        runtime_contract=(
            "Trajectory includes anchor and constrained body positions at every frame.",
            "constraint_trace.json records constraint_id, anchor_id, body_id, expected length, and measured distance.",
            "Runtime exports enough data to detect length drift and teleporting constrained bodies.",
        ),
        verifier_checks=(
            "Constraint length label is present and positive.",
            "Anchor-body distance stays within tolerance for every frame.",
            "Constrained body motion is continuous and below teleport threshold.",
            "Pendulum smoke cases cross or approach the center line after release.",
        ),
        failure_modes=(
            "Constraint is rendered as a visual rope with no physics label.",
            "Anchor/body distance drifts beyond tolerance.",
            "Constrained body teleports between frames.",
            "Runtime artifact misses constraint_trace or object roles.",
        ),
        iteration_moves=(
            "Export constraint_trace before tuning rope visuals.",
            "Fix constraint_length_m, anchor/body ids, and solver timestep before changing camera or materials.",
            "Add negative cases for missing constraint label, length drift, and teleporting body.",
        ),
    ),
    CapabilitySpec(
        capability_id="dataset_artifact_packaging",
        title="Verified Multi-View Dataset Packaging",
        pattern_type="FLOW",
        stage_ids=("dataset", "signals", "verifier"),
        keywords=(
            "dataset",
            "shard",
            "multi-view",
            "多机位",
            "RGB",
            "depth",
            "normal",
            "audio",
            "semantic annotations",
            "gravity labels",
            "package_dataset",
        ),
        description="Package only readiness-gated runs into a dataset layout with video, synchronized signals, physics labels, asset metadata, and hashes.",
        prompt_moves=(
            "Request dataset-grade evidence explicitly when generating cases.",
            "Use fixed view names for comparable samples.",
            "Separate debug/evidence runs from training-ready samples.",
        ),
        runtime_contract=(
            "Materialized shards contain samples/{sample_id}/videos, signals, audio, depth, and normal.",
            "Manifest records quality tier, source paths, hashes, and signal availability.",
            "Failed or incomplete samples stay visible but are not training-ready.",
        ),
        verifier_checks=(
            "Readiness gate overrides legacy status.",
            "Signal manifest and dataset manifest agree.",
            "Sample count and failed_or_incomplete counts are explicit.",
        ),
        failure_modes=(
            "A debug run is silently included as training data.",
            "Dataset docs overclaim native signal quality.",
            "Hashing or materialization misses a required sidecar.",
        ),
        iteration_moves=(
            "Run package_dataset.py after verifier updates.",
            "Audit sample tiers before publishing.",
            "Use benchmark taxonomy to grow cases systematically.",
        ),
    ),
    CapabilitySpec(
        capability_id="failure_driven_refinement_loop",
        title="Failure-Driven Replanning And Refinement",
        pattern_type="BRIDGE",
        stage_ids=("planner", "runtime", "verifier", "benchmark"),
        keywords=(
            "failure",
            "failed",
            "失败",
            "refine",
            "iterate",
            "迭代",
            "root cause",
            "reason",
            "fix",
            "修复",
            "重新跑",
            "多轮",
        ),
        description="Use structured failure evidence to decide the next minimal change: prompt expansion, asset binding, scene layout, physics controls, runtime settings, or verifier thresholds.",
        prompt_moves=(
            "Ask for outcome intent, constraints, and evidence streams, then let the harness fill details.",
            "Do not overfit one case with hidden hardcoded parameters.",
            "Keep each iteration's hypothesis explicit.",
        ),
        runtime_contract=(
            "Every failed run writes failure type, root cause, and artifact links.",
            "Refinement should diff the previous spec/control/runtime evidence.",
            "One iteration should change the smallest responsible stage.",
        ),
        verifier_checks=(
            "Failure mode is categorized.",
            "Next-step plan is present.",
            "Regression tests protect previously fixed failures such as pre-contact passive motion.",
        ),
        failure_modes=(
            "Agent adds a template instead of fixing the control surface.",
            "Multiple stages change at once and root cause becomes untraceable.",
            "A passing single run hides instability across seeds.",
        ),
        iteration_moves=(
            "Mine failure evidence from verifier and trajectory.",
            "Change exactly one stage owner file or prompt contract.",
            "Rerun tests and a small prompt suite before publishing.",
        ),
    ),
    CapabilitySpec(
        capability_id="lineage_based_capability_extraction",
        title="Lineage-Based Harness Capability Extraction",
        pattern_type="BRIDGE",
        stage_ids=("meta", "docs", "tests"),
        keywords=(
            "WHAT",
            "HOW",
            "FLOW",
            "BRIDGE",
            "skill",
            "技能",
            "memory",
            "lineage",
            "提取",
            "复盘",
            "pattern",
        ),
        description="Mine repeated project memory, reports, and final artifacts into reusable harness capabilities without publishing raw private sessions or secrets.",
        prompt_moves=(
            "Start from project memory and final artifacts, then re-anchor claims to source files.",
            "Classify each learning as WHAT, HOW, FLOW, or BRIDGE.",
            "Distill private session evidence into public, sanitized capability profiles.",
        ),
        runtime_contract=(
            "Generated public profiles exclude raw logs, secrets, run outputs, and local-only agent docs.",
            "Internal extraction reports may cite local memory and private reports but are not exported.",
            "Capability profiles include modification points and validation commands.",
        ),
        verifier_checks=(
            "Generated profile contains no keys or private archive paths.",
            "Each capability has evidence, failure modes, and iteration moves.",
            "Docs point users to executable owner files and tests.",
        ),
        failure_modes=(
            "A report becomes a generic essay with no owner files.",
            "Private session logs leak into the public repo.",
            "The extracted skill overfits to the billiard case and cannot generalize.",
        ),
        iteration_moves=(
            "Run local extraction from memory and reports.",
            "Run public extraction from README, docs, tests, and code.",
            "Diff the capability profile after new benchmark or prompt iterations.",
        ),
    ),
)


SECRET_PATTERNS = (
    re.compile("s" + r"k-[A-Za-z0-9]{20,}"),
    re.compile("m" + r"s-[0-9A-Fa-f-]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
)


def extract_capability_profile(
    root: Path = ROOT,
    *,
    source_paths: Iterable[str] | None = None,
    source_preset: str = "public",
    max_evidence_per_capability: int = 8,
    include_private_sources: bool = False,
) -> dict[str, Any]:
    """Extract a deterministic, sanitized physics-aware harness capability profile."""
    paths = list(source_paths or (LOCAL_SOURCE_PATHS if source_preset == "local" else PUBLIC_SOURCE_PATHS))
    source_lines = load_source_lines(root, paths)
    capabilities = [
        build_capability(spec, source_lines, max_evidence_per_capability=max_evidence_per_capability)
        for spec in CAPABILITY_SPECS
    ]
    if not include_private_sources:
        capabilities = [sanitize_private_evidence(capability) for capability in capabilities]
    return {
        "schema_version": "physics_aware_harness_capabilities_v1",
        "source_preset": source_preset,
        "source_policy": {
            "raw_sessions_committed": False,
            "private_agent_docs_committed": False,
            "publishable_profile_sanitized": not include_private_sources,
            "extraction_methods": [
                "WHAT/HOW/FLOW/BRIDGE pattern mining",
                "decision-chain re-anchoring",
                "failure taxonomy extraction",
                "capability quality checklist",
            ],
        },
        "capability_count": len(capabilities),
        "capabilities": capabilities,
        "contact_causality_reference_workflow": contact_causality_reference_workflow(),
        "iteration_playbook": iteration_playbook(source_preset),
    }


def load_source_lines(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for rel_path in paths:
        path = root / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            clean = line.strip()
            if not clean or contains_secret(clean):
                continue
            lines.append({"path": rel_path, "line": line_number, "text": clean})
    return lines


def build_capability(
    spec: CapabilitySpec,
    source_lines: list[dict[str, Any]],
    *,
    max_evidence_per_capability: int,
) -> dict[str, Any]:
    scored: list[dict[str, Any]] = []
    lowered_keywords = tuple(keyword.lower() for keyword in spec.keywords)
    for item in source_lines:
        text = str(item["text"])
        lower = text.lower()
        matched = [spec.keywords[index] for index, keyword in enumerate(lowered_keywords) if keyword and keyword in lower]
        if not matched:
            continue
        score = len(matched)
        if spec.capability_id == "rigid_body_contact_causality" and any(term in lower for term in ("billiard", "台球", "cue_ball", "contact")):
            score += 2
        if any(term in lower for term in ("fail", "失败", "violation", "reference_ready=false", "自走")):
            score += 1
        scored.append({**item, "matched_terms": sorted(set(matched)), "score": score})
    scored.sort(key=lambda value: (-int(value["score"]), str(value["path"]), int(value["line"])))
    evidence = [
        {
            "source": f"{item['path']}:{item['line']}",
            "matched_terms": item["matched_terms"],
            "text": truncate(str(item["text"]), 220),
        }
        for item in scored[:max_evidence_per_capability]
    ]
    evidence_count = len(scored)
    confidence = min(0.95, 0.35 + min(evidence_count, 12) * 0.05)
    return {
        "id": spec.capability_id,
        "title": spec.title,
        "pattern_type": spec.pattern_type,
        "stage_ids": list(spec.stage_ids),
        "description": spec.description,
        "confidence": round(confidence, 2),
        "evidence_count": evidence_count,
        "evidence": evidence,
        "prompt_moves": list(spec.prompt_moves),
        "runtime_contract": list(spec.runtime_contract),
        "verifier_checks": list(spec.verifier_checks),
        "failure_modes": list(spec.failure_modes),
        "iteration_moves": list(spec.iteration_moves),
    }


def sanitize_private_evidence(capability: dict[str, Any]) -> dict[str, Any]:
    clean = dict(capability)
    evidence = []
    hidden_count = 0
    for item in capability.get("evidence") or []:
        source = str(item.get("source") or "")
        if source.startswith(PRIVATE_PATH_PREFIXES):
            hidden_count += 1
            continue
        evidence.append(item)
    clean["evidence"] = evidence
    if hidden_count:
        clean["private_evidence_suppressed"] = hidden_count
    return clean


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def contact_causality_reference_workflow() -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "name": "Compile object graph",
            "contract": "Create active driver bodies and passive receiver bodies with stable ids.",
        },
        {
            "step": 2,
            "name": "Bind physical assets",
            "contract": "Use colliders, mass, rigid body, material, and no-overlap semantics for every physics-critical object.",
        },
        {
            "step": 3,
            "name": "Set physics controls",
            "contract": "Active velocity or impulse encodes requested action; passive bodies start below velocity epsilon.",
        },
        {
            "step": 4,
            "name": "Execute runtime",
            "contract": "Runtime, not the LLM or keyframes, produces passive motion through contact events.",
        },
        {
            "step": 5,
            "name": "Verify causality",
            "contract": "Reject if passive bodies move above threshold before first causal contact.",
        },
        {
            "step": 6,
            "name": "Iterate controls",
            "contract": "Tune speed, restitution, friction, spacing, mass ratio, and solver substeps only after causality passes.",
        },
    ]


def iteration_playbook(source_preset: str = "public") -> list[dict[str, Any]]:
    mine_input = (
        "README/docs, tests, runtime code, and public config"
        if source_preset == "public"
        else "project memory, active context, private check reports, README/docs, tests, and runtime code"
    )
    return [
        {
            "phase": "mine",
            "input": mine_input,
            "output": "candidate capabilities with evidence cards",
            "rule": "Use WHAT/HOW/FLOW/BRIDGE extraction; do not publish raw sessions.",
        },
        {
            "phase": "distill",
            "input": "local capability candidates",
            "output": "publishable config/harness_capability_profile.json",
            "rule": "Remove private paths, secrets, and raw logs; keep owner files and checks.",
        },
        {
            "phase": "exercise",
            "input": "prompt cases and capability profile",
            "output": "draft/run artifacts and verifier failures",
            "rule": "Run several prompts and classify failures by stage before changing code.",
        },
        {
            "phase": "tighten",
            "input": "failure clusters",
            "output": "stage-specific prompt, asset, runtime, verifier, or benchmark patch",
            "rule": "Change one responsible stage at a time and rerun tests.",
        },
    ]


def render_markdown_report(profile: dict[str, Any]) -> str:
    lines = [
        "# Physics-Aware Harness Capability Profile",
        "",
        "This report is generated from project memory, docs, tests, and runtime code. It distills reusable harness capabilities without committing raw private sessions.",
        "",
        "## Extraction Method",
        "",
    ]
    for method in profile["source_policy"]["extraction_methods"]:
        lines.append(f"- {method}")
    lines.extend(["", "## Capability Summary", ""])
    for capability in profile["capabilities"]:
        lines.append(f"### {capability['title']}")
        lines.append("")
        lines.append(f"- id: `{capability['id']}`")
        lines.append(f"- pattern: `{capability['pattern_type']}`")
        lines.append(f"- stages: `{', '.join(capability['stage_ids'])}`")
        lines.append(f"- confidence: `{capability['confidence']}` from `{capability['evidence_count']}` matched lines")
        if capability.get("private_evidence_suppressed"):
            lines.append(f"- private evidence suppressed: `{capability['private_evidence_suppressed']}`")
        lines.append("")
        lines.append(capability["description"])
        lines.append("")
        lines.append("Key iteration moves:")
        for move in capability["iteration_moves"][:3]:
            lines.append(f"- {move}")
        if capability.get("evidence"):
            lines.append("")
            lines.append("Evidence:")
            for evidence in capability["evidence"][:3]:
                lines.append(f"- `{evidence['source']}`: {evidence['text']}")
        lines.append("")
    lines.extend(["## Rigid-Body Contact Reference Workflow", ""])
    for step in profile["contact_causality_reference_workflow"]:
        lines.append(f"{step['step']}. **{step['name']}**: {step['contract']}")
    lines.extend(["", "## Closed-Loop Demo Cases", ""])
    lines.append("| Case | Capability | Verified Contract |")
    lines.append("|---|---|---|")
    lines.append("| Contact causality, including billiards | `rigid_body_contact_causality` | Active bodies move first; passive bodies move only after contact propagation |")
    lines.append("| Falling blocks | `rigid_body_gravity_collision` | Gravity/collision are enabled, z decreases, and support contact is recorded |")
    lines.append("| Domino chain | `sequential_contact_propagation` | First domino is actively triggered; downstream dominoes tip through ordered adjacent contacts |")
    lines.append("| Spin decay | `angular_damping_spin_decay` | Angular velocity and damping are explicit, and angular speed decays without unexplained gain |")
    lines.append("| Distance constraint / pendulum | `constraint_distance_pendulum_motion` | Anchor-body length stays within tolerance, motion is continuous, and constraint trace is exported |")
    lines.extend(["", "## Iteration Playbook", ""])
    for phase in profile["iteration_playbook"]:
        lines.append(f"- **{phase['phase']}**: {phase['rule']}")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract physics-aware harness capabilities from project evidence.")
    parser.add_argument("--root", default=str(ROOT), help="Repository root.")
    parser.add_argument("--source-preset", choices=("public", "local"), default="public")
    parser.add_argument("--source", action="append", default=[], help="Additional source path relative to root. Can be repeated.")
    parser.add_argument("--output", default="config/harness_capability_profile.json")
    parser.add_argument("--report-output", default="docs/PHYSICS_AWARE_CAPABILITIES.md")
    parser.add_argument("--max-evidence", type=int, default=8)
    parser.add_argument("--include-private-sources", action="store_true", help="Keep private source references in evidence. Use only for local reports.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    base_sources = list(LOCAL_SOURCE_PATHS if args.source_preset == "local" else PUBLIC_SOURCE_PATHS)
    source_paths = base_sources + list(args.source or [])
    profile = extract_capability_profile(
        root,
        source_paths=source_paths,
        source_preset=args.source_preset,
        max_evidence_per_capability=args.max_evidence,
        include_private_sources=args.include_private_sources,
    )
    write_json(root / args.output, profile)
    write_text(root / args.report_output, render_markdown_report(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
