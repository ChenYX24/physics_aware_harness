# ICLR Core80 高风险资产准备单

状态：Core80 启动前高风险资产准备已收口。M01–M05 及 M04b 已完成生成、下载、UE 导入和 Catalog 登记；M03 开放漏斗、M04b 通孔几何、R076–R079 三类 Geometry Collection 源均通过真实 UE 资格验证。R086 正式展示改用下述 6 cm 真孔桌面组合，M04b 仅保留为通孔能力证据。正式案例仍须逐案通过 Smoke 和人工画面审核。

## 总结

| 资产组 | 当前状态 | 首选实现 | 必须通过的资格门 |
|---|---|---|---|
| R085 十二滑块装置 | Meshy 底座外壳已入库；只作装饰壳 | 程序化金属导轨/滑块 + 已有弹簧和 scratched-phosphate 材质 | 12 滑块真实 Smoke、相位/过中心/拟合圆断言、双视角构图 |
| R086 孔眼桌与两段绳 | 正式组合已锁定；M04b 仅作通孔能力证据 | 普通四腿木桌外观 + 四块 Oak 桌面围成 6 cm 真孔 + 独立金属孔眼；绳按两段端点实时显示 | 正式 Smoke 复核比例/构图；四块桌面同时承担视觉与碰撞；绳端点与球/孔眼同步 |
| R009 开放篮筐 | Meshy 主角已入库，视觉开口清楚 | 篮筐视觉网格 + 筐沿/侧壁/底部解析复合碰撞 | 球能从顶部进入且被容纳；不能以整体 box 封住开口 |
| R026 开放漏斗 | Meshy 主角已通过三球贯通资格 Smoke | 漏斗视觉网格 + 环形口沿/分段斜内壁解析碰撞 | 正式 Case 仍需十球通过中心通道；不能用封闭 convex/box 代理 |
| R076–R079 破碎源 | 三个可复用 Geometry Collection 已登记并通过联合真实 UE Smoke | 薄玻璃板 + Oak 木板 + Poly Haven Wooden Crate 02 开口木箱 | 阈值上下语义、原生撞击、碎片释放和木板/木箱内容物下落均已验证；正式案例只需逐案构图 |
| C012 磁吸盖布堆叠 | G2 已验证；堆叠资产大部分已有 | Meshy 马蹄磁铁；仓库钢件 + 本地木/陶瓷物 + Hessian 230 布 + 木桌 | 先跑 UE 磁吸抽底块，再烘焙刚体轨迹进 Taichi；布料不反作用刚体 |

## 已登记的 Meshy 资产

| 编号 | Catalog asset_id | 状态与使用边界 |
|---|---|---|
| M01 马蹄磁铁 | `generated.meshy.199ddac15b300dec1201afb1` | `runtime_bound / approved / pass_local_preview`；红色 U 形外壳可用，磁力仍由 signed point-field 给出 |
| M02 开放篮筐 | `generated.meshy.b94cfb83671ea2a62b3bf705` | 同上；开放内腔视觉合格，正式碰撞必须使用解析复合体 |
| M03 开放漏斗 | `generated.meshy.82b445d342cd59d8a53ad933` | `pass_functional_smoke`；金属漏斗与中央暗孔可读，三球均越过出口平面；正式碰撞继续使用开放解析复合体 |
| M04 中央孔实验桌 | `generated.meshy.2a3d1cf5be5d956858562f78` | `fail_visual_through_hole`；隐藏解析碰撞允许球通过，但顶视视觉网格是连续实心桌面，禁止用于正式 R086 孔眼展示 |
| M04b 中央大孔实验桌 | `generated.meshy.3b9a5226963df5f84481c73a` | `pass_visual_through_hole`；真实大孔、孔内壁和桌下空间在顶视/斜视均清楚，落球通过；自带贴图不合格，正式 R086 覆盖现有 Oak Wood Planks |
| M05 放射装置底座 | `generated.meshy.9f056f3be325ee968a224d3d` | 同上；生成结果存在中央实心轮毂，只作外观底座，十二条真实导轨由 CaseSpec 精确组合 |

五项 UE binding 均包含独立 base-color `Texture2D` 和 Material 依赖；完整 GLB、OBJ、PBR 文件保存在外接盘 Provider 缓存。登记摘要为外部 `reports/core80_meshy_high_risk_registration_001.json`。没有把 Meshy 视觉网格的默认单凸包冒充开放/带孔结构的物理碰撞。

## 已锁定的本地复用项

- 金属弹簧：`local_input.rusty_spring_wcliegz_high.25153b9e97a222f0`。
- 木箱候选：`external.polyhaven.wooden_crate_01.1ac5d053ce4c` 与 `external.polyhaven.wooden_crate_02.2733eca1a1c1`。
- 桌子候选：`external.polyhaven.woodentable_01.acee43a6468d`、`external.polyhaven.modern_coffee_table_01.31772c0aab6f`，以及 HomeInterior 的 Dinner table/Table 00–02；R086 仅复用材质和桌腿风格，不把无孔桌冒充有孔桌。
- C012 结构物：warehouse_low 的铁梁/金属桶/箱体、HomeInterior 的书/杯/花瓶与桌子可组成金属—木质—陶瓷三层堆叠；布使用已验证的 Hessian 230 材质。
- 破碎材质：玻璃使用透明玻璃材质；木板/木箱复用 Oak Wood Planks。普通 StaticMesh 合格不代表破碎合格。

## Meshy 生成 brief

生成时要求单个主角、中央摆放、无背景、真实尺度、现代写实 PBR、干净低中面数；需要运动或碰撞的组件必须分件导出，不接受把整个装置焊成一个网格。

### M01 马蹄磁铁组

`Photorealistic modern laboratory horseshoe magnet, red painted steel body with bare brushed-metal pole tips, clean PBR materials, isolated single object, symmetric U shape, open gap clearly visible, no stand, no text, game-ready topology.`

另生成一根独立条形磁铁。磁场仍由 `radial_force`/signed point-field 真值提供，不从网格形状推断。

### M02 开放篮筐

`Photorealistic open wire storage basket, rigid metal rim, sparse vertical and horizontal rods, fully open top and visible empty interior, no liner, no contents, isolated, game-ready PBR, clean topology.`

物理拆成顶部环形筐沿、若干侧壁杆和底部承接面；视觉网格不直接充当封闭碰撞体。

### M03 开放漏斗

`Photorealistic laboratory funnel, brushed stainless steel, wide open bowl and clearly open narrow outlet tube, hollow interior visible, isolated upright object, no liquid, no stand, game-ready PBR.`

物理用环形口沿、分段斜内壁和开放出口；需真实投球验证中心通道贯通。

### M04 R086 孔眼桌组件

`Photorealistic compact university physics laboratory table, warm wood top with one clean circular through-hole at the exact center, sturdy modern metal legs, isolated, PBR, no props, hole visibly passes through the tabletop.`

另生成独立 `smooth polished steel eyelet/grommet ring for a tabletop through-hole`。桌面碰撞用孔周四至八块 box，而不是整张桌面；两段绳视觉由 runtime 端点连接，不要求 Meshy 生成固定绳形。

### M05 R085 星形底座外壳

`Photorealistic precision physics demonstration apparatus, circular brushed-metal base with twelve straight full-diameter guide rails crossing the center at 15 degree angular spacing, no sliders, no springs, top view readable, isolated, game-ready PBR.`

滑块使用一只可复用的独立金属外壳；十二个实例、弹簧与约束仍由 CaseSpec 组合。若该网格影响尺寸可读性，正式 Case 直接使用程序化导轨配真实金属材质，不阻塞物理运行。

## 破碎源准备

- R076/R077 共用同一薄玻璃板及完全相同的碎片结构，只改变冲击能量相对阈值；先做成规则平板，避免为形状调用 Meshy。
- R078 使用规则木板、Oak 材质和两个真实罐体；验证破碎后罐子失去支撑，而非预先下落。
- R079 首选 Wooden Crate 01/02；若 Geometry Collection 无法得到可读碎片，再生成一只封闭、板缝清晰、无内部杂物的木箱。
- 每类只做一次严格资格 Smoke。失败时先判定 Geometry Collection/碰撞问题，再决定换源，不循环随机生成。

已登记的通用破碎源：

- 玻璃板：`generated.harness.core80_glass_panel_gc_v1`，36 个 Voronoi 碎片。
- 水平 Oak 木板：`generated.harness.core80_wood_board_gc_v1`，30 个 Voronoi 碎片；本地轴向已统一为水平，不需要运行时补旋转。
- 开口木箱：`generated.harness.core80_open_wood_crate_gc_v1`，源自 Wooden Crate 02，运行时释放 40 个碎片。

## 审核后执行顺序

1. M03/M04 联合资格 Smoke 已完成：M03 通过，原 M04 视觉孔洞失败；M04b 替换件随后通过真实 UE 顶视/斜视资格。
2. 玻璃板、木板和现有木箱的 Geometry Collection 源已完成联合真实 UE 资格验证；规则板未调用 Meshy。
3. R085/R086/C012 在各自正式 CaseSpec 中完成十二体、路径绳和 G2 组合 Smoke。
4. 只有开放拓扑或 Geometry Collection 连续两次不合格时，才提出精确 Fab/人工下载需求；当前无需用户补资产。

## M03/M04 联合资格 Smoke（2026-09-05）

- Job：`job_iclr_core80_asset_openings_qualification_001`，`attempt_002`；真实 UE Smoke、Verifier 与 Render Sync 均通过，时长 `5.041667 s`，仅使用一次 UE launch，按请求停在 Smoke 边界，未运行 Candidate/Reviewer。
- M03：顶视可辨识开放中央出口；中心球和两个偏置球均穿过出口平面，判定可作为 R026 视觉外壳。一个偏置球通过后滚出有限隐藏地面并继续下落，只影响该诊断镜头，不否定漏斗贯通。
- M04：轨迹断言通过仅证明隐藏的四块解析桌面碰撞留有中央孔；实际木桌视觉网格没有可读通孔，球看起来穿过实心木板，因此视觉资格失败。
- 本 Job 共 `12 MB`，未保留 PNG/EXR；产物保留在外接盘 `jobs/`，不计入正式 evaluated 结果。

## M04b 替换件资格（2026-09-05）

- 新 prompt 把孔径固定为桌面短边约 `25%`，明确要求实际缺失几何、可见垂直孔内壁/底边/桌下空间，并排除纹理圆、凹槽、盖板、塞子和装饰件；仅提交一个 preview/refine，消耗 `30 credits`。
- 资产：`generated.meshy.3b9a5226963df5f84481c73a`，source URI `meshy://text-to-3d/01a06d57-83bd-76ac-9841-0aec4e10a68d`，UE binding/material dependency 均 ready。
- Job：`job_iclr_core80_asset_m04b_qualification_001/attempt_001`。Smoke、Candidate、Verifier、Render Sync 与 Evidence 均通过，球的最小 `z=0.03 m`；未调用 Reviewer，未保留 EXR。该 seed Job 没有结构化 Smoke stop 字段，因此自然语言中的停止要求未形成边界并额外运行了非正式 Candidate；它只保留在 `jobs/`，不计入 evaluated。
- 几何资格通过：顶视和斜视均可直接看到大孔及内壁。材质资格不通过：Meshy 自带木纹在 UE 中呈三角碎片状；正式 R086 只覆盖为已登记 Oak Wood Planks，不修改网格或通用孔周解析碰撞。

## R086 正式视觉/物理组合锁定（2026-09-05）

- 正式桌面使用四块规则木板围成一个直径 `0.06 m` 的真实中央空洞；四块网格同时是可见 Oak Wood Planks 桌面和精确解析碰撞，不用贴图假孔，也不叠加隐藏实心桌面。
- 桌腿采用普通现代四腿桌比例，独立抛光金属孔眼覆盖孔缘；两段绳由 runtime 按桌上球—孔眼、孔眼—下垂球的实时端点更新。
- M04b 的大孔桌保留为通孔生成/导入资格证据，不进入正式镜头。这样避免大孔改变桌上球圆周运动的可读尺度，也避免其破碎木纹。
- 物理继续复用已审计的 `unilateral_path_length_spring`；正式 Smoke 只需调视觉比例、桌地协调和镜头，不再开发新约束。

## R076–R079 破碎源资格（2026-09-05）

- Durable Job：`job_iclr_core80_fracture_sources_qualification_001 / attempt_006`，按请求在 Smoke 后停止；Smoke Gate、Harness Verifier、Render Sync 与 Fracture Sensor State 均为 `pass`，时长 `5.541667 s`，Reviewer/Candidate 均未运行，未生成逐帧 EXR。
- 三个原生碰撞均成立：玻璃 `1` 次、木板 `1` 次、木箱 `4` 次。木箱在第 7 帧以 `16.908777 J` 释放 40 片，玻璃在第 11 帧以 `16.083622 J` 释放 36 片，木板在第 12 帧以 `15.883705 J` 释放 30 片。
- 内容物释放断言通过：两罐 Z 位移分别为 `-0.86515 m`、`-0.86521 m`，木箱中央球为 `-0.3624 m`。
- 既有成对阈值 Smoke 同时证明 R076/R077 的对照语义：低于 `10 J` 时不碎，高于 `10 J` 时碎。联合 Smoke 的目标是验证三种最终源及内容物释放，不替代正式案例自己的阈值和画面审核。
- 资产生成与登记收据位于外接盘 `reports/core80_fracture_asset_generation_001.json`、`reports/core80_wood_board_horizontal_generation_001.json` 和 `reports/core80_fracture_asset_registration_001.json`；资格视频保存在该 Job 的 `attempt_006` Smoke 目录。
