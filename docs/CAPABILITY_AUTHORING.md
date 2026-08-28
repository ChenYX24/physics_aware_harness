# Capability Authoring

## 边界

不要为新的物理现象新增 capability、runtime mode、fallback 函数或 verifier。台球、坠落、连续碰撞、泼洒、反弹等都是 CaseSpec 实例，不是软件能力类别。

只有以下扩展值得新增 active capability：

- 新的通用 state representation；
- 新的 solver primitive 或 coupling；
- 新的 backend adapter；
- 新的 pipeline/asset/runtime bridge；
- 新的通用 measurement reduction 或 assertion operator；
- 新的 artifact/readiness gate。

## 当前物理 capability

- `rigid_body_dynamics`
- `fluid_particle_dynamics`
- `deformable_body_dynamics`

旧现象 capability 只能是 `compatibility_alias`，不得进入 active profile，也不得拥有专用 verifier。

## 实现顺序

1. 扩展 CaseSpec 的通用字段和结构校验。
2. 在 `physics_contract` 中声明状态域/backend 支持关系；不要读取 prompt 或 case id。
3. 扩展 backend adapter，使其消费通用字段。
4. 如需验证，扩展通用 assertion vocabulary，而不是新增 `<family>_verifier.py`。
5. 增加至少两个语义不同、但使用同一 primitive 的 case 回归。
6. 更新 `tests/test_no_process_dispatch.py`，证明旧现象标签不会改变执行。

## 验证

```bash
python scripts/harness_list_capabilities.py --json
conda run -n base python -m unittest tests.test_no_process_dispatch
conda run -n base python -m unittest discover -s tests -p 'test_*.py'
```
