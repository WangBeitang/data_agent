# Stage 7 归因稳定性收口与最终冻结验收报告（最终收口版）

> 范围：Stage 7 最终收口，仅修复架构复核发现的 3 项问题（反向依赖 / 契约校验与
> Normalizer 语义漂移 / 默认 repair 路径覆盖），不新增产品功能、不扩张架构边界。
> 验收方式：真实后端 API/SSE（`POST /api/query`，StreamingResponse），命中 `http://127.0.0.1:8000`。
> 原始事件存于 `/tmp/accept_stage7/*.json`；机器可读汇总见仓库根目录 `acceptance_results.json`。

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

## 三、三项修复说明（Stage 7 最终收口）

### 3.1 消除 `QueryService → app.attribution` 反向依赖

`validate_contract_result` 下沉到中立低层纯函数模块 `app/services/result_contract.py`：

- 不调用 LLM、不访问数据库、不依赖 Attribution Graph；
- `QueryService` 与 `Normalizer` 共同复用（`query_service.py` 改为
  `from app.services.result_contract import validate_contract_result`）；
- `normalizer.py` 仅做轻量 re-export（兼容既有测试从 normalizer 导入该函数），
  Normalizer 类未放宽；
- 未新增 Provider 层或其它无意义抽象。

依赖方向现为：

```
app/services/query_service.py  ─┐
                               ├─→ app/services/result_contract.py（纯函数，无任何 app 依赖）
app/attribution/normalizer.py ─┘
```

### 3.2 硬契约校验与冻结 Normalizer 语义真正一致

修复前 `validate_contract_result` 与 Normalizer 存在两处漂移，本轮对齐：

| 校验点 | 修复前（漂移） | 修复后（与 Normalizer 一致） |
|---|---|---|
| period 合法条件 | `set(period_values) ∪ {comparison, current}`（union 放宽，period_values 含漂移值也能通过） | `period ∈ PERIOD_WHITELIST 且 ∈ set(period_values)`（交集，与 `_normalize_rows` 完全一致） |
| 指标值 NULL | `None` 判非法（触发误修复） | `None` 合法，对应 `MetricPeriodValue: float \| null`（与 `_to_float` 一致） |
| bool / 字符串 | 非法 | 仍非法（不猜测、不自动转换字符串数字） |

继续保留：缺少契约列失败；非法 dimension member 失败；同 member + period 重复失败；
合规空结果返回 empty（不触发 repair）；Normalizer 本身未放宽。
`PERIOD_WHITELIST` 在 `result_contract.py` 中定义，Normalizer 引用同一来源，从根上防止两套校验再次漂移。

### 3.3 覆盖真实默认 repair 路径

既有 `contract_repair=fake` 注入测试全部保留；新增
`test_default_repair_path_without_injected_contract_repair`：仅 mock LLM
（`app.agent.llm.llm` → RunnableLambda），**不 mock `_default_contract_repair()` 本身**，
验证完整生产路径：

```
QueryService.execute() → contract violation → _default_contract_repair()（真实）
→ repair prompt + LLM → validate_sql → execute_query → contract revalidate → success
```

并断言：原 query / 原 SQL / result_contract / violation reason 均传入；
LLM 输出 SQL 最终进入 repository validate/execute；全流程只 repair 一次
（validate_sql 与 execute_query 各调用 1 次）。

## 四、修改的文件

| 文件 | 性质 | 说明 |
|---|---|---|
| `app/services/result_contract.py` | 新增 | 中立纯函数硬契约模块：`PERIOD_WHITELIST` + `validate_contract_result()`（不调 LLM / 不访问 DB / 不依赖 Attribution）。 |
| `app/services/query_service.py` | 修改 | 移除 `from app.attribution.normalizer import ...`，改为 `from app.services.result_contract import ...`；删除未使用的 `Union` import。执行逻辑不变。 |
| `app/attribution/normalizer.py` | 修改 | 删除内嵌的 `validate_contract_result`，改为从 `result_contract` 导入并轻量 re-export；`_ALLOWED_PERIODS` 引用共享 `PERIOD_WHITELIST`。`Normalizer` 类逻辑未改。 |
| `tests/attribution/test_contract_repair.py` | 修改 | 新增 5 个用例：period 交集语义（union 放宽被拒）/ NULL 指标合法 / bool 非法 / 字符串非法 / 真实默认 repair 路径（只 mock LLM）。 |
| `stage7_accept_api.py` | 新增 | 真实 API/SSE 回归脚本（5 普通 + 两场景各 3 次），保存原始事件并汇总。 |
| `stage7_controlled_real_repair.py` | 新增 | 受控真实默认 repair 验收脚本（不进生产调用路径）。 |
| `stage7_gen_acceptance_results.py` | 新增 | 从原始事件重新生成仓库根 `acceptance_results.json`。 |
| `docs/stage7_acceptance_report.md` | 修改 | 本报告（机器汇总路径 / SSE 契约表述 / 受控真实 repair 验收 / 测试与验收数据更新）。 |
| `acceptance_results.json` | 重建 | 仓库根机器可读汇总（真实 API/SSE 结果）。 |

## 五、自动化测试结果（§四）

| 项 | 命令 | 结果 |
|---|---|---|
| 全量 pytest | `uv run pytest -q` | **343 passed**（6.84s） |
| 字节码编译 | `uv run python -m compileall app` | **OK** |
| 前端构建 | `npm run build`（vite） | **OK**（dist/index.html + css 13.18kB + js 84.07kB，673ms） |

覆盖说明：

- `test_contract_repair.py` 现有 14 个契约修复测试全部保留；本轮新增 5 个：
  period 交集语义（防 union 放宽）/ NULL 指标合法（对齐 `_to_float`）/ bool 指标非法 /
  字符串指标非法 / 真实默认 repair 路径（不 mock 修复函数，只 mock LLM）。
- 既有 Normalizer「不猜列」测试继续通过（见 `test_normalizer.py`）。
- 既有 Attribution / QueryService 全量测试继续通过（整套 343）。

## 六、受控真实默认 repair 验收（§四）

脚本 `stage7_controlled_real_repair.py`（不进生产调用路径）：仅在脚本内 monkeypatch
`app.services.query_service.graph` 使其返回确定性错误契约结果，随后调用生产
`QueryService.execute(query, result_contract)`，**不注入 `contract_repair` fake**，
实际走：真实 `_default_contract_repair` → 当前真实 LLM → `DWMysqlRepository.validate_sql`
→ 真实 MySQL execute → 二次硬契约校验。

### 6.1 直接 execute 验收（列漂移：缺 period_key）

- 漂移 Graph 输出：`columns=["sales_amount"]`（缺契约列 `period_key`）。
- 结果：`QueryExecutionResult.status=success`；真实 LLM 生成修复 SQL 并通过真实 MySQL
  `EXPLAIN` 校验与执行；二次硬契约校验 `reason=None`；`validate_sql` 调用 **1** 次、
  `execute_query` 调用 **1** 次（恰好 repair 一次）。
- 实际修复 SQL：
  ```sql
  SELECT 'comparison' AS period_key, SUM(order_amount) AS sales_amount FROM fact_order
  WHERE date_id BETWEEN 20250101 AND 20250131
  UNION ALL
  SELECT 'current' AS period_key, SUM(order_amount) AS sales_amount FROM fact_order
  WHERE date_id BETWEEN 20250201 AND 20250228
  ```
- 实际返回行：`period_key=comparison → sales_amount=109030.5`、
  `period_key=current → sales_amount=80009.0`（`period_key ∈ {comparison, current}`）。

### 6.2 直接 execute 验收（期间值漂移：period_key=2025-01/2025-02）

- 漂移 Graph 输出：`period_key` 取 `2025-01`/`2025-02`（不在冻结白名单），
  触发原因 `"period 值 '2025-01' 不在允许范围 (comparison/current)"`。
- 结果：同样 `success`，二次硬契约校验 `reason=None`，validate/execute 各 **1** 次；
  真实 LLM 用 `CASE WHEN date_id BETWEEN ... THEN 'comparison'/'current'` 修复，
  返回 `comparison → 109030.5`、`current → 80009.0`。

### 6.3 Attribution query_action_count / action_start 验收

真实 AttributionGraph + 可编程 Planner（3 个查询 Action + finish）完整归因运行，
3 个查询 Action 各触发 1 次真实默认 repair（真实 LLM）：

- `query_action_count == 3`（内部 repair 未 +1）；
- `action_start` 恰好 4 次（3 查询 + 1 finish，未新增）；
- 3 个 Observation 全部 success，归因状态：**completed**。

**结论：受控真实默认 repair 验收 PASS——repair 恰好 1 次 / 修复 SQL 通过真实 DB
校验与执行 / 返回列与 period 值满足 contract / QueryExecutionResult 最终 success /
query_action_count 未增加 / 未新增 action_start。**

## 七、真实最终验收（§五）

### 7.1 五个普通问数（mode=query）

全部 `done.status=completed`、`query_count=1`、无错误（`n_error=0`）。其中
`normal_1`（统计2025年各月销售额）返回核心月销售额，已逐项核对：

- 202501 = **109030.5**
- 202502 = **80009.0**
- 202503 = **90120.0**

与验收基线完全一致。

### 7.2 场景一「为什么2025年2月销售额较1月明显下降？」× 3 连续

| run | 状态 | qcount | Jan | Feb | delta | change_rate | drivers |
|---|---|---|---|---|---|---|---|
| run1 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3 |
| run2 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3 |
| run3 | completed | 5 | 109030.5 | 80009.0 | **-29021.5** | **-26.62%** | 3 |

三次连续运行数值完全稳定；每次 ≥2 个有效拆解维度、≥1 条 driver Evidence，
结论可追溯到 SQL 与确定性计算。

### 7.3 场景二「为什么2025年3月销售数量大幅增长，但销售额增长有限？」× 3 连续

| run | 状态 | qcount | Feb qty | Mar qty | qty change | Feb amount | Mar amount | amount change | avg unit price | drivers |
|---|---|---|---|---|---|---|---|---|---|---|
| run1 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |
| run2 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |
| run3 | completed | 5 | 151 | 322 | **+113.25%** | 80009.0 | 90120.0 | **+12.64%** | 529.86 → 279.88 | 3 |

三次连续运行数值完全稳定；平均单价由后端 Calculator 确定性计算
（80009/151=529.86、90120/322=279.88），出现在 calculation / evidence 链中。

### 7.4 事件 / 状态与泄露扫描

- **SSE 契约共八类**：`route` / `stage` / `action_start` / `query_result` / `calculation` /
  `report` / `error` / `done`；成功链路 `error` 事件次数为 **0**（`n_error=0`）。
- **状态**：11 次 run 的 `done.status` 全部为 `completed`（无 `422` / `failed` / `partial` 异常）。
- **泄露扫描**：对全部 11 个原始事件文件扫描 `password` / `api_key` / `sk-` / `traceback` /
  `数据库密码` / `Prompt` → **0 命中**。未泄露 Prompt、隐藏推理、traceback、API Key、数据库密码。

## 八、query_action_count 合规

- 6 次归因 run 的 `query_action_count` 均为 **5**（≤ 6 上限）。`n_action_start=6`
  （5 个查询 Action + 1 个 finish Action）。
- 内部修复不增加 `query_action_count`：`test_internal_repair_does_not_increase_query_action_count_or_action_start`
  以 3 个查询 Action 各触发 1 次修复验证，`query_action_count` 仍为 3，且 `action_start` 仅 4 次
  （3 查询 + 1 finish）；受控真实默认 repair 验收（§六 6.3）用真实 LLM 复验同一结论。

## 九、内部契约修复是否触发及次数

- 实时 6 次归因 run 中，SQL 首次生成即满足契约（结果稳定且契约合规），
  **修复后路未被运行期触发**（无 repair 警告日志、SSE 无修复标记）。
- 修复路径正确性由单元测试覆盖（缺 alias / period 违约触发一次修复、修复后合规→success、
  修复后仍错→failed 不无限重试、`result_contract=None` 不进修复、不增加
  `query_action_count`、不产生额外 Action/`action_start`），并由
  **受控真实默认 repair 验收（§六）** 以真实 LLM + 真实 MySQL 端到端验证。

## 十、明确未做（§七 禁止项确认）

- 未放宽 Normalizer；未增加第二次 repair；未增加 query_action_count；未新增 Attribution Action；
- 未修改 6 次查询上限；未修改「连续 2 次空/失败强制停止」；
- 未硬编码两个演示问题的 SQL；未对用户问题字符串写 `if/else`；
- 未固定 Planner 必走某几个维度；未重构 DataAgent Graph；未新增基础设施；未处理其它技术债。

## 十一、停止条件确认

- 三项复核问题已修复并重新做最终验收：全量 pytest 343 passed、编译 OK、前端构建 OK；
  5 个普通问数全过；两个冻结场景各 **3 次连续稳定通过**（数值与基线完全一致）；
  受控真实默认 repair 验收通过；无敏感信息泄露。
- 提交后停止，不进入其它 Stage，等待架构复核。

**结论：Stage 7 最终收口完成，三项问题闭合，冻结验收通过。**
