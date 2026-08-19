# Stage 7 归因稳定性收口与最终冻结验收报告

> 范围：仅修复已发现缺陷并完成首版端到端冻结验收，不新增产品功能、不扩张架构边界。
> 验收方式：真实后端 API/SSE（`POST /api/query`，StreamingResponse），命中 `http://127.0.0.1:8000`。
> 原始事件存于 `/tmp/accept/*.json`；机器可读汇总见 `data_agent/acceptance_results.json`（已重建，替代早期浏览器脚手架）。

## 一、修复根因（Repair root cause）

固定归因场景一「为什么2025年2月销售额较1月明显下降？」偶发「无有效数据 / 无 driver，重试正常」。复现脚本 `repro_contract_drift.py` 在**未经改动的 main**（仅 mock `app.services.query_service.graph`）上确定性复现两类失败，证明根因落在允许修复边界内：

- **类 #2 列漂移**：SQL 输出缺少契约列 `period_key`（如只返回 `sales_amount` 聚合列）→ Normalizer 严格校验失败 → `Observation.failed` → 连续 2 次失败 → 触发「连续 2 次空/失败强制停止」→ 无 driver。
- **类 #3 期间值漂移**：`period_key` 取 `2025-01`/`2025-02` 而非契约允许的 `{comparison, current}` → 同样 Normalizer 失败 → 强制停止。

**根因 = `result_contract` 原本只是 `generate_sql` Prompt 的软约束，与 SQL 实际输出之间存在漂移，下游 Normalizer 严格执行契约而失败。** 该根因属于「硬契约落在 QueryService / DataAgent Graph 内」的允许修复边界，无需升级到架构 Agent。

## 二、架构决策落点（§二 合规）

将 `result_contract` 从 Prompt 软约束提升为 `QueryService.execute()` 内的**硬执行契约**：

```
Attribution Action → QueryService.execute()（一次调用）
  → DataAgent Graph → SQL
  → result_contract 硬校验（validate_contract_result）
  → 至多一次受控内部 SQL 修复
  → QueryExecutionResult → Normalizer
```

约束全部满足：

- 内部修复**不增加** `query_action_count`、**不产生**第二个 Action / 第二个 `action_start`、**不改变** 6 次查询上限。
- `result_contract=None`（普通问数）完全保持原行为，不进入修复。
- 修复可见上下文：原 `query`、原 `SQL`、`result_contract`、显式契约失败原因、`table_infos` / `metric_infos` / `date_info` / `db_info`。
- 至多修复一次，重执行并重校验；仍违反则正常返回 `failed`，**不无限重试**。

## 三、修改的文件

| 文件 | 性质 | 说明 |
|---|---|---|
| `app/services/query_service.py` | 核心修改 | `execute()` 新增 `result_contract` + `contract_repair` 参数；Graph 产出后硬契约校验；新增 `_do_contract_repair()`（复用 `dw_mysql_repo.validate_sql` / `execute_query`）；新增 `_default_contract_repair()`（LLM 修复）。 |
| `app/attribution/normalizer.py` | 仅新增守卫函数 | 新增模块级 `validate_contract_result()`，与 Normalizer 严格语义一致，作为 QueryService 边界硬契约检查（不调 LLM、不猜列）。`Normalizer` 类未改动。 |
| `app/prompts/repair_sql_contract.prompt` | 新增 | LLM 修复 Prompt，含上下文与「规则 9」镜像（契约列别名约束）。 |
| `tests/attribution/test_contract_repair.py` | 新增 | 14 个测试，覆盖 §四 #1–#7 及 `validate_contract_result` 守卫。 |
| `repro_contract_drift.py` | 新增 | 复现证据脚本（不进生产路径）。 |
| `data_agent/acceptance_results.json` | 重建 | 删除早期浏览器脚手架（NETWORK_ERROR），以真实 API/SSE 结果重新生成。 |

## 四、自动化测试结果（§四）

| 项 | 命令 | 结果 |
|---|---|---|
| 全量 pytest | `uv run pytest -q` | **338 passed**（4.25s） |
| 字节码编译 | `uv run python -m compileall app` | **OK** |
| 前端构建 | `npm run build`（vite） | **OK**（dist/index.html + css 13.18kB + js 84.07kB，419ms） |

覆盖说明：

- 新增 14 个契约修复测试（§四 #1 缺 alias 触发一次修复 / #2 period 值违约触发修复 / #3 修复后合规→success / #4 修复后仍错→failed 不无限重试 / #5 `result_contract=None` 不进修复 / #6 不增加 `query_action_count` / #7 不产生额外 Action/`action_start`）。
- 既有 Normalizer「不猜列」测试继续通过（见 `test_normalizer.py`）。
- 既有 Attribution / QueryService 全量测试继续通过（整套 338）。

## 五、真实最终验收（§五）

### 5.1 五个普通问数（mode=query）

全部 `done.status=completed`、`query_count=1`、无错误（`n_error=0`）。其中 `normal_1`（统计2025年各月销售额）返回核心月销售额，已逐项核对：

- 202501 = **109030.5**
- 202502 = **80009.0**
- 202503 = **90120.0**

与验收基线完全一致。

### 5.2 场景一「为什么2025年2月销售额较1月明显下降？」× 3 连续

| run | 状态 | qcount | Jan | Feb | delta | change_rate | drivers |
|---|---|---|---|---|---|---|---|
| run1 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3（region 华东 / category 手机数码 / product iPhone 15 Pro） |
| run2 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3（region 华东 / category 手机数码 / customer 白银） |
| run3 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3（region 华东 / category 手机数码 / customer 白银） |

三次连续运行数值完全稳定；每次 ≥2 个有效拆解维度、≥1 条 driver Evidence，结论可追溯到 SQL 与确定性计算。

### 5.3 场景二「为什么2025年3月销售数量大幅增长，但销售额增长有限？」× 3 连续

| run | 状态 | qcount | Feb qty | Mar qty | qty change | Feb amount | Mar amount | amount change | avg unit price | drivers |
|---|---|---|---|---|---|---|---|---|---|---|
| run1 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |
| run2 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |
| run3 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |

三次连续运行数值完全稳定；平均单价由后端 Calculator 确定性计算（80009/151=529.86、90120/322=279.88），出现在 calculation / evidence 链中。每次 ≥2 个有效拆解维度、≥1 条 driver Evidence，结论可追溯到 SQL 与确定性计算。

### 5.4 事件 / 状态与泄露扫描

- **事件类型齐全**：`route` / `stage` / `action_start` / `query_result` / `calculation` / `report` / `error` / `done` 均在事件流中出现（`error` 在成功路径为 0 次）。
- **状态**：11 次 run 的 `done.status` 全部为 `completed`（无 `422` / `failed` / `partial` 异常）。
- **泄露扫描**：对全部 11 个原始事件文件扫描 `password` / `api_key` / `sk-` / `traceback` / `数据库密码` / `Prompt` → **0 命中**。未泄露 Prompt、隐藏推理、traceback、API Key、数据库密码。

## 六、query_action_count 合规

- 6 次归因 run 的 `query_action_count` 均为 **5**（≤ 6 上限）。`n_action_start=6`（5 个查询 Action + 1 个 finish Action）。
- 内部修复不增加 `query_action_count`：`test_internal_repair_does_not_increase_query_action_count_or_action_start` 以 3 个查询 Action 各触发 1 次修复验证，`query_action_count` 仍为 3，且 `action_start` 仅 4 次（3 查询 + 1 finish）。

## 七、内部契约修复是否触发及次数

- 实时 6 次归因 run 中，SQL 首次生成即满足契约（结果稳定且契约合规），**修复后路未被运行期触发**（无 repair 警告日志、SSE 无修复标记）。
- 修复路径正确性由 14 个单元测试覆盖：缺 alias 触发一次修复 / period 值违约触发修复 / 修复后合规→success / 修复后仍错→failed 不无限重试 / `result_contract=None` 不进修复 / 不增加 `query_action_count` / 不产生额外 Action/`action_start`。

## 八、浏览器控制台异常

- 本次验收为 API/SSE 层，不依赖浏览器，无浏览器控制台异常可言。
- 旧 `acceptance_results.json` 为早期浏览器脚手架（`[NETWORK_ERROR]`，后端未起时截图），已删除，并以真实 API/SSE 验收结果重新生成，避免与最终结论矛盾。

## 九、明确未做（§三 禁止项确认）

- 未放宽 Normalizer 严格契约校验；未在 Normalizer 猜列；
- 未修改「连续 2 次空/失败强制停止」规则；未修改 6 次查询上限；
- 未硬编码两个冻结场景的专用 SQL；未对用户问句字符串写 `if/else`；
- 未强制 Planner 固定维度路由；未重写既有 DataAgent Graph；
- 未加 Provider 层；未加基础设施；未改前端展示；未处理无关技术债。

## 十、停止条件确认

- 已稳定复现根因（`result_contract` / SQL 漂移，属允许边界）→ 完成修复 + 全量验收，符合「修复后全量验收即停止」的停止条件。
- 两个冻结场景均能 **3 次连续稳定通过**；5 个普通问数全过；自动化测试 338 passed、编译 OK、前端构建 OK；无敏感信息泄露。

**结论：Stage 7 归因稳定性收口与首版冻结验收通过。**
