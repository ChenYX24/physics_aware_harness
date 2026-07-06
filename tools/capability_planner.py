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
        terms=("billiard", "billiards", "pool", "cue ball", "cue_ball", "台球", "白球", "目标球", "ball collision"),
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
        return {
            "schema_version": "capability_plan_v1",
            "prompt": prompt,
            "case_family": primary["case_family"],
            "primary_capability_id": primary["capability_id"],
            "matched_capabilities": matches or [primary],
            "failure_taxonomy_version": "physics_failure_taxonomy_v1",
            "execution_strategy": {
                "preferred_runtime": "UE",
                "fallback_runtime": "SIM_PROXY",
                "dry_run_supported": True,
                "requires_trajectory": True,
                "requires_contact_events": primary["capability_id"] in {"rigid_body_contact_causality", "billiard_causality_compiler", "sequential_contact_propagation"},
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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _term_matches(normalized_prompt: str, term: str) -> bool:
    normalized_term = _normalize(term)
    if not normalized_term:
        return False
    if re.search(r"[\u4e00-\u9fff]", normalized_term):
        return normalized_term in normalized_prompt
    return re.search(rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])", normalized_prompt) is not None
