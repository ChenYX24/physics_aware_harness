# ICLR Core80 执行加速方案

状态：设计已审计，尚未实现调度器，也未做并行 UE 实验。

## 结论

正式 Core80 当前不应直接使用已有 `harness_run_case_batch.py --parallel N`：它是旧式批处理入口，线程共享进程级环境变量，并让多个 UnrealEditor-Cmd 使用同一 Project、Saved、Intermediate、Catalog 和资产目录；它也不提供 durable Job Controller 的预算、恢复和审核状态。因此不能把“能并发启动”当作科研运行可用。

当前机器为 Apple M4、16 GB 内存，UE 项目约 6.7 GB。两个完整 UE 编辑器进程即使能运行，也很容易触发内存压力和交换，未必提高总吞吐。

## 推荐的第一阶段

采用三段流水线，但只有一个实际 UE worker：

1. `prepare`：并行编写/检查 CaseSpec、查询 Catalog、生成或下载资产、准备 assertion。
2. `compile`：并行做不启动 UE 的 schema、资产解析和 runtime compilation；需要 importer/Map materialization 的任务先进入独占 UE 队列。
3. `ue`：一个独占 worker 通过 durable Job Controller 依序执行 Smoke/Candidate，保持每个 Case 独立进程、独立输出目录和可恢复 checkpoint。

这能减少 UE 等待资产/人工准备的空转，并保持干净进程隔离；它不修改物理或 verifier，也不要求常驻 UE 进程。

## 调度器的最小范围

等首批 6–10 个正式 CaseSpec 准备好后，再写一个很小的队列入口：

- 只读取 `core80_execution_order.csv` 和各 Job 状态；
- 同时允许多个 `prepare/compile` 任务；
- 对所有最终会启动 UnrealEditor/UnrealEditor-Cmd 的动作使用单一文件锁和既有 `run_ue_case.sh`/Job Controller；
- 一个 Case 到人工审核边界就停止，不自动批准或修改 CaseSpec；
- 原子更新队列状态，但不把执行逻辑按 Case ID 分流；
- 不引入服务端、数据库或前端依赖，现有 JSON/HTML 状态页继续作为控制台。

## 可选的第二阶段：两个 UE worker

仅在单 worker 成为主要瓶颈后测试。两个 worker 必须各有完整隔离的 UE Project、Saved/Intermediate、可写 Catalog 快照和临时资产目录；只共享 UE Engine 与已经只读冻结的源资产。不能让两个进程写当前主 Project。

由于本机只有 16 GB，先用四个代表性 Smoke 做 A/B：单 worker 与双 worker 各跑相同 Case，比较总墙钟时间、失败率、输出轨迹/视频摘要和内存压力。只有双 worker 吞吐提升至少约 30% 且证据一致时才用于正式运行。当前不创建这些副本，也不进行该测试。

## 预计收益

- 最大收益来自提前完成资产与 CaseSpec，使 UE 连续工作，而不是等待 Agent/人工处理。
- 单进程逐 Case 启动成本仍存在，但换来最清晰的状态隔离和失败恢复。
- 若后续实测 UE 启动成本占比很高，再评估隔离双 worker；不先开发常驻 PIE 队列，因为跨 Case 清场与状态污染会增加科研风险。
