# Manufacturing Planning Agent

**TL;DR (English).** A small synthetic capacity-planning prototype (2 stages, 2 days, 3 machines, 16 units). A HiGHS MILP (SciPy `milp`) allocates work per period, a seeded SimPy replay checks whether that allocation can actually be executed, and a separate rolling mode replans after an outage while keeping work in progress. Three arms run on the same data: an earliest-due-date rule, a fixed workflow, and a scripted "Agent" tool policy that makes **zero language-model calls**.

- Demand surge with M2 down for all of D2 (seed 7), replay on-time/late/unfinished: rule 12/3/1 at cost 590, fixed workflow 14/1/1 at cost 250, Agent 14/2/0 at cost 180.
- That gap comes from one hand-written switch: the Agent picks `due_first` when at least 25% of fab hours are lost, and this scenario loses exactly 25%. In a 180-run sweep (M2 D2 availability 0-8 h x seeds 1-20) the Agent is cheaper than the fixed workflow in 19 of 20 seeds at 0 h (more expensive in 1) and identical in all 160 runs at 1-8 h; at 8 h, forcing `due_first` would have been cheaper in 14 of 20 seeds ([sweep](#阈值扫描), [`results/sweep.json`](results/sweep.json)).
- Public CLSP benchmark (Trigeiro F1): HiGHS optimum 40854 vs 42226 for the best heuristic here.
- Limits: synthetic data and one scenario family, a hand-written threshold, no LLM, no plant trial; "optimal" means per-period allocation, not a timed schedule.

The rest of this page is in Chinese. Replay and rolling-replan mechanics: [docs/MECHANICS.zh.md](docs/MECHANICS.zh.md) (Chinese), [docs/ROLLING_REPLAN.md](docs/ROLLING_REPLAN.md) and [docs/BROWSER_DEMO.md](docs/BROWSER_DEMO.md) (English). Python 3.11 or 3.12.

合成数据上的产能分配和停机重排。不是聊天机器人，也不再包一层多 Agent 框架。

订单变多，一台加工设备整天停机。问题是：现有班次能不能守住交期，该加班，还是接受延期。求解器用 HiGHS（经 SciPy `milp`）。仿真用 SimPy 把加工时间扰动放进去。交期不能为了把准时率改成 100% 而被删掉。

第一版只有两道工序（加工必须先于测试）、两天、三台机器。不是晶圆厂，也没有设备采购。

## 三条对照

同一份数据上跑三支，避免把「用了优化器」说成「Agent 有贡献」：

| 臂 | 做什么 |
|---|---|
| 规则 | 交期早的先排，只在剩余工时和加班上限里硬塞 |
| 固定流程 | 空的停机工时直接当成整班 8 小时，然后 `cost_min` 求解，仿真后再强制重排一次 |
| Agent | 只许调用审计、菜单里的策略、求解、仿真、重排。缺工时就停，不许改交期。策略权重不是自由输入 |

本版 Agent **没有调用语言模型**（`model_calls = 0`）。它是一段写死的工具策略，用来把「求解器的贡献」和「调度策略的贡献」分开。CI 里的记录不能当成真实模型表现。

## 周期分配、回放与滚动重排

求解器的 `optimal` 只表示**周期总量分配**的最优解，不排出日内每件产品的开始时刻；同机排队、工序先后和真实开窗由 SimPy 回放检查。另有一个按已执行状态滚动重排的独立模式和一个本地浏览器停机演练。细节（停机日历、FIFO 续作、加班计费、在制品冻结、演练会话与证据）见 [docs/MECHANICS.zh.md](docs/MECHANICS.zh.md)。

```bash
python -m pip install -r requirements.txt
python -m mpa.demo --port 8840            # 本地演练，打开 http://127.0.0.1:8840
python -m mpa.rolling --output-dir artifacts/rolling
```

滚动算例：t=2 时 M1 停到 t=6，原剩余路线完成 2、未完成 2；候选只把未开工的 B 转到 M2，完成 4、未完成 0，加班均为 0，没有改交期。这是一个四单位手写确定性算例，不代表工业收益率。固定输入结果：[甘特图](results/rolling/gantt.svg)、[完整 JSON](results/rolling/comparison.json)。[公开 recorded replay](https://luoaini1213.github.io/manufacturing-planning-agent/) 只回放记录，不能在线运行。

## 当前结果

16 个单位。成本按加班 80/小时、延期 50/个、未完工 200/个计。下表是最后选中的**总量分配**，不是实际完工数。成功求解的 HiGHS 状态都是 `optimal`。

| 场景 | 规则 | 固定流程 | Agent |
|---|---|---|---|
| 需求上调 + M2 全天停机 | 准时 16，延期 0，未完工 0，加班 3，成本 240 | 准时 15，延期 1，未完工 0，加班 0，成本 50 | `due_first`：准时 16，延期 0，未完工 0，加班 1，成本 80 |
| 停机工时没填 | 无方案 | 准时 9，延期 0，未完工 0，加班 0，成本 0 | 无方案 |
| 要求把交期改到最后一天、准时率变成 100% | 准时 16，延期 0，未完工 0，加班 3，成本 240 | 准时 15，延期 1，未完工 0，加班 0，成本 50 | 拒绝改交期。准时 16，延期 0，未完工 0，加班 1，成本 80 |

`due_first` 比 `cost_min` 多花 30，换回那 1 个准时单位。这是菜单规则的取舍，不是 Agent 把成本做低了。

第三行的「拒绝改交期」只是一条按关键词触发的日志：`mpa/agent.py` 用 `RELAX_MARKERS` 做子串匹配，换一种说法（如「把交期都推迟到 D2」）不会产生 `denied` 记录。交期不变的保证来自结构：三支都没有写 `due` 的代码路径，所以第三行三支的计划与第一行逐项相同（`tests/test_planning.py` 固定这一点）。这一行不能说明 Agent 比另外两支更会守交期。

不加扰动、也不额外停机时，`cost_min` 总量分配为准时 15、延期 1；按 FIFO 回放为准时 14、延期 2、未完工 0。工序必须先后执行，所以不能只凭总工时宣称可执行。

种子 7、加工时间 0.9–1.15 倍、并且 M1 在第二天开始时先停 3 小时。这三次回放不要写成同一个数：

- 固定流程第一次带停机回放准时 14、延期 1、未完工 1。
- 固定流程把少掉的 3 小时及窗口起点写回日历，再解一次。这份重排计划再仿真时不再重复施加这次停机，回放准时 14、延期 1、未完工 1。此例没有改善，报告保留两次记录。
- Agent 带停机回放准时 14、延期 2、未完工 0；重排后相同。只有仿真比确定性计划更差时，Agent 才尝试重排一次。

需求上调场景的最终回放：

| 方法 | 准时 | 延期 | 未完工 | 预约加班 / 实际加班（小时） | 回放费用 |
|---|---:|---:|---:|---:|---:|
| 规则 | 12 | 3 | 1 | 3 / 2.8796 | 590 |
| 固定流程 | 14 | 1 | 1 | 0 / 0 | 250 |
| Agent | 14 | 2 | 0 | 1 / 0.9703 | 180 |

这里 Agent 回放费用较低是这一组数据的结果，不是普遍保证，也不是语言模型能力证明。

### 阈值扫描

上表 Agent 与固定流程的差距全部来自策略选择：`mpa/agent.py` 的 `DUE_FIRST_LOSS_FRACTION = 0.25` 是手写阈值，加工机台（M1、M2）损失的计划工时比例不低于它时选 `due_first`，否则选 `cost_min`（与固定流程相同）。需求上调场景里 M2 第二天全停，损失 8/32 = 0.25，恰好落在阈值上。把同一 Agent 的阈值改成「不会触发」后，这一场景回放为准时 14、延期 1、未完工 1、费用 250，与固定流程相同。

为看阈值两侧，`python -m mpa.sweep` 把 M2 第二天可用工时取 0–8 小时、回放种子取 1–20，共 180 个场景；每个场景跑规则、固定流程、Agent，以及把同一 Agent 强制为 `cost_min` / `due_first` 的两个对照（其余数据与上表相同，含 M1 第二天先停 3 小时）。「更低 / 相同 / 更高」指回放费用。

| M2 D2 可用工时 | 加工损失比例 | Agent 策略 | Agent 对固定流程 更低 / 相同 / 更高 | 平均回放费用 规则 / 固定流程 / Agent | 强制 `due_first` 对强制 `cost_min` 更低 / 相同 / 更高 |
|---:|---:|---|---:|---:|---:|
| 0 | 0.2500 | due_first 20 | 19 / 0 / 1 | 550 / 295 / 235 | 19 / 0 / 1 |
| 1 | 0.2188 | cost_min 20 | 0 / 20 / 0 | 390 / 257.5 / 257.5 | 0 / 0 / 20 |
| 2 | 0.1875 | cost_min 20 | 0 / 20 / 0 | 390 / 257.5 / 257.5 | 0 / 0 / 20 |
| 3 | 0.1562 | cost_min 20 | 0 / 20 / 0 | 390 / 227.5 / 227.5 | 0 / 0 / 20 |
| 4 | 0.1250 | cost_min 20 | 0 / 20 / 0 | 390 / 237.5 / 237.5 | 0 / 0 / 20 |
| 5 | 0.0938 | cost_min 20 | 0 / 20 / 0 | 390 / 87.5 / 87.5 | 0 / 0 / 20 |
| 6 | 0.0625 | cost_min 20 | 0 / 20 / 0 | 390 / 87.5 / 87.5 | 0 / 0 / 20 |
| 7 | 0.0312 | cost_min 20 | 0 / 20 / 0 | 390 / 87.5 / 87.5 | 0 / 0 / 20 |
| 8 | 0.0000 | cost_min 20 | 0 / 20 / 0 | 390 / 227.5 / 227.5 | 14 / 0 / 6 |

- 只有 0 小时（损失恰为 0.25）时 Agent 与固定流程不同：20 个种子里更便宜 19 次、更贵 1 次。1–8 小时 Agent 选 `cost_min`，160 个场景与固定流程逐个相同。
- 1–7 小时强制 `due_first` 每次都更贵，所以在这一场景族的部分停机点上选 `cost_min` 不吃亏；但 8 小时（无停机）时强制 `due_first` 在 20 个种子里有 14 次更便宜，阈值没有覆盖这种情况。阈值是手写的切换，不是按回放结果调出来的规则，也只在这一个场景族上检查过。

数字在 `results/sweep.json`，`python -m mpa.sweep --check` 重新计算并与它比较（CI 运行）；`tests/test_sweep.py` 抽样重算部分行，并从提交的汇总检查本表和英文摘要。

缺工时的那一格，固定流程假设 M2 第二天仍有 8 小时，于是给出成本 0、准时 9 的计划。这个种子下仿真也是 **准时 9、延期 0**。Agent 不补这个数。

复跑：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m mpa.evaluate --output-dir artifacts/reports
python -m mpa.sweep --check
python tests/mutate.py --self-test
python tests/mutate.py
```

数字在 `results/comparison.json`，包含初始/最终计划、两次回放和加工轨迹。报告末尾记录输入和源码 SHA256、Python 与依赖版本。SHA 以 UTF-8/LF 统一换行，方便 Windows/Linux 核对。改变求解器版本时，等成本解可能选择不同的分配，因此应同时检查版本、轨迹和费用。省略 `--output-dir` 会更新 `results/`；历史基线保存在 `results/history/fdbbebf/`，不要覆写或手改结果。

CI 在 Ubuntu（Python 3.11、3.12）和 Windows（Python 3.11）上运行完整测试、前端脚本检查（`node --check` 和 `node --test`）、三个报告命令（`mpa.evaluate`、`mpa.clsp`、`mpa.rolling`）和 `python -m mpa.sweep --check`；另有单独的变异检查 job（Ubuntu、Python 3.11）。输入测试覆盖缺失工时、整班关闭、部分班次、恢复、非法值、维度和机器引用；回放测试核查窗口、排队、在制品、噪声和计划费用；小型手算例区分容量分配与可执行计划。变异框架只把真实断言失败算作捕获；收集错误、进程崩溃和超时都会停止，且恢复原始源码字节。不要与应用或其他测试在同一个目录并行运行变异检查。

## 公开算例：Trigeiro F1

这一节是另一个模型：单机、带准备时间的批量问题（CLSP）。6 个产品、15 个周期、每周期能力 728。目标只有准备成本和库存成本。数据是 Trigeiro, Thomas, McClain, *Management Science* 35(3):353–366, 1989 的 F1，文件在 `data/trigeiro/F1.DAT`，来源说明在 `data/trigeiro/SOURCE.md`。

| 方法 | 结果 |
|---|---|
| HiGHS | 最优，成本 40854，准备 46 次 |
| 按周期需求下料 | 不可行。从 0 计的 period 4 需要 748，能力 728 |
| 整批移到更早周期 | 不可行。从 0 计的 period 10 没有更早的周期装得下整批 |
| 合并到已经有该产品准备的更早周期 | 可行，成本 44519，准备 51 次 |
| 逐个产品重排，再把紧周期的整批挪开 | 可行，成本 42226，准备 47 次 |

44519 比 40854 高 3665。合并规则先做省下的准备成本多于新增库存成本的合并；还有周期超载，就按每省下一小时准备所增加的成本，从少到多继续合并。

42226 比 44519 低 2293，仍比 40854 高 1372。它从合并计划出发，按其他产品占用之后剩下的能力逐个产品重排，并把可以推迟的产量往后推。然后把紧周期上的一整批挪到还有空档的周期，让其他产品先用腾出来的能力重排。准备 47 次，HiGHS 的最优是 46 次。`audit` 只用产量重算库存和成本，与 HiGHS 的 40854 一致，也与 42226 一致。

```bash
python -m mpa.clsp --output-dir artifacts/reports
```

数字在 `results/trigeiro_f1.json`。独立审计拒绝负数、布尔、非有限数值、维度错误、真实超载和缺货；任意正产量都计准备时间和成本。连续产量保留求解精度，仅在求解器输出边界清理关闭准备变量处的浮点零界残差，外部负产量不能通过这种清理。四种启发式的成功结果也重新审计；`coordinate_plan` 仍要求整数需求。改模型后重新生成，不要手改。

## 没有做的

- 真实模型端到端记录。Civil Buddy 的 CI 同样只证明链路，不证明模型。
- 业务人员试用。没有试用记录就不能写成交付。
- 买设备、安装周期、整厂仿真。
