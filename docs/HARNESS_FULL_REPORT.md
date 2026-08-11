# Physics-aware Harness 当前实现报告

## 结论

Harness 只保留一个声明式求解契约。自然语言中的“坠落、连续撞击、多米诺、上抛、泼洒”等名称不是 capability、runtime mode、backend 路由键或 verifier 类型。它们只能出现在用户请求、case 说明和回归样例中。

统一不等于只使用一个数值引擎。Runtime Compiler 根据场景所声明的状态表示和 coupling 选择 backend：

| 场景域 | 通用 capability | 可用 backend |
|---|---|---|
| 刚体状态 | `rigid_body_dynamics` | UE；fallback 仅作非真值预览 |
| 粒子状态 | `fluid_particle_dynamics` | Genesis SPH；fallback 仅作契约调试 |
| 可变形网格状态 | `deformable_body_dynamics` | Genesis FEM / Taichi cloth；fallback 仅作契约调试 |

Backend 选择只读取对象状态域、显式 solver capability 和 backend constraint，不读取 case id、目录、prompt 中的现象词或旧 `capability_id`。

## 主路径

```text
user text/images
  -> CaseSpec V2
  -> Provider -> Catalog -> Asset Resolve
  -> Runtime Compiler
  -> scene-domain Backend Planner
  -> backend adapter / numerical solver
  -> canonical state, event and sensor artifacts
  -> generic assertion verifier
  -> readiness / review / dataset package
```

允许扩展的是通用 primitive、state representation、coupling、backend adapter、measurement reduction 和 assertion operator。禁止增加以物理现象命名的求解函数、runtime mode、fallback trajectory、verifier 或隐式质量门。

## 当前代码边界

- `harness/core/physics_contract.py`：仅推断 `rigid_body`、`particle`、`deformable` 三种状态域。
- `harness/planning/backend_planner.py`：按状态域和显式 backend constraints 选择执行器。
- `harness/planning/verification_compiler.py`：只编译通用 state/event assertion。
- `harness/verification/trajectory_assertion_verifier.py`：执行 `trajectory_integrity`、`state_value`、`state_delta`、`event_exists`、`event_count`、`event_sequence`、`artifact_complete`。
- `harness/verification/physics_verifier.py`：按状态域调用一个通用 verifier，不按现象标签分派。
- `harness/runtime/fallback_backend.py`：只输出声明初态的非参考运动学预览，不制造碰撞、反弹、破碎或其他物理事件。
- `harness/runtime/mujoco_rigid.py`：只按 actor binding 和通用刚体字段构建系统。
- `scripts/harness_local_ue_runner.py`：所有刚体场景统一编译为 `llm_object_graph`。
- `scripts/native_ue_scene.py`：入口拒绝任何其他 `case_type`；当前运行主路径只消费对象图。

历史现象 capability JSON 仅作为读取旧 case/artifact 的 `compatibility_alias`，统一投影为 `rigid_body_dynamics` 或 `deformable_body_dynamics`。它们不进入 active profile，也不能选择 runtime/verifier。

## Case 与导航

`cases/` 仍保留按历史样例命名的目录，便于定位回归输入；目录名不属于执行契约。`scripts/harness_case_tree.py` 只生成 Markdown 导航，任何 planner、backend、solver 和 verifier 都不得读取 TREE 或目录路径进行分流。

批量生成不再接受 `--suite`。它只复制一个已经声明完整的 CaseSpec：

```bash
python scripts/harness_generate_cases.py \
  --case cases/falling/falling_block_on_floor.json \
  --count 4 --seed 7 --out /tmp/generated_cases
```

这里的源路径只是一个示例文件；生成器不会因为目录名而改变参数或物理逻辑。

## 资产与 Meshy

用户图片、Meshy 结果、本地 FBX/OBJ、UE builtin、Map 与 analytic recipe 都必须经过相同的 Provider/Catalog/Asset Resolve 生命周期。Provider 结果不能直接注入 runtime。Meshy 只负责资产生成，不负责场景物理、杯腔碰撞正确性或视频验证；碰撞几何必须由 Catalog/binding 明确记录并通过资格门。

## 真值与失败语义

- fallback/fake runner 只能验证协议，不能标记为真实物理或真实 UE 渲染。
- UE 刚体 readiness 需要原生 Chaos/C++ capture provenance；仅有视频和手写 trajectory 不足以得到 `physics_ready`。
- Genesis/Taichi cache 是对应状态域的 solver truth；UE replay 只是渲染 adapter。
- 缺少 backend primitive、coupling、资产碰撞几何、importer 或 solver provenance 时 fail closed。
- 视频已生成但通用断言失败时，运行状态应为 `failed_verification`；这不是网络或 importer 错误。

## 回归门

核心架构回归位于 `tests/test_no_process_dispatch.py`。它验证：更换旧现象标签不会改变 backend、verifier、fallback trajectory 或 UE native mode。

完整测试命令：

```bash
conda run -n base python -m unittest discover -s tests -p 'test_*.py'
```

外部 UE、Genesis 或 Taichi 条件不满足的测试可以明确 skip；不可用 fallback 冒充通过。
