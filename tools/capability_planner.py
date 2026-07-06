from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = ROOT / "config" / "harness_capability_profile.json"


@dataclass(frozen=True)
class CapabilityRule:
    capability_id: str
    case_family: str
    terms: tuple[str, ...]
    required_terms: tuple[str, ...] = ()
    priority: int = 0


CAPABILITY_RULES: tuple[CapabilityRule, ...] = (
    CapabilityRule(
        capability_id="rigid_body_contact_causality",
        case_family="rigid_body_contact",
        terms=("billiard", "billiards", "pool", "cue ball", "cue_ball", "bowling", "pins", "rigid-body impact", "rigid body impact", "crate impact", "mass ratio collision", "台球", "白球", "目标球", "保龄球", "球瓶", "刚体碰撞", "ball collision"),
        priority=100,
    ),
    CapabilityRule(
        capability_id="rigid_body_gravity_collision",
        case_family="falling_blocks",
        terms=("falling block", "falling blocks", "falling crate", "falling object", "stacking", "gravity", "下落", "坠落", "堆叠", "重力", "落到地面"),
        priority=90,
    ),
    CapabilityRule(
        capability_id="sequential_contact_propagation",
        case_family="domino_chain",
        terms=("domino", "dominoes", "chain reaction", "sequential contact", "contact propagation", "多米诺", "连锁反应", "链式碰撞", "依次倒下"),
        priority=95,
    ),
    CapabilityRule(
        capability_id="ramp_sliding_friction",
        case_family="ramp_sliding",
        terms=("ramp", "inclined plane", "slope", "sliding down", "rolling down", "斜面", "坡道", "下滑", "滚下", "摩擦斜坡"),
        priority=85,
    ),
    CapabilityRule(
        capability_id="projectile_gravity_motion",
        case_family="projectile_motion",
        terms=("projectile", "throw", "thrown", "upward throw", "launch angle", "parabolic", "抛体", "上抛", "抛出", "投掷", "发射角度"),
        priority=84,
    ),
    CapabilityRule(
        capability_id="bounce_restitution_ball",
        case_family="bounce_restitution",
        terms=("bounce", "bouncing", "rebound", "restitution", "drop and bounce", "弹跳", "反弹", "恢复系数", "落地反弹", "皮球反弹"),
        priority=83,
    ),
    CapabilityRule(
        capability_id="rolling_friction_ball",
        case_family="rolling_friction",
        terms=("rolling friction", "rolls on a floor", "rolling distance", "roll and stop", "frictional rolling", "滚动摩擦", "滚动距离", "滚动停止", "地面滚动", "球滚动"),
        priority=82,
    ),
    CapabilityRule(
        capability_id="sliding_crate_friction",
        case_family="sliding_crate_friction",
        terms=("sliding crate", "sliding friction", "crate slides", "slide and stop", "static friction threshold", "滑动摩擦", "箱子滑动", "滑动停止", "静摩擦阈值", "推不动"),
        priority=81,
    ),
    CapabilityRule(
        capability_id="force_field_wind_drift",
        case_family="wind_balloon_drift",
        terms=("wind", "wind field", "wind drift", "balloon", "light body drift", "force field", "air drag", "gust", "风", "风场", "气球", "漂移", "风吹", "轻物体"),
        priority=80,
    ),
    CapabilityRule(
        capability_id="magnetic_force_field",
        case_family="magnetic_force_field",
        terms=("magnetic", "magnet", "magnetic force", "magnetic field", "attract", "repel", "magnetic attraction", "magnetic repulsion", "磁", "磁场", "磁吸", "吸引", "排斥", "相斥"),
        priority=89,
    ),
    CapabilityRule(
        capability_id="mass_ratio_momentum_transfer",
        case_family="mass_ratio_collision",
        terms=("mass ratio", "momentum transfer", "heavy striker", "light target", "lighter target", "light striker", "heavy target", "heavier target", "collision mass", "mass-dependent collision", "动量传递", "质量比", "重物撞轻物", "轻物撞重物", "更轻目标", "更重目标", "碰撞后速度"),
        priority=106,
    ),
    CapabilityRule(
        capability_id="brittle_impact_fracture",
        case_family="brittle_impact_fracture",
        terms=("brittle", "fracture", "shatter", "breakable", "destructible", "glass break", "glass shatter", "mirror breaks", "wood crate breaks", "crate breaks", "破碎", "碎裂", "断裂", "玻璃碎", "镜子碎", "玻璃杯碎", "木箱破碎", "可破坏"),
        priority=108,
    ),
    CapabilityRule(
        capability_id="angular_damping_spin_decay",
        case_family="angular_damping_spin",
        terms=("angular damping", "spin decay", "spinning body", "angular velocity", "rotational damping", "spin slows", "spin down", "角阻尼", "角速度", "旋转衰减", "自转", "旋转变慢"),
        priority=79,
    ),
    CapabilityRule(
        capability_id="agent_rigidbody_action_coupling",
        case_family="agent_rigidbody_action",
        terms=("agent pushes", "agent push", "robot pushes", "robot push", "character pushes", "character throws", "agent throws", "agent throw", "robot throws", "action trace", "agent-to-rigidbody", "agent rigid body", "推箱子", "智能体推", "机器人推", "角色推", "智能体抛", "角色抛", "动作轨迹"),
        priority=107,
    ),
    CapabilityRule(
        capability_id="constraint_distance_pendulum_motion",
        case_family="constraint_distance_motion",
        terms=("pendulum", "swinging pendulum", "distance constraint", "fixed length", "constraint length", "joint constraint", "rope constraint", "hinge constraint", "单摆", "摆锤", "距离约束", "固定长度", "约束长度", "铰链约束", "绳长约束"),
        priority=78,
    ),
    CapabilityRule(
        capability_id="constraint_momentum_transfer",
        case_family="constrained_impulse_chain",
        terms=("newton cradle", "newton's cradle", "impulse chain", "constrained impulse", "momentum chain", "chain momentum transfer", "suspended ball chain", "constraint momentum transfer", "牛顿摆", "冲量链", "动量链", "悬挂球", "受约束动量传递"),
        priority=88,
    ),
    CapabilityRule(
        capability_id="elastic_energy_launch",
        case_family="elastic_energy_launch",
        terms=("spring launch", "spring launcher", "compressed spring", "elastic launch", "elastic energy", "catapult", "弹簧", "弹簧发射", "压缩弹簧", "弹射", "弹性势能"),
        priority=87,
    ),
    CapabilityRule(
        capability_id="elastic_constraint_rebound",
        case_family="elastic_constraint_rebound",
        terms=("bungee", "elastic rope", "elastic tether", "stretchy rope", "rope rebound", "tether rebound", "elastic constraint", "蹦极", "弹性绳", "弹力绳", "弹性约束", "绳子回弹", "拉伸回弹"),
        priority=86,
    ),
)


PIPELINE_STAGE_CAPABILITIES: tuple[dict[str, str], ...] = (
    {"stage": "prompt_to_case", "capability_id": "prompt_case_capability_planning"},
    {"stage": "asset_intent_resolution", "capability_id": "asset_intent_resolution"},
    {"stage": "scene_spec_compilation", "capability_id": "scene_spec_compilation"},
    {"stage": "static_scene_placement", "capability_id": "static_scene_placement"},
    {"stage": "asset_runtime_binding", "capability_id": "asset_runtime_binding_invocation"},
    {"stage": "runtime_artifact_bridge", "capability_id": "capability_runtime_artifact_bridge"},
    {"stage": "signal_capture", "capability_id": "canonical_signal_capture"},
    {"stage": "physics_verification", "capability_id": "physics_verifier_truth_gate"},
    {"stage": "dataset_packaging", "capability_id": "dataset_artifact_packaging"},
)


GENERIC_PHYSICS_CONTROL_CAPABILITIES: tuple[str, ...] = (
    "explicit_physics_control_surface",
    "physics_property_constraint_validation",
)


ASSET_OPERATION_CAPABILITIES: tuple[str, ...] = (
    "asset_intent_resolution",
    "asset_runtime_binding_invocation",
)


class CapabilityProfile:
    def __init__(self, path: str | Path = DEFAULT_PROFILE_PATH) -> None:
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.capabilities = {str(item.get("id")): item for item in self.data.get("capabilities") or [] if item.get("id")}

    def get(self, capability_id: str) -> dict[str, Any]:
        return dict(self.capabilities.get(capability_id) or {})

    def has(self, capability_id: str) -> bool:
        return capability_id in self.capabilities


class CapabilityPlanner:
    def __init__(self, profile_path: str | Path = DEFAULT_PROFILE_PATH) -> None:
        self.profile = CapabilityProfile(profile_path)

    def plan(self, prompt: str) -> dict[str, Any]:
        matches = self.match(prompt)
        primary = matches[0] if matches else self._fallback_match(prompt)
        capability_layers = self._capability_layers(primary["capability_id"])
        return {
            "schema_version": "capability_plan_v1",
            "prompt": prompt,
            "case_family": primary["case_family"],
            "primary_capability_id": primary["capability_id"],
            "matched_capabilities": matches or [primary],
            "supporting_capabilities": sorted(set(capability_layers["all_capability_ids"]) - {primary["capability_id"]}),
            "capability_layers": capability_layers,
            "failure_taxonomy_version": "physics_failure_taxonomy_v1",
            "execution_strategy": {
                "preferred_runtime": "UE",
                "fallback_runtime": "SIM_PROXY",
                "dry_run_supported": True,
                "requires_trajectory": True,
                "requires_contact_events": primary["capability_id"] in {"rigid_body_contact_causality", "sequential_contact_propagation", "constraint_momentum_transfer", "brittle_impact_fracture"},
            },
        }

    def match(self, prompt: str) -> list[dict[str, Any]]:
        normalized = _normalize(prompt)
        matches = []
        for rule in CAPABILITY_RULES:
            matched_terms = [term for term in rule.terms if _term_matches(normalized, term)]
            if not matched_terms:
                continue
            if rule.required_terms and not all(_term_matches(normalized, term) for term in rule.required_terms):
                continue
            capability = self.profile.get(rule.capability_id)
            matches.append(
                {
                    "capability_id": rule.capability_id,
                    "title": capability.get("title") or rule.capability_id,
                    "case_family": rule.case_family,
                    "stage_ids": capability.get("stage_ids") or [],
                    "matched_terms": matched_terms,
                    "score": rule.priority + len(matched_terms),
                    "reason": f"matched prompt terms: {', '.join(matched_terms[:6])}",
                    "verifier_checks": capability.get("verifier_checks") or [],
                }
            )
        matches.sort(key=lambda item: (-int(item["score"]), str(item["capability_id"])))
        return matches

    def _fallback_match(self, prompt: str) -> dict[str, Any]:
        capability = self.profile.get("explicit_physics_control_surface")
        return {
            "capability_id": "explicit_physics_control_surface",
            "title": capability.get("title") or "Explicit Physics Control Surface",
            "case_family": "generic_physics_control",
            "stage_ids": capability.get("stage_ids") or [],
            "matched_terms": [],
            "score": 0,
            "reason": "no specialized capability matched; using generic physics control surface",
            "verifier_checks": capability.get("verifier_checks") or [],
        }

    def _capability_layers(self, primary_capability_id: str) -> dict[str, Any]:
        physics_constraints = [primary_capability_id, *GENERIC_PHYSICS_CONTROL_CAPABILITIES]
        if primary_capability_id == "explicit_physics_control_surface":
            physics_constraints = list(GENERIC_PHYSICS_CONTROL_CAPABILITIES)
        all_ids = [
            primary_capability_id,
            *GENERIC_PHYSICS_CONTROL_CAPABILITIES,
            *ASSET_OPERATION_CAPABILITIES,
            *(item["capability_id"] for item in PIPELINE_STAGE_CAPABILITIES),
        ]
        return {
            "pipeline_stages": [
                {
                    **stage,
                    "title": self.profile.get(stage["capability_id"]).get("title") or stage["capability_id"],
                }
                for stage in PIPELINE_STAGE_CAPABILITIES
            ],
            "physics_constraints": [
                {
                    "capability_id": capability_id,
                    "title": self.profile.get(capability_id).get("title") or capability_id,
                }
                for capability_id in dict.fromkeys(physics_constraints)
            ],
            "asset_operations": [
                {
                    "capability_id": capability_id,
                    "title": self.profile.get(capability_id).get("title") or capability_id,
                }
                for capability_id in ASSET_OPERATION_CAPABILITIES
            ],
            "verification": [
                {
                    "capability_id": "physics_verifier_truth_gate",
                    "title": self.profile.get("physics_verifier_truth_gate").get("title") or "Physics Verifier Truth Gate",
                }
            ],
            "all_capability_ids": list(dict.fromkeys(all_ids)),
        }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _term_matches(normalized_prompt: str, term: str) -> bool:
    normalized_term = _normalize(term)
    if not normalized_term:
        return False
    if re.search(r"[\u4e00-\u9fff]", normalized_term):
        return normalized_term in normalized_prompt
    return re.search(rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])", normalized_prompt) is not None
