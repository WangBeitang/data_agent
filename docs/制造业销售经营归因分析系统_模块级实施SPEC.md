# 制造业销售经营归因分析系统：模块级实施 SPEC

## 0. 文档定位

本文档是制造业销售经营归因分析系统第一版的模块级实施规格，是以下冻结文档的下位实施约束：

1. `docs/制造业销售经营归因分析系统_概要设计总纲.md`
2. `docs/制造业销售经营归因分析系统_数据对象设计.md`
3. `docs/制造业销售经营归因分析系统_API接口设计.md`
4. `docs/决策文档.md`

本文档冻结：

- 现有代码与新增模块的映射；
- 新增/修改文件职责；
- 模块依赖方向；
- QueryService 结构化改造方式；
- Attribution Graph 节点与循环方式；
- Planner、Action Router、Normalizer、Calculator、Evidence、Report 的实施边界；
- SSE 与前端迁移方式；
- 分阶段实施顺序；
- 测试与验证方式；
- 固定验收值；
- 每阶段停止条件。

执行 Agent 的代码勘察结果只作为现状参考，不作为上位设计依据。若勘察建议与冻结设计、实际代码或本文档冲突，以本文档为准。

第一版仍坚持：

> 核心链路可运行、可复算、可追溯优先；不为未来可能需求提前建设复杂平台能力。

---

# 1. 实施目标

第一版最终必须同时跑通两条链路。

## 1.1 普通问数

```text
POST /api/query
→ AnalysisService
→ Intent Router
→ query
→ QueryService
→ 现有 DataAgent Graph
→ SQL
→ QueryTable
→ SSE query_result
→ done
```

必须保持现有问数 Graph 的核心资产，不另起 SQL Agent。

## 1.2 经营归因

```text
POST /api/query
→ AnalysisService
→ Intent Router
→ attribution
→ Target Parser
→ Attribution Graph
→ Planner
→ Action Router
→ QueryService
→ 现有 DataAgent Graph
→ QueryExecutionResult
→ Normalizer
→ Observation
→ Calculator
→ Evidence Builder
→ Planner 循环
→ Report Generator
→ SSE report
→ done
```

核心要求：

- Planner 只决定下一步 Action；
- SQL 仍由现有问数能力生成；
- 所有关键数值由 Python 计算；
- 主要原因必须能追溯到 SQL 和 Evidence；
- 查询 Action 最多 6 次；
- 两个冻结归因场景可稳定复算。

---

# 2. 实施优先级

发生冲突时按以下顺序处理：

```text
概要设计总纲
>
数据对象设计
>
API 接口设计
>
本文档
>
实际现有代码约束
>
执行 Agent 实现便利性
```

若实现过程中发现前三份冻结文档之间存在真实冲突，停止扩大实现范围，由架构 Agent 决策。

执行 Agent 不得自行修改冻结数据对象、接口字段或业务范围来“方便实现”。

---

# 3. 正式架构决策

## 3.1 现有 DataAgent Graph 保留

以下对象继续承担普通问数内部实现：

```text
app/agent/graph.py
app/agent/state.py
app/agent/context.py
app/agent/nodes/*
```

不搬目录，不重写 Graph，不把归因逻辑塞进 `DataAgentState`。

允许对现有问数 Graph 做两类最小改造：

1. 增加结构化查询结果所必需的内部字段；
2. 给现有阶段事件增加稳定 `stage_code`。

这不属于“将 DataAgentState 扩展为归因 State”。

---

## 3.2 公共分析数据对象放入中立模型模块

新增：

```text
app/models/analysis.py
```

该文件承载数据对象设计中冻结的公共枚举与 Pydantic Model，包括：

- RequestMode
- AnalysisMode
- RouteSource
- AnalysisStatus
- ActionType
- MetricKey
- DimensionKey
- ObservationStatus
- CalculationType
- FactorDirection
- FilterOperator
- Period
- FilterCondition
- QueryTable
- MetricPeriodValue
- ComparisonRow
- RouteResult
- AttributionTarget
- QueryExecutionResult
- Action
- Observation
- 各 Calculation
- Evidence
- AttributionReport 及其子对象

不将 `QueryExecutionResult` 放到 `app/attribution/`，避免：

```text
services/query_service.py
→ attribution
```

这种反向依赖。

不为每个枚举或模型单独拆文件。

---

## 3.3 AttributionState 独立

新增：

```text
app/attribution/state.py
```

只定义：

- `AttributionState`
- `AttributionContext`

`AttributionState` 使用 `TypedDict`，字段按照数据对象设计冻结。

`AttributionContext` 至少包含：

```python
query_service: QueryService
```

原因：

- `QueryService` 持有请求级数据库 Session；
- Attribution Graph 是模块级编译对象；
- 不能把请求级 QueryService 放成全局单例；
- 不能让 Attribution Graph 直接重新创建 Repository/Session。

LLM 继续复用现有 `app.agent.llm.llm`，第一版不新增 LLM Client Manager。

---

## 3.4 analysis_id 复用现有 req_id

现有 HTTP middleware 已为每个请求生成唯一 `req_id`。

第一版规定：

```text
analysis_id = req_id
```

不再生成第二套业务 UUID。

原因：

- 第一版一个 HTTP 请求只对应一次分析；
- API 只要求请求内唯一，不要求固定前缀；
- 减少日志关联和排障复杂度。

`AnalysisService` 使用 `get_req_id()` 取得 ID。

在脱离 HTTP 的单元测试中，如 `req_id` 为空，允许使用 `uuid4()` 作为 fallback。

不修改 `main.py` 的 req_id middleware。

---

## 3.5 QueryService 同时支持流式与结构化执行

最终 QueryService 对外提供两个能力：

```python
async def execute(
    query: str,
    result_contract: dict | None = None,
) -> QueryExecutionResult
```

用于 Attribution Provider。

以及：

```python
async def stream(
    query: str,
) -> AsyncIterator[dict]
```

用于普通问数模式的阶段流。

两者都调用现有 `app.agent.graph.graph`，不复制问数业务逻辑。

现有 `search()` 可在迁移阶段暂时保留，Stage 3 完成后若已无引用，可删除；不要求长期维护两套公开接口。

---

## 3.6 归因子查询增加内部结果契约

这是第一版稳定归一化的关键实现约束。

普通问数：

```text
QueryService.execute/stream(query)
result_contract = None
```

归因问数：

```text
Action Router
→ sub_query
→ internal result_contract
→ QueryService.execute(sub_query, result_contract)
```

`result_contract` 是内部实现参数，不进入 API，不进入 Action，不作为新的业务数据对象。

用途：

- 约束 SQL 结果列别名；
- 让 Normalizer 不依赖 LLM 任意命名；
- 保证两个固定归因场景稳定复算。

推荐契约：

### 总体比较

```json
{
  "period_alias": "period_key",
  "dimension_alias": null,
  "metric_aliases": {
    "sales_amount": "sales_amount"
  },
  "period_values": ["comparison", "current"]
}
```

要求 SQL 返回：

```text
period_key
sales_amount
```

其中 `period_key` 只允许：

```text
comparison
current
```

### 维度拆解

例如类别：

```json
{
  "period_alias": "period_key",
  "dimension_alias": "dimension_value",
  "metric_aliases": {
    "sales_amount": "sales_amount"
  },
  "period_values": ["comparison", "current"]
}
```

要求 SQL 返回：

```text
dimension_value
period_key
sales_amount
```

### 量额分析

```text
period_key
sales_amount
sales_quantity
```

`generate_sql.prompt` 增加 `result_contract` 输入：

- 为空：维持普通问数行为；
- 非空：必须严格按照契约生成 SELECT 列别名和值。

这样 Normalizer 只验证契约，不靠模糊猜列。

---

## 3.7 stage_code 必须由问数节点直接产生

禁止在 `AnalysisService` 中通过：

```text
"执行SQL" → sql_execution
"生成SQL" → sql_generation
```

这种中文字符串反查方式。

现有节点的 `runtime.stream_writer()` 统一增加稳定字段：

```json
{
  "stage_code": "sql_generation",
  "stage": "生成SQL"
}
```

映射固定：

| 节点 | stage_code |
| --- | --- |
| `extract_keywords` | `query_retrieval` |
| `recall_column` | `query_retrieval` |
| `recall_metric` | `query_retrieval` |
| `recall_value` | `query_retrieval` |
| `merge_retrieved_info` | `query_retrieval` |
| `filter_metric` | `query_retrieval` |
| `filter_table` | `query_retrieval` |
| `add_extra_context` | `query_retrieval` |
| `generate_sql` | `sql_generation` |
| `validate_sql` | `sql_validation` |
| `correct_sql` | `sql_validation` |
| `execute_sql` | `sql_execution` |

多个连续内部节点拥有同一 `stage_code` 时，外部 SSE 只展示一个阶段。

---

## 3.8 stage success/failed 由 AnalysisService 补齐

现有问数节点只在阶段开始时写事件，不要求本期把 12 个节点全部改造成 start/success 双事件。

`AnalysisService` 内维护当前活动阶段：

```text
收到新 stage_code
→ 上一个阶段发 success
→ 当前阶段发 running

收到 query_result
→ 当前阶段发 success

发生错误
→ 当前阶段发 failed
→ error

请求结束
→ done
```

连续收到相同 `stage_code` 时不重复向前端发送。

这样既满足 API 契约，也避免大规模改动现有 Graph 节点。

---

## 3.9 不新增 provider.py

概要设计中的 Provider 职责由：

```text
QueryService
+ DataAgent Graph
```

共同承担。

Attribution Graph 内的执行节点直接：

```text
Action Router
→ QueryService.execute()
→ Normalizer
→ Observation
```

第一版不再新增只做一层转发的 `provider.py`。

---

## 3.10 Report 不完全交给 LLM

`report_generator.py` 采用：

```text
确定性结构装配
+ LLM 语言组织
```

确定性生成：

- metric_overview
- drivers
- offsets
- evidence_ids
- data_boundaries 基础内容
- 所有数值
- Evidence 引用

LLM 只允许组织：

- question_definition 文案；
- core_conclusion；
- factor summary；
- 与已确认因素对应的 recommendations。

如果报告 LLM 失败：

- 不丢弃已获得的 Evidence；
- 使用确定性模板生成可读报告；
- 只有确定性报告也无法生成时，才使用 `REPORT_GENERATION_FAILED`。

禁止因为 LLM 报告失败把已有真实分析事实全部丢掉。

---

## 3.11 pytest 明确作为本项目新增开发依赖

当前项目没有冻结 pytest 测试基础。

本期新增：

```text
pytest
```

作为开发依赖。

不额外引入大型测试框架。

优先用 pytest 覆盖：

- 数据对象；
- Action 校验；
- Normalizer；
- Calculator；
- 停止条件；
- Planner fallback；
- API 请求校验。

真实 MySQL/Qdrant/ES/LLM 链路继续使用端到端验收。

---

# 4. 最终目标目录

```text
data-agent/
├── app/
│   ├── agent/                         # 现有问数能力
│   │   ├── graph.py
│   │   ├── state.py                   # 最小增加结果字段/result_contract
│   │   └── nodes/
│   │       └── *.py                   # 增加 stage_code；execute_sql 返回结构化结果
│   │
│   ├── models/
│   │   └── analysis.py                # 新增：统一分析数据对象
│   │
│   ├── attribution/                   # 新增：归因能力
│   │   ├── __init__.py
│   │   ├── state.py
│   │   ├── intent_router.py
│   │   ├── target_parser.py
│   │   ├── planner.py
│   │   ├── action_router.py
│   │   ├── normalizer.py
│   │   ├── calculator.py
│   │   ├── evidence_builder.py
│   │   ├── report_generator.py
│   │   └── graph.py
│   │
│   ├── services/
│   │   ├── query_service.py
│   │   └── analysis_service.py        # 新增
│   │
│   └── api/
│       ├── dependencies.py
│       ├── routers/query_router.py
│       └── schemas/query_schema.py
│
├── prompts/
│   ├── attribution_intent.prompt      # 新增
│   ├── attribution_target.prompt      # 新增
│   ├── attribution_planner.prompt     # 新增
│   ├── attribution_report.prompt      # 新增
│   └── 现有问数 prompt
│
├── conf/
│   └── meta_config.yaml
│
├── tests/
│   ├── test_analysis_models.py
│   ├── test_query_service.py
│   └── attribution/
│       ├── test_action_router.py
│       ├── test_normalizer.py
│       ├── test_calculator.py
│       ├── test_evidence_builder.py
│       └── test_stopping.py
│
├── pyproject.toml
└── uv.lock


date-agent-frontend/
└── src/
    ├── App.vue
    ├── composables/
    │   └── useAnalysisStream.js
    └── components/
        ├── AnalysisTimeline.vue
        ├── ResultTable.vue
        ├── AttributionReport.vue
        └── ContributionChart.vue
```

第一版不增加：

```text
provider.py
enums.py
models/ 子包
agent registry
repository for attribution
report persistence
task repository
websocket
redis
message queue
```

---

# 5. 新增文件职责

## 5.1 app/models/analysis.py

职责：

- 实现数据对象设计的全部枚举与 Pydantic Model；
- 执行字段级和模型级校验；
- 供 API、services、attribution 共同依赖。

必须包含：

- `Action` 条件校验；
- `Period.start_date <= end_date`；
- `QueryTable.row_count == len(rows)`；
- `RouteResult.resolved_mode != auto`；
- Calculation 对应字段；
- AttributionReport 子对象。

不得：

- 调 LLM；
- 调数据库；
- 包含 LangGraph 节点；
- 放业务流程函数。

---

## 5.2 app/attribution/state.py

职责：

- `AttributionState`；
- `AttributionContext`。

`AttributionContext`：

```text
query_service
```

即可。

不新增 Repository 字段。

---

## 5.3 app/attribution/intent_router.py

职责：

1. 处理强制模式；
2. `auto` 时规则优先；
3. 模糊问题使用 LLM；
4. 返回 `RouteResult`。

规则：

### query 明确词

包括但不限于：

```text
统计
查询
多少
排名
分别是多少
```

### attribution 明确词

包括但不限于：

```text
为什么
原因
归因
哪些因素导致
下降原因
增长原因
增长但
下降但
```

优先级：

```text
forced mode
> attribution 强特征
> query 强特征
> LLM
```

LLM 输出只允许：

```json
{"mode":"query"}
```

或：

```json
{"mode":"attribution"}
```

解析失败可重试一次。

第二次失败时第一版降级为 `query`，同时服务端记录 warning；不因为模糊意图分类格式错误直接让整个普通问数能力不可用。

---

## 5.4 app/attribution/target_parser.py

职责：

从归因问题得到：

- metrics；
- current_period；
- comparison_period。

输出：

```text
AttributionTarget
```

优先使用明确文本信息。

第一版不实现服务端会话追问。

如果以下信息无法可靠确定：

- 对比指标；
- 本期；
- 对比期；

则返回 `TARGET_PARSE_FAILED`。

不擅自补不存在的生产/库存/成本指标。

量额背离问题：

```text
metrics = [sales_quantity, sales_amount]
```

平均单件销售额由后续 Calculator 派生，不要求加入 target.metrics。

---

## 5.5 app/attribution/planner.py

职责：

```text
AttributionState
→ 单个下一步 Action
```

Planner 输入只提供：

- 原始问题；
- Target；
- 已执行 Action 摘要；
- Observation 归一化摘要；
- Calculation；
- Evidence；
- 剩余查询次数。

不提供：

- Prompt 隐藏推理；
- 数据库凭证；
- 无关 Graph 内部召回状态。

输出必须是 Action JSON。

### 校验失败

第一次非法：

```text
Pydantic 校验错误
→ 回填 Planner
→ 重试一次
```

第二次仍非法：

使用冻结通用 fallback：

```text
compare_period
→ breakdown_region
→ breakdown_category
→ breakdown_product
→ finish_analysis
```

Fallback：

- 只固定动作路线；
- 不固定 SQL；
- 不固定查询结果；
- 不固定结论。

如果相应动作已经执行过，跳过到下一个未执行动作。

---

## 5.6 app/attribution/action_router.py

职责：

1. Action 白名单校验；
2. 参数条件校验；
3. Action 去重；
4. 查询次数限制；
5. 受控 Filter 校验；
6. 生成 sub_query；
7. 生成内部 `result_contract`。

### sub_query

使用模板拼接，LLM 不参与。

例如：

#### compare_period

```text
分别统计【对比期】和【本期】的【指标列表】。
```

#### breakdown_region

```text
分别统计【对比期】和【本期】各销售区域的【指标列表】。
```

#### breakdown_category

```text
分别统计【对比期】和【本期】各产品类别的【指标列表】。
```

#### breakdown_product

```text
分别统计【对比期】和【本期】各产品的【指标列表】，
并限定【filters】。
```

#### breakdown_customer

```text
分别统计【对比期】和【本期】各客户/客户等级的【指标列表】。
```

#### analyze_unit_price

```text
分别统计【对比期】和【本期】的销售额和销售数量。
```

禁止把任何用户输入直接拼成 SQL。

### 去重

使用数据对象设计冻结的逻辑键。

`calculate_contribution` 只允许引用尚未生成对应贡献 Calculation 的成功 breakdown Observation。

---

## 5.7 app/attribution/normalizer.py

职责：

```text
Action
+ QueryExecutionResult
+ result_contract
→ normalized_rows
```

Normalizer 只接受符合 `result_contract` 的列。

不得：

- 猜测任意 LLM 列名；
- 自己执行 SQL；
- 做贡献计算；
- 生成报告解释。

### 期间值

`period_key` 只接受：

```text
comparison
current
```

### 维度缺失成员

如果同一查询成功覆盖两个期间：

- 某成员只在一个期间出现；
- 另一期间没有该成员行；

则缺失期间指标按 `0` 处理。

这是“该成员当期无销售记录”，不是未知数据。

### 空查询

`row_count == 0`：

```text
ObservationStatus.empty
normalized_rows = []
```

### 契约错误

缺少：

- period_key；
- dimension_value；
- 约定 metric alias；

则：

```text
ObservationStatus.failed
RESULT_NORMALIZATION_FAILED
```

不做模糊猜测兜底。

---

## 5.8 app/attribution/calculator.py

纯确定性模块。

禁止：

- LLM；
- Repository；
- HTTP；
- SQL。

建议使用 `Decimal` 完成内部金额与比率运算，输出时转为 JSON number。

必须实现：

### period_change

```text
delta = current - comparison
change_rate = delta / comparison
```

comparison 为 0：

```text
change_rate = null
```

### contribution

```text
item_delta = item.current - item.comparison
contribution_rate = item_delta / total_delta
```

total_delta 为 0：

```text
contribution_rate = null
```

方向：

```text
与 total_delta 同号 → driver
反号 → offset
0 → neutral
```

贡献率允许：

```text
> 1
< 0
```

### unit_price

```text
sales_amount / sales_quantity
```

quantity 为 0：

```text
unit_price = null
```

---

## 5.9 app/attribution/evidence_builder.py

职责：

```text
Action
+ Observation
+ Calculation
→ Evidence[]
```

原则：

- 查询事实必须来自 success Observation；
- 变化数值必须引用 Calculation；
- breakdown 贡献可以按成员生成 Evidence；
- 报告主要因素必须引用 Evidence；
- 不生成置信度；
- 不写入不存在于输入中的数字。

Evidence 不复制完整 SQL。

SQL 通过：

```text
Evidence
→ observation_id
→ query_result
```

追溯。

---

## 5.10 app/attribution/report_generator.py

按第 3.10 节实现。

驱动/抵消因素排序第一版采用：

```text
abs(delta) 降序
```

报告默认展示：

- 最多 3 个主要 driver；
- 最多 3 个主要 offset；

Evidence 明细仍保留完整证据，不因此删除其它因素。

不冻结“80%覆盖度”等复杂阈值。

### 数据边界固定基础文案

至少包含：

```text
当前归因仅基于现有销售订单数据，属于数据归因，不代表实验验证的因果关系。
当前数据不支持库存、成本、利润、生产、设备、质量、产能等原因验证。
```

若查询失败或数据为空，追加具体缺口。

---

## 5.11 app/attribution/graph.py

负责真正多步归因循环。

该文件包含：

- 编译的 `Attribution Graph`；
- LangGraph node；
- 条件边；
- 对外 runner/stream 入口。

不把 Calculator、Normalizer 等具体逻辑重新写在 graph.py 中，只调用对应模块。

---

## 5.12 app/services/analysis_service.py

HTTP 层唯一业务服务入口。

职责：

```text
analysis_id
→ route
→ query / attribution
→ SSE event
→ error
→ done
```

包含：

- SSE 序列化；
- stage 状态跟踪；
- QueryService 普通问数事件适配；
- Attribution Graph 事件适配；
- 错误码映射；
- done 兜底。

不得：

- 生成 SQL；
- 做贡献计算；
- 直接操作 Repository；
- 写 Planner Prompt。

---

# 6. 现有文件修改职责

## 6.1 app/agent/state.py

最小增加：

```text
result_contract
result_columns
result_rows
```

其中：

- `result_contract`：普通问数为 None，归因 QueryService.execute 时传入；
- `result_columns/result_rows`：最终 SQL 查询结果。

不增加：

- Action；
- Evidence；
- AttributionReport；
- Planner history。

---

## 6.2 app/repositories/mysql/dw_mysql_repository.py

新增结构化查询方法，推荐：

```python
async def execute_query(self, sql) -> tuple[list[str], list[dict]]
```

实现：

```text
result.keys() → columns
result.mappings().all() → rows
```

现有 `execute_sql()` 暂不删除，避免无关调用被破坏。

Repository 不返回 Pydantic `QueryTable`，只返回基础 Python 数据。

---

## 6.3 app/agent/nodes/execute_sql.py

改为调用：

```text
dw_mysql_repo.execute_query()
```

返回给 State：

```text
result_columns
result_rows
```

custom stream 中在迁移期可继续保留：

```json
{"result":[...]}
```

同时增加内部结构化字段供 QueryService 使用，例如：

```json
{
  "result": [...],
  "query_result": {
    "sql": "...",
    "columns": [...],
    "rows": [...]
  }
}
```

注意：

`result` 只是内部兼容字段，不允许被新的 AnalysisService 原样暴露为正式 API。

---

## 6.4 其它 app/agent/nodes/*.py

只增加稳定 `stage_code`。

不修改节点业务逻辑。

尤其本期不顺手重构：

- recall 并行逻辑；
- SQL correction 流程；
- prompt chain 写法；
- Graph 边。

---

## 6.5 app/agent/nodes/generate_sql.py

额外读取：

```text
result_contract
```

传给：

```text
generate_sql.prompt
```

普通问数为空。

归因子查询非空。

---

## 6.6 prompts/generate_sql.prompt

新增规则：

```text
当 result_contract 非空时：
必须严格按照指定列别名输出结果；
period_key 值必须遵循 contract；
不得额外改变 contract 字段名。
```

继续保留：

- 只输出 SQL；
- 使用实际表字段；
- 当前 MySQL 方言；
- 原有元数据信息。

---

## 6.7 app/services/query_service.py

最终职责：

### execute

```text
query
+ optional result_contract
→ graph.ainvoke
→ QueryExecutionResult
```

### stream

```text
query
→ graph.astream(custom)
→ 内部 stage/query_result chunks
```

错误处理：

- 详细 exception 写日志；
- 返回/抛出的用户消息必须清洗；
- 不把 `str(e)` 直接透传 SSE。

---

## 6.8 app/api/schemas/query_schema.py

改为：

```text
query
mode = auto
```

约束按照 API 文档。

直接复用：

```text
RequestMode
```

---

## 6.9 app/api/dependencies.py

新增：

```text
get_analysis_service
```

复用现有：

```text
get_query_service
```

不重新创建 Repository。

---

## 6.10 app/api/routers/query_router.py

最终只保留：

```text
接收 QuerySchema
→ Depends AnalysisService
→ StreamingResponse
```

不做意图判断。

不直接依赖 QueryService。

---

## 6.11 conf/meta_config.yaml

物理字段不变。

业务描述改为制造业销售经营口径。

核心指标至少配置：

### 销售额

```text
SUM(order_amount)
```

别名可包含：

```text
销售额
销售金额
订单金额
收入
```

### 销售数量

```text
SUM(order_quantity)
```

### 销售订单数

```text
COUNT(DISTINCT order_id)
```

### 平均单件销售额

```text
SUM(order_amount) / SUM(order_quantity)
```

说明必须明确：

```text
仅表示销售额与销售数量的比值。
```

不得继续把 GMV/AOV 作为项目主业务口径。

产品字段中文描述统一使用：

```text
产品
产品类别
```

`member_level` 使用：

```text
客户等级
```

---

# 7. Attribution Graph 详细 SPEC

## 7.1 Graph 输入

在进入编译 Graph 前先完成：

```text
RouteResult
AttributionTarget
```

Runner 初始化：

```text
AttributionState(
  analysis_id,
  question,
  requested_mode,
  route,
  target,
  actions=[],
  observations=[],
  calculations=[],
  evidences=[],
  query_action_count=0,
  consecutive_empty_or_failed=0,
  max_query_actions=6,
  status=running,
  report=None,
  failure_reason=None
)
```

---

## 7.2 Graph 节点

建议固定为：

```text
plan_next
validate_and_route_action
execute_query_action
normalize_observation
calculate
build_evidence
generate_report
```

不为每种 Action 单独创建 Graph node。

---

## 7.3 主流程

```text
START
  ↓
plan_next
  ↓
validate_and_route_action
  ├─ query action
  │    ↓
  │ execute_query_action
  │    ↓
  │ normalize_observation
  │    ↓
  │ calculate
  │    ↓
  │ build_evidence
  │    ↓
  │ plan_next
  │
  ├─ calculate_contribution
  │    ↓
  │ calculate
  │    ↓
  │ build_evidence
  │    ↓
  │ plan_next
  │
  └─ finish_analysis
       ↓
     generate_report
       ↓
      END
```

---

## 7.4 查询 Action 执行

顺序：

```text
Action Router.build_execution_spec()
→ action_start event
→ query_action_count + 1
→ QueryService.execute()
→ QueryExecutionResult
→ Observation
```

QueryService 每个查询 Action 只调用一次。

---

## 7.5 自动计算

成功 Observation 后：

### compare_period

自动生成：

```text
PeriodChangeCalculation
```

### breakdown_*

自动生成：

```text
ContributionCalculation
```

前提：

已有目标指标总体变化额。

如果尚无总体变化 Calculation：

- 不生成贡献；
- Evidence 只保留原始拆解事实；
- Planner 应优先补 compare_period。

### analyze_unit_price

自动生成：

```text
UnitPriceCalculation
```

因此正常路线不要求 Planner 专门选择 `calculate_contribution`。

`calculate_contribution` Action 保留为合法本地动作，但只用于已有成功 breakdown Observation 尚未生成贡献 Calculation 的情况。

---

# 8. 停止条件实施

## 8.1 强制停止

满足任一：

```text
query_action_count >= 6
consecutive_empty_or_failed >= 2
```

则禁止继续查询。

强制停止时系统生成受控结束，不再让 Planner 发查询 Action。

最终：

- 有有效 Evidence → `partial`
- 无有效 Evidence → `failed`

达到查询上限不能自动标记 `completed`。

---

## 8.2 正常 completed

Planner 主动选择 `finish_analysis` 时，Action Router 只在以下条件满足时接受：

1. 有成功总体比较；
2. 至少两个不同维度成功 breakdown；
3. 至少存在一个 `driver` Evidence；
4. 当前不是连续错误导致的强制结束；
5. Planner 判断无新的高价值下钻。

如果 Planner 过早 finish：

- 返回校验错误给 Planner；
- 重试一次；
- 仍失败进入 fallback。

---

## 8.3 Action 重复

相同 Action 逻辑键：

```text
禁止重复查询
```

Planner 重复时：

- 作为 Action 校验失败；
- 提示选择其它方向；
- 不增加 query_action_count。

---

# 9. Planner 正常路线要求

Planner 不是固定脚本，但两个冻结场景应倾向以下路线。

## 9.1 销售额下降

```text
compare_period(sales_amount)
→ breakdown_region
→ breakdown_category
→ breakdown_product(重点负向类别)
→ 可选 breakdown_customer
→ finish
```

---

## 9.2 量额背离

```text
compare_period(sales_quantity, sales_amount)
→ analyze_unit_price
→ breakdown_category(sales_quantity, sales_amount)
→ breakdown_product(重点类别)
→ 必要时补其它维度
→ finish
```

Planner 不需要逐字匹配以上顺序，但必须获得能够支撑报告的证据。

---

# 10. Prompt 文件 SPEC

新增：

```text
prompts/attribution_intent.prompt
prompts/attribution_target.prompt
prompts/attribution_planner.prompt
prompts/attribution_report.prompt
```

---

## 10.1 attribution_intent.prompt

只分类：

```text
query
attribution
```

不做回答。

不做 SQL。

---

## 10.2 attribution_target.prompt

只抽取：

```text
metrics
current_period
comparison_period
```

不提供原因。

不提供数字事实。

---

## 10.3 attribution_planner.prompt

必须明确：

- 只能从 Action 白名单选；
- 一次只输出一个 Action；
- 不输出 SQL；
- 不计算数值；
- 不编造 Observation；
- 依据已有 Evidence；
- 尊重剩余步数；
- 不查询生产/库存/成本等不存在维度。

---

## 10.4 attribution_report.prompt

输入只提供：

- question；
- target；
- Evidence；
- 已计算 factor；
- 数据缺口。

提示词明确：

```text
禁止增加未出现在 Evidence 中的业务事实或数字。
禁止把数据相关描述成已验证因果。
建议必须对应已有因素。
```

---

# 11. API/SSE 实施 SPEC

完全遵守 API 接口设计。

## 11.1 AnalysisService 输出

每条事件：

```text
data: <单行JSON>\n\n
```

全部包含：

```text
type
analysis_id
```

---

## 11.2 普通问数

```text
route
→ stage*
→ query_result
→ done
```

QueryService 内部 legacy：

```json
{"result":[...]}
```

不得出现在外部新 API。

---

## 11.3 归因

```text
route
→ stage(target_parsing)
→ stage(planning)
→ action_start
→ query_result
→ calculation
→ ...
→ report
→ done
```

---

## 11.4 error

统一由 AnalysisService 转为 API 错误对象。

禁止：

```text
traceback
absolute path
database password
LLM key
prompt
raw hidden reasoning
```

---

## 11.5 done

SSE 已建立后：

```text
AnalysisService finally
→ 尽量保证 done
```

网络主动断开除外。

---

# 12. 前端实施 SPEC

## 12.1 App.vue

最终只负责：

- 页面骨架；
- 输入；
- 示例问题；
- 页面级 analysis state；
- 组合子组件。

不继续把所有 SSE 解析、时间线、表格和报告全部堆在单文件。

---

## 12.2 useAnalysisStream.js

职责：

```text
fetch POST /api/query
→ reader
→ buffer
→ \n\n split
→ parse data JSON
→ emit event callback
```

必须以：

```text
data.type
```

分发。

收到：

```text
done
```

后正常结束 loading。

连接关闭但未收到 done：

```text
标记连接异常
```

---

## 12.3 AnalysisTimeline.vue

展示：

- stage；
- Action；
- success/running/failed；
- SQL 执行状态。

不展示模型隐藏思考。

---

## 12.4 ResultTable.vue

使用：

```text
table.columns
table.rows
```

禁止继续通过：

```javascript
Object.keys(rows[0])
```

推导正式列结构。

空结果时仍能展示正确空状态。

---

## 12.5 AttributionReport.vue

完整支持概要设计七部分：

1. 问题定义；
2. 核心结论；
3. 指标概览；
4. 主要驱动因素；
5. 抵消因素；
6. 证据明细；
7. 数据边界与建议。

必须支持：

```text
报告复制
```

这是第一版既定范围，不延期。

---

## 12.6 ContributionChart.vue

原生 Vue + CSS。

数据来源：

```text
report.drivers
report.offsets
```

使用：

```text
delta
```

作为条形长度主要依据。

第一版不增加 ECharts/Chart.js。

---

# 13. 测试 SPEC

## 13.1 开发依赖

新增 pytest。

建议：

```bash
uv add --dev pytest
```

同步：

```text
pyproject.toml
uv.lock
```

---

## 13.2 test_analysis_models.py

至少验证：

- auto 不能作为 resolved mode；
- Period 日期顺序；
- QueryTable row_count；
- Action 条件校验；
- FilterOperator；
- Report 引用结构。

---

## 13.3 test_action_router.py

至少验证：

- 8 类 Action；
- dimension 白名单；
- filter 白名单；
- 重复 Action；
- query_action_count=6；
- premature finish；
- calculate_contribution 来源 Observation 限制；
- result_contract 生成。

---

## 13.4 test_normalizer.py

至少验证：

- compare_period；
- region/category/product/customer breakdown；
- 多 metric；
- 成员仅一个期间存在时另一期间补 0；
- empty；
- 缺 period_key；
- 缺 metric alias；
- 错误 period value；
- 不接受任意猜测列。

---

## 13.5 test_calculator.py

固定验证：

### 场景一

```text
109030.5
80009.0
delta = -29021.5
change_rate ≈ -0.2662
```

### 场景二

```text
151
322
quantity change_rate ≈ 1.1325

80009.0
90120.0
amount change_rate ≈ 0.1264

529.86 → 279.88
```

同时验证：

- comparison=0；
- total_delta=0；
- quantity=0；
- contribution_rate > 1；
- contribution_rate < 0；
- driver/offset/neutral。

---

## 13.6 test_stopping.py

验证：

- 第 6 个查询后不能继续；
- 连续两次 empty 停止；
- empty + failed 连续两次停止；
- success 重置连续失败计数；
- 强制停止最多 partial；
- 无 Evidence → failed；
- 正常 finish 最低条件。

---

# 14. 实施阶段

每个 Stage 独立提交和复核。

执行 Agent **完成当前 Stage 后必须停止**，不得顺手进入下一 Stage。

---

# Stage 1：销售经营口径切换 + 测试基础

## 目标

先让现有普通问数以新业务口径稳定运行。

## 修改

```text
conf/meta_config.yaml
现有有效 prompts
pyproject.toml
uv.lock
```

新增：

```text
tests/
```

## 要求

- 不修改 dw2 表；
- 不修改 115 条数据；
- 去掉主口径 GMV/AOV；
- 加四个核心销售指标；
- 更新商品/会员等主要电商文案；
- 新增 pytest 开发依赖；
- 重建 meta2/Qdrant/ES。

## 验收

五个问题：

1. 统计 2025 年各月销售额；
2. 查询各销售区域销售额排名；
3. 查询各产品类别销售数量；
4. 查询 2025 年 2 月各产品销售额；
5. 查询黄金等级客户的销售额。

要求：

- SQL 可执行；
- 结果与独立 SQL 一致；
- 现有问数页面仍可展示结果；
- 核心月销售额必须为：

```text
2025-01 = 109030.5
2025-02 = 80009.0
2025-03 = 90120.0
```

## 验证命令

```bash
cd data-agent
uv run pytest
uv run python app/scripts/build_meta_knowledge.py
```

随后启动后端并手工验证五个问题。

## 停止条件

五个普通问数问题未全部通过前，不进入 Stage 2。

---

# Stage 2：QueryService 结构化执行边界

## 目标

在不改变外部 API 的情况下，让问数能力同时返回结构化结果。

## 新增

```text
app/models/analysis.py
```

## 修改

```text
app/agent/state.py
app/repositories/mysql/dw_mysql_repository.py
app/agent/nodes/execute_sql.py
app/agent/nodes/generate_sql.py
prompts/generate_sql.prompt
app/services/query_service.py
app/agent/nodes/*.py stage_code
```

## 要求

完成：

```text
QueryService.execute()
QueryService.stream()
QueryExecutionResult
QueryTable
result_contract
stable stage_code
```

现有 `/api/query` 在此 Stage 完成时仍可继续按旧方式工作。

## 验收

- execute success；
- execute empty；
- execute failed；
- empty QueryTable 仍有 columns；
- result_contract 下 SQL 输出固定 alias；
- 原普通问数不传 contract 时保持行为；
- 五个普通问数继续通过。

## 停止条件

QueryService 结构化结果未稳定前，不实现 Attribution Graph。

---

# Stage 3：统一 AnalysisService + 新 SSE 契约 + query 模式前端迁移

## 目标

先完成新的统一接口，但只要求普通问数完整跑通。

## 新增

```text
app/services/analysis_service.py
app/attribution/__init__.py
app/attribution/intent_router.py
prompts/attribution_intent.prompt

date-agent-frontend/src/composables/useAnalysisStream.js
date-agent-frontend/src/components/AnalysisTimeline.vue
date-agent-frontend/src/components/ResultTable.vue
```

## 修改

```text
app/api/schemas/query_schema.py
app/api/dependencies.py
app/api/routers/query_router.py
date-agent-frontend/src/App.vue
```

## 要求

实现：

```text
mode=auto/query/attribution
route
stage
query_result
error
done
```

本 Stage `attribution` 可以返回明确的：

```text
NOT_IMPLEMENTED / TARGET_PARSE_FAILED
```

但不得伪造归因成功。

普通问数正式迁移到：

```text
table.columns
table.rows
```

旧 API `result` 不再暴露。

## 验收

```text
{query:"统计2025年各月销售额"}
```

默认 auto：

```text
route(query)
→ stage*
→ query_result
→ done(completed)
```

前端以 `done` 结束 loading。

五个问题继续通过。

## 前端验证

```bash
cd date-agent-frontend
npm run build
```

## 停止条件

新 SSE query 模式未稳定，不进入归因业务实现。

---

# Stage 4：归因确定性核心

## 目标

先实现不依赖 Planner 质量的可测试核心。

## 新增

```text
app/attribution/state.py
app/attribution/action_router.py
app/attribution/normalizer.py
app/attribution/calculator.py
app/attribution/evidence_builder.py

tests/attribution/*
```

## 要求

实现：

- AttributionState；
- AttributionContext；
- Action 校验；
- sub_query；
- result_contract；
- Observation；
- Normalizer；
- 3 类 Calculation；
- Evidence；
- 停止条件纯函数。

不实现完整 LLM Planner。

## 验收

所有数据对象设计第 22 节验收条件中与：

```text
Action
Observation
Calculation
Evidence
```

相关项全部自动测试通过。

固定数值测试全部通过。

## 停止条件

Calculator/Normalizer 未全部自动测试通过，禁止继续做 Planner。

---

# Stage 5：Target Parser + Planner + Attribution Graph + Report

## 目标

完成真实多步归因后端。

## 新增

```text
app/attribution/target_parser.py
app/attribution/planner.py
app/attribution/report_generator.py
app/attribution/graph.py

prompts/attribution_target.prompt
prompts/attribution_planner.prompt
prompts/attribution_report.prompt
```

## 修改

```text
app/services/analysis_service.py
```

## 要求

实现：

- Target parsing；
- Planner 单 Action；
- invalid retry once；
- fallback route；
- QueryService Provider；
- Graph 循环；
- 最多 6 query actions；
- 连续两次 empty/failed；
- completed/partial/failed；
- report；
- calculation/report SSE。

## 后端冻结场景验收

### 场景一

```text
为什么 2025 年 2 月销售额较 1 月明显下降？
```

必须：

```text
route = attribution
1月销售额 = 109030.5
2月销售额 = 80009.0
delta = -29021.5
change_rate ≈ -26.62%
>= 2 个有效 breakdown dimension
主要原因有 Evidence
query actions <= 6
```

### 场景二

```text
为什么 2025 年 3 月销售数量大幅增长，但销售额增长有限？
```

必须：

```text
2月数量 = 151
3月数量 = 322
数量变化率 ≈ 113.25%

2月销售额 = 80009.0
3月销售额 = 90120.0
销售额变化率 ≈ 12.64%

平均单件销售额 ≈ 529.86 → 279.88
```

平均单件销售额必须来自 Calculation。

## 停止条件

两个场景后端事件流未稳定，不进入最终前端归因展示。

---

# Stage 6：归因分析工作台

## 目标

完成第一版演示页面。

## 新增

```text
AttributionReport.vue
ContributionChart.vue
```

## 修改

```text
App.vue
AnalysisTimeline.vue
useAnalysisStream.js
```

## 必须展示

- 示例问题；
- 当前分析模式；
- 执行时间线；
- Action；
- SQL；
- QueryTable；
- 指标概览；
- ContributionChart；
- 七段归因报告；
- Evidence；
- 数据边界；
- 报告复制。

## 不增加

```text
ECharts
Chart.js
Pinia
Router
WebSocket
```

除非实际实现发现 Vue 当前结构无法完成核心页面；否则禁止自行引依赖。

## 验收

两个固定场景可在页面完整演示。

刷新页面允许丢失状态。

## 验证

```bash
npm run build
```

必须通过。

## 停止条件

页面两个归因场景未完整展示，不进入最终收尾。

---

# Stage 7：端到端冻结验收

## 目标

只做缺陷修复与最终验证，不新增功能。

## 全量验收

### 普通问数

5 个固定问题全部通过。

### Attribution

2 个固定场景全部通过。

### API

验证：

```text
mode auto/query/attribution
8 类 SSE event
done
partial
failed
422
```

### 边界

验证：

- comparison=0；
- total_delta=0；
- quantity=0；
- duplicate action；
- 6 query limit；
- 2 consecutive empty/failed；
- unsupported dimension；
- report 不增加无 Evidence 数值。

### 安全

SSE 不出现：

```text
password
API key
traceback
Prompt
hidden reasoning
```

### 构建

```bash
cd data-agent
uv run pytest
uv run python -m compileall app

cd ../date-agent-frontend
npm run build
```

全部通过。

## 停止条件

完成上述验收后停止。

不在 Stage 7：

- 顺手重构；
- 增加后台；
- 增加会话；
- 增加持久化；
- 增加更多归因领域。

---

# 15. 明确不修改

第一版默认不修改：

```text
data-agent/sql/dw.sql
data-agent/sql/meta.sql
data-agent/app/agent/graph.py 的整体拓扑
data-agent/app/agent/context.py
data-agent/app/clients/*
data-agent/app/repositories/es/*
data-agent/app/repositories/qdrant/*
data-agent/app/services/meta_knowledge_service.py
data-agent/app/scripts/build_meta_knowledge.py
data-agent/main.py
date-agent-frontend/vite.config.js
```

如果实施阶段证明其中某文件必须修改才能跑通核心链路，执行 Agent必须在结果中说明原因，不得静默扩大范围。

---

# 16. 已知 V1 技术债

以下问题第一版接受，不作为上线阻塞：

1. 不做服务端历史会话；
2. 不做分析任务恢复；
3. 不做 SSE 重连续传；
4. 不做多用户；
5. 不做租户；
6. 不做报告持久化；
7. 不做复杂 Planner 成本控制；
8. 不做模型置信度；
9. 不做实验因果推断；
10. 不扩展生产/库存/成本领域；
11. 现有问数 Graph 不进行架构性重构；
12. 现有 SQL correction 流程除非阻塞固定场景，否则本期不顺手重写。

---

# 17. 执行纪律

执行 Agent 每个 Stage 必须：

1. 先读取四份冻结文档和本文档；
2. 只实现当前 Stage；
3. 不修改下一 Stage 文件，除非当前 Stage 明确列出；
4. 不自行变更 API/Data Object；
5. 不新增无要求基础设施；
6. 完成测试；
7. 输出：
   - 修改文件；
   - 关键实现；
   - 测试结果；
   - 未完成/风险；
   - `git diff --stat`；
8. 停止等待架构复核。

架构复核结果只有三种：

```text
接受
要求修复
延期为技术债
```

未经复核，不自动继续下一 Stage。

---

# 18. 第一版完成定义

只有同时满足以下条件，才算项目第一版完成：

```text
普通问数链路稳定
+
真实多步 Attribution Graph
+
Python 确定性计算
+
SQL/Observation/Calculation/Evidence 可追溯
+
两个固定归因场景稳定复算
+
单页前端完整展示
```

不是以：

```text
文件数量
Agent 数量
Prompt 数量
企业级基础设施数量
```

作为完成标准。

最终系统架构保持：

```text
现有一级问数执行引擎
+
受控 Attribution Graph
+
确定性计算
+
Evidence
+
统一 SSE 工作台
```
