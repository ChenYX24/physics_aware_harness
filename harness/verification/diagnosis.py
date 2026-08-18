from __future__ import annotations


REPAIR_SUGGESTIONS = {
    "F1_missing_trajectory": "重新运行 runtime，确保写出 trajectory.json。",
    "F2_missing_contact_events": "启用 contact event capture，并检查 collision profile / collider。",
    "F3_initial_overlap": "调整初始位置或半径，避免 t=0 collider 重叠。",
    "F4_causality_violation": "移除被动对象的隐藏初始运动，只允许 contact 后传播运动。",
    "F5_passive_precontact_motion": "将 passive object 初始速度清零，并检查 runtime 是否在 contact 前施加了速度。",
    "F6_unexplained_motion_after_render": "检查是否存在 keyframe/visual-only animation 伪造物理运动。",
    "F7_runtime_artifact_incomplete": "补齐 summary、readiness、render pass manifest、trajectory/contact sidecars。",
    "F_RUNTIME_CONSTRAINT_ENFORCEMENT_FAILED": "检查约束残差、运行时刚体状态和求解器约束执行；不要用布局修订掩盖运行时漂移。",
}


def repair_suggestion(failure_type: str | None) -> list[str]:
    if not failure_type:
        return ["当前 verifier 未发现阻塞问题。"]
    return [REPAIR_SUGGESTIONS.get(failure_type, "检查 case spec、trajectory 和 contact events 是否一致。")]
