# ICLR Core80 本地资产审计与补充批次

状态：2026-09-04 完成本地只读审计；尚未为本批次提交新的 Meshy 付费任务，也未启动 UE 资格验证。

## 结论

- 当前 Catalog 有 379 个已物化资产和 390 条 UE runtime-ready binding。
- HomeInterior 提供 102 个网格，warehouse_low 提供 57 个网格；它们足以覆盖大部分桌椅、书杯、箱桶、托盘、梁、梯子、绳盘和工业道具。
- 本地已有 31 个已登记 Poly Haven 实物资产、保龄球/球瓶、弹簧，以及 Oak Wood Planks、Hessian 230、scratched phosphate 材质。
- 30 个 Poly Haven 室内 HDRI 源文件均在盘上；HomeInterior、Empty Workshop、Lebombo、Wrestling Gym 已经是 runtime-ready Map，其余按 Case 需要逐个物化和 smoke，不批量预处理。
- Core80 不需要再下载大型环境包。需要补充的是约 16 组可跨 Case 复用的辨识度主角资产；多数可先用 Meshy，开放结构与破碎源还必须单独验证碰撞/Geometry Collection。

## 已有且可优先复用

| 资产组 | Catalog/本地代表 | 主要覆盖 Case | 使用边界 |
|---|---|---|---|
| 桌面与家具 | HomeInterior `Dinner table/Table 00–02/Kitchen island table`；Poly Haven `Wooden Table 01/Modern Coffee Table 01` | R005/R010/R011/R025/R043/R047/R050/R066、C005/C006/C012 | 真实网格作视觉；接触面需要时用显式 box/cylinder 真值 |
| 书、杯、盘、花瓶、地毯 | HomeInterior `Book 01/Books/Cup 1/Plate 01–02/Vase 01–03/Carpet/Puff` | R003/R005/R010/R025/R030/R043/R047/R050/R052/R066、C004–C006 | 已有 UE binding；动态碰撞仍按具体对象检查 |
| 箱、桶、罐、托盘、梁 | warehouse_low 多个 cardboard box/plastic crate/metal barrel/wooden pallet/iron beam/trash can | R004/R011/R019/R020/R031/R033/R038/R044/R045/R048/R051/R054/R067/R072–R074/R078/R079、C007/C011/C012 | 足够形成多样组合；复合打包网格不作为独立动态物体 |
| 梯子、绳盘、链条、货架 | HomeInterior Ladder；warehouse_low metal/folding ladder、rope spool、hanging chain、pallet racking | R033/R052/R067/R069、仓库布景 | 链/绳网格主要作视觉，物理仍用通用约束 |
| 球类 | 本地带纹理球、Poly Haven baseball/football、程序化 sphere | 多数碰撞/滚动/摆 Case | 解析 sphere 为物理真值，外壳负责真实外观 |
| 保龄球组 | 本地 qualified bowling ball 与 pin | R015 | 已通过 Pilot，可直接复用 |
| 弹簧 | `local_input.rusty_spring...` | R060/R061/R063/R085 | 弹簧网格仅视觉，约束是物理真值 |
| 容器 | Poly Haven open crate/planter/plastic crate、HomeInterior bucket/wineglass | R009/R026/R053、流体和布景 | “开放外观”不等于开放碰撞；篮筐/漏斗仍需显式复合碰撞 |
| 材质 | Oak Wood Planks、Hessian 230、scratched phosphate | 木材、绳布、金属装置 | 可跨 Case 复用 |

## 需要补充的 Meshy 主角批次

以下按“复用价值优先”组织，不是一 Case 一资产。建议先生成 A 批，跑正式 Case 时再按实际缺口生成 B 批。

### A 批：优先生成与验证

| 组 | 资产 | 覆盖 Case | 资格重点 |
|---:|---|---|---|
| A01 | 通用低矮四轮实验小车，视觉上可加配重 | R023/R060/R061 | 轮廓与质心合理；物理可继续用简化车体/轮轨 |
| A02 | 马蹄磁铁 + 条形磁铁 + 可复用钢球/钢片外观组 | R081–R084、C012 | 磁力仍由 signed radial field 提供，不从网格推断 |
| A03 | 开放篮筐 | R009 | Mesh 必须看起来贯通；物理用环形/立柱复合碰撞，不能用整体 box |
| A04 | 开放漏斗 | R026 | 中心通道必须真实开放；物理用锥壁/分段复合碰撞 |
| A05 | 跷跷板装置 | R041/R053 | 板与支架分件，便于通用 hinge |
| A06 | 台球桌 + 球杆 | R016/R024 | 库边用解析碰撞；球继续用 sphere |
| A07 | 牛顿摆支架 | R018 | 支架只作视觉；球和悬挂约束独立 |
| A08 | 轮胎 | R054 | 中空视觉；动态物理可用匹配尺寸 cylinder/convex shell |
| A09 | 铰链家用门 + 工业栅门 | R057/R058 | 门扇与框架分件，转轴位置清楚 |
| A10 | 转盘底座与圆盘 | R066 | 圆盘独立，便于角驱动和表面摩擦 |
| A11 | 带中心孔的桌面 + 光滑孔眼 | R086 | 桌面孔视觉贯通；绳仍由两段路径约束驱动 |
| A12 | 十二直径星形导轨底座及统一滑块外壳 | R085 | 底座与滑块分件；正式 Case 必须真实验证 12 滑块组合 |

### B 批：随正式 Case 需要生成

| 组 | 资产 | 覆盖 Case | 资格重点 |
|---:|---|---|---|
| B01 | 破拆球支架/吊球外壳 | R006 | hinge 与砌块仍用通用物理 |
| B02 | 模块化跳台/导轨装饰 | R012 | 实际坡面和缺口用解析碰撞 |
| B03 | 真实硬币与陀螺 | R035/R036 | 外形轴线、pivot 和尺寸匹配 |
| B04 | 双摆支架 | R056 | 连杆/球与两级 hinge 独立 |
| B05 | 弹簧铰链翻板外壳 | R070 | 翻板独立、轴点清晰 |
| B06 | 旗杆/桅杆/横杆、滑动隔断和托盘外壳 | C001–C003/C008/C010 | 可优先程序化加真实材质；画面不足时再 Meshy |

## 破碎资产单独处理

- R076/R077：准备一份可复用玻璃板源并制作 Geometry Collection；两 Case 共用同一碎片结构，只改变冲击能量与阈值关系。
- R078：木板可用简单板形源配 Oak 材质制作 Geometry Collection，不必为形状单独生成复杂 Meshy 模型。
- R079：优先从已有 Wooden Crate/warehouse crate 选择结构清楚的源；若 Geometry Collection 失败，再生成一只封闭、分板明确的 Meshy 木箱。
- 这些源资产的普通 StaticMesh 可用不代表破碎已合格，必须以实际 Geometry Collection smoke 为准。

## HDRI 与构图

- 30 个本地 HDRI 已覆盖清单中的 Ballroom、Bank Vault、Billiard Hall、Country Club、Machine Shop、Hangar、Warehouse、Gym 等环境。
- 每个背景只在首次使用时物化；HDRIBackdrop 放大，主体放在中央空旷区域，采用较长焦/较小 FOV，避开边缘弯曲与接缝。
- 背景不参与物理碰撞；显式地面/桌面仍由 CaseSpec 提供。

## 当前不需要用户下载

先按 A 批尝试 Meshy；若开放篮筐/漏斗无法生成干净开放拓扑，或门、破碎源无法分件，再向用户提出具体 Fab/人工下载需求。条件图的准备与实物资产获取分开，不阻塞 text-only Case。
