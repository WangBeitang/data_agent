# 制造业销售经营归因分析系统：API 接口设计

## 0. 文档定位

本文档是以下冻结设计的下位接口文档：

1. `docs/制造业销售经营归因分析系统_概要设计总纲.md`
2. `docs/制造业销售经营归因分析系统_数据对象设计.md`

本文档冻结第一版对前端暴露的 HTTP 接口、请求参数、SSE 事件、普通问数结果、经营归因过程、报告返回、错误语义和兼容边界。

本文档不定义：

- Planner Prompt 具体内容；
- LangGraph 节点和逐文件实现；
- 数据库 Repository 接口；
- 前端组件文件划分；
- 测试命令和开发阶段。

上述内容由后续模块级实施 SPEC 冻结。

---

## 1. 接口设计原则

1. **单入口**：第一版继续只暴露 `POST /api/query`，普通问数和经营归因不拆成两个 HTTP 接口。
2. **模式显式可控**：请求增加 `mode=auto|query|attribution`，默认 `auto`。
3. **统一流式协议**：两种模式均返回 SSE，前端使用同一套读取逻辑。
4. **JSON 内判别事件类型**：SSE 继续只使用 `data:` 承载单行 JSON，通过 JSON 字段 `type` 区分事件，不引入 WebSocket。
5. **接口对象与内部状态解耦**：不直接向前端暴露 `DataAgentState`、`AttributionState` 等完整内部状态。
6. **SQL 与事实可追溯**：每次查询结果事件都包含最终 SQL、原始结果和执行状态。
7. **LLM 不作为数值来源**：变化、贡献和平均单件销售额通过 `calculation` 事件返回确定性计算结果。
8. **失败可结束**：SSE 建立后，即使分析失败，也应尽可能发送 `error` 和最终 `done`，不能仅靠连接异常表示业务失败。
9. **第一版不做服务端会话持久化**：不提供查询历史、报告查询、任务查询、重连恢复等接口。

---

## 2. 当前接口基线

当前后端已经存在：

```http
POST /api/query
Content-Type: application/json
```

当前请求：

```json
{
  "query": "统计2025年各月销售额"
}
```

当前返回已经使用：

```http
Content-Type: text/event-stream
```

现有前端通过 `fetch()` 读取 `response.body`，按空行拆分 SSE，并解析 `data:` 后的 JSON。

因此本期不改变传输方式，只扩展请求参数并统一 SSE 事件结构。

---

## 3. HTTP 接口总览

第一版只冻结一个业务接口。

| 方法 | 路径 | 用途 | 返回 |
| --- | --- | --- | --- |
| `POST` | `/api/query` | 普通问数或经营归因统一入口 | `text/event-stream` |

明确不增加：

- `/api/attribution`
- `/api/query/{id}`
- `/api/report/{id}`
- `/api/history`
- `/api/session`
- `/api/task`
- WebSocket 接口

`analysis_id` 仅用于本次流内对象关联和日志关联，不表示可再次查询的服务端持久化资源。

---

# 4. POST /api/query

## 4.1 请求

### Header

```http
Content-Type: application/json
Accept: text/event-stream
```

`Accept` 推荐前端发送，但第一版后端不要求必须显式传入。

### Body

```json
{
  "query": "为什么2025年2月销售额较1月明显下降？",
  "mode": "auto"
}
```

### QueryRequest

| 字段 | 类型 | 必填 | 默认值 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `query` | `string` | 是 | - | trim 后 1～1000 字符 | 用户问题 |
| `mode` | `string` | 否 | `auto` | `auto/query/attribution` | 请求模式 |

### mode 语义

#### auto

系统自动判断最终模式：

- 明确“统计、查询、多少、排名”等表达优先路由为 `query`；
- 明确“为什么、原因、归因、哪些因素导致、增长但、下降原因”等表达优先路由为 `attribution`；
- 规则无法明确判断时允许调用 LLM 分类。

#### query

强制普通问数。

即使问题包含“为什么”，也不进入归因 Graph。

#### attribution

强制经营归因。

系统仍需解析目标指标、本期和对比期。若问题本身无法形成有效比较目标，则通过 SSE 返回失败，不得自行虚构期间或指标。

---

## 4.2 请求校验

以下情况在 SSE 建立前直接返回 HTTP `422`：

- `query` 缺失；
- `query` trim 后为空；
- `query` 超过 1000 字符；
- `mode` 不属于 `auto/query/attribution`；
- JSON 格式非法。

FastAPI/Pydantic 的具体 422 错误报文不作为业务契约的一部分，前端只需按“请求参数非法”处理。

示例：

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/json
```

---

## 4.3 成功建立流

正常进入业务执行后：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
```

如部署链路存在代理缓冲，后端或反向代理应关闭该接口的响应缓冲。

---

# 5. SSE 传输规范

## 5.1 基本格式

每个事件使用一个 `data:` 块：

```text
data: {"type":"route","analysis_id":"an_xxx",...}

data: {"type":"stage","analysis_id":"an_xxx",...}

data: {"type":"done","analysis_id":"an_xxx",...}

```

要求：

- `data:` 后必须是合法 JSON；
- 一个事件 JSON 在协议层按单行发送；
- 两个事件之间使用 `\n\n` 分隔；
- 第一版不依赖 SSE 的 `event:`、`id:`、`retry:` 字段；
- 前端必须根据 JSON 内的 `type` 分发事件；
- 单条流内事件顺序即执行顺序，不增加额外 sequence 字段。

## 5.2 公共字段

所有业务 SSE 事件都必须包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | `string` | 事件类型 |
| `analysis_id` | `string` | 本次请求唯一标识 |

`analysis_id` 由服务端在一次请求开始时生成，例如：

```text
an_01K2...
```

具体 ID 算法不作为接口契约，只要求本次请求内唯一且可作为日志关联键。

---

# 6. SSE 事件类型

第一版固定事件：

| type | 普通问数 | 经营归因 | 说明 |
| --- | --- | --- | --- |
| `route` | 是 | 是 | 最终模式已确定 |
| `stage` | 是 | 是 | 当前执行阶段变化 |
| `action_start` | 否 | 是 | 一个归因 Action 开始 |
| `query_result` | 是 | 是 | 一次问数执行结束 |
| `calculation` | 否 | 是 | 确定性计算完成 |
| `report` | 否 | 是 | 归因报告生成完成 |
| `error` | 可有 | 可有 | 当前步骤或整个请求发生错误 |
| `done` | 是 | 是 | 整个请求结束 |

不新增单独的 `observation`、`evidence` 事件。

- Attribution 模式的 `query_result` 同时承载 Observation；
- Evidence 在 `calculation` 和最终 `report` 中返回。

---

# 7. route 事件

## 7.1 用途

路由完成后立即发送，前端据此展示当前模式。

## 7.2 Schema

```json
{
  "type": "route",
  "analysis_id": "an_001",
  "requested_mode": "auto",
  "resolved_mode": "attribution",
  "source": "rule",
  "rule": "原因类关键词"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requested_mode` | `auto/query/attribution` | 是 | 请求模式 |
| `resolved_mode` | `query/attribution` | 是 | 最终模式 |
| `source` | `forced/rule/llm` | 是 | 路由来源 |
| `rule` | `string/null` | 是 | 简短可展示规则；无则 `null` |

约束：

- `resolved_mode` 永远不能是 `auto`；
- 不返回 LLM 隐藏推理过程。

---

# 8. stage 事件

## 8.1 用途

用于前端执行时间线，不承载最终业务事实。

## 8.2 Schema

```json
{
  "type": "stage",
  "analysis_id": "an_001",
  "stage_code": "sql_generation",
  "stage": "正在生成查询 SQL",
  "status": "running"
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stage_code` | `string` | 稳定机器标识 |
| `stage` | `string` | 中文展示文案 |
| `status` | `running/success/failed` | 当前阶段状态 |

### 第一版稳定 stage_code

| stage_code | 说明 |
| --- | --- |
| `routing` | 意图路由 |
| `target_parsing` | 解析归因指标和期间 |
| `planning` | Planner 选择下一动作 |
| `query_retrieval` | 问数元数据/字段值召回 |
| `sql_generation` | SQL 生成 |
| `sql_validation` | SQL 校验或修正 |
| `sql_execution` | SQL 执行 |
| `normalization` | 查询结果归一化 |
| `calculation` | 确定性计算 |
| `evidence_building` | 构建 Evidence |
| `report_generation` | 生成归因报告 |

不是每次请求都必须经过全部阶段。

内部 Graph 可以有更细节点，但对前端只映射为上述稳定 stage_code，避免前端与内部节点名强耦合。

### 兼容说明

保留字符串字段 `stage`，因此现有前端基于：

```javascript
if (data.stage) { ... }
```

的逻辑在迁移阶段仍可识别时间线事件。

新前端必须优先使用 `type === "stage"`。

---

# 9. action_start 事件

## 9.1 用途

只在 Attribution 模式使用。

Planner 生成合法 Action、且 Action Router 校验通过后发送。

## 9.2 Schema

```json
{
  "type": "action_start",
  "analysis_id": "an_001",
  "action": {
    "action_id": "a1",
    "type": "compare_period",
    "metrics": ["sales_amount"],
    "current_period": {
      "label": "2025年2月",
      "start_date": "2025-02-01",
      "end_date": "2025-02-28"
    },
    "comparison_period": {
      "label": "2025年1月",
      "start_date": "2025-01-01",
      "end_date": "2025-01-31"
    },
    "dimension": null,
    "filters": [],
    "source_observation_ids": [],
    "reason": "先比较1月和2月总体销售额，确认下降幅度。"
  },
  "query_action_count": 1,
  "max_query_actions": 6
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `action` | `Action` | 数据对象设计冻结的 Action |
| `query_action_count` | `int` | 当前已开始的查询 Action 数 |
| `max_query_actions` | `int` | 第一版固定 6 |

规则：

- `calculate_contribution`、`finish_analysis` 不增加 `query_action_count`；
- Action 必须已经完成白名单、重复和参数校验后才能发送；
- Planner 的非法原始输出不得通过本事件暴露给前端。

---

# 10. query_result 事件

`query_result` 是本接口最重要的事实事件。

---

## 10.1 普通问数模式

普通问数不创建 Attribution Observation。

### Schema

```json
{
  "type": "query_result",
  "analysis_id": "an_101",
  "mode": "query",
  "action_id": null,
  "observation_id": null,
  "query": "统计2025年各月销售额",
  "sql": "SELECT ...",
  "table": {
    "columns": ["月份", "销售额"],
    "rows": [
      {"月份": 1, "销售额": 109030.5},
      {"月份": 2, "销售额": 80009.0},
      {"月份": 3, "销售额": 90120.0}
    ],
    "row_count": 3
  },
  "status": "success",
  "error": null
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mode` | `query` | 固定 |
| `action_id` | `null` | 普通问数无 Action |
| `observation_id` | `null` | 普通问数无 Observation |
| `query` | `string` | 实际执行问题 |
| `sql` | `string/null` | 最终 SQL |
| `table` | `QueryTable` | 原始结果表 |
| `status` | `success/empty/failed` | 执行结果 |
| `error` | `string/null` | 错误说明 |

### empty

查询成功但没有数据：

```json
{
  "type": "query_result",
  "analysis_id": "an_101",
  "mode": "query",
  "action_id": null,
  "observation_id": null,
  "query": "查询2028年销售额",
  "sql": "SELECT ...",
  "table": {
    "columns": ["销售额"],
    "rows": [],
    "row_count": 0
  },
  "status": "empty",
  "error": null
}
```

普通问数 `empty` 属于一次成功执行，不等同于系统错误。

---

## 10.2 经营归因模式

Attribution 模式的 `query_result` 对应一个 Observation。

### Schema

```json
{
  "type": "query_result",
  "analysis_id": "an_001",
  "mode": "attribution",
  "action_id": "a1",
  "observation_id": "o1",
  "sub_query": "分别查询2025年1月和2月的销售额",
  "query": "分别查询2025年1月和2月的销售额",
  "sql": "SELECT ...",
  "table": {
    "columns": ["period", "sales_amount"],
    "rows": [
      {"period": "2025-01", "sales_amount": 109030.5},
      {"period": "2025-02", "sales_amount": 80009.0}
    ],
    "row_count": 2
  },
  "dimension": null,
  "normalized_rows": [
    {
      "dimension_value": null,
      "metric_values": {
        "sales_amount": {
          "current_value": 80009.0,
          "comparison_value": 109030.5
        }
      }
    }
  ],
  "status": "success",
  "error": null
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mode` | `attribution` | 固定 |
| `action_id` | `string` | 来源 Action |
| `observation_id` | `string` | Observation ID |
| `sub_query` | `string` | Action Router 生成的受控子问题 |
| `query` | `string` | QueryService 实际执行问题，第一版通常与 `sub_query` 相同 |
| `sql` | `string/null` | 最终 SQL |
| `table` | `QueryTable` | 原始查询结果 |
| `dimension` | `DimensionKey/null` | 当前拆解维度 |
| `normalized_rows` | `ComparisonRow[]` | 归一化事实 |
| `status` | `success/empty/failed` | Observation 最终状态 |
| `error` | `string/null` | Provider 或归一化错误 |

规则：

1. Calculator 只消费 `normalized_rows`；
2. 前端 SQL 明细展示使用 `sql + table`；
3. `status=failed` 时允许 `sql=null`；
4. `status=empty` 时 `table.rows=[]`，`normalized_rows=[]`；
5. Provider 成功但归一化失败时，本事件 `status=failed`。

---

# 11. calculation 事件

## 11.1 用途

Attribution 模式中 Python 确定性计算完成后发送。

一个 Action 可以产生一个或多个 Calculation。

## 11.2 Schema

```json
{
  "type": "calculation",
  "analysis_id": "an_001",
  "action_id": "a1",
  "calculations": [
    {
      "calculation_id": "c1",
      "type": "period_change",
      "source_observation_ids": ["o1"],
      "metric": "sales_amount",
      "formula": "变化额 = 本期值 - 对比期值；变化率 = 变化额 / 对比期值",
      "current_value": 80009.0,
      "comparison_value": 109030.5,
      "delta": -29021.5,
      "change_rate": -0.2662
    }
  ],
  "evidences": [
    {
      "evidence_id": "e1",
      "action_id": "a1",
      "observation_ids": ["o1"],
      "calculation_ids": ["c1"],
      "title": "2月销售额较1月明显下降",
      "statement": "2025年2月销售额为80009.0，较1月109030.5减少29021.5，降幅约26.62%。",
      "metric": "sales_amount",
      "dimension": null,
      "member": null,
      "direction": null
    }
  ]
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `action_id` | `string` | 触发本次计算的 Action |
| `calculations` | `Calculation[]` | 新生成计算对象 |
| `evidences` | `Evidence[]` | 本次计算后新生成的 Evidence，可为空 |

为什么 Evidence 不单独设计 SSE 类型：

- Evidence 本身是 Observation + Calculation 的绑定结果；
- 前端主要在计算完成和报告阶段消费；
- 第一版避免增加额外事件类型。

### 变化率/贡献率约定

接口返回小数比例：

```json
"change_rate": -0.2662
```

前端展示：

```text
-26.62%
```

禁止后端同时返回 `-0.2662` 和字符串 `"-26.62%"` 两套口径。

---

# 12. report 事件

## 12.1 用途

Attribution 模式完成报告生成后发送。

`report` 是前端归因报告区域的最终结构化数据源。

## 12.2 Schema

```json
{
  "type": "report",
  "analysis_id": "an_001",
  "report": {
    "analysis_id": "an_001",
    "status": "completed",
    "question_definition": "分析2025年2月销售额相较1月下降的主要数据驱动因素。",
    "core_conclusion": "2月销售额较1月下降约26.62%，主要下降来自……，部分增长因素形成抵消。",
    "metric_overview": [
      {
        "metric": "sales_amount",
        "current_period_label": "2025年2月",
        "current_value": 80009.0,
        "comparison_period_label": "2025年1月",
        "comparison_value": 109030.5,
        "delta": -29021.5,
        "change_rate": -0.2662,
        "evidence_ids": ["e1"]
      }
    ],
    "drivers": [],
    "offsets": [],
    "evidence_ids": ["e1"],
    "data_boundaries": [
      "当前数据仅支持销售经营维度，不支持库存、成本、生产等原因验证。"
    ],
    "recommendations": []
  },
  "evidences": [
    {
      "evidence_id": "e1",
      "action_id": "a1",
      "observation_ids": ["o1"],
      "calculation_ids": ["c1"],
      "title": "2月销售额较1月明显下降",
      "statement": "2025年2月销售额为80009.0，较1月109030.5减少29021.5，降幅约26.62%。",
      "metric": "sales_amount",
      "dimension": null,
      "member": null,
      "direction": null
    }
  ]
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `report` | `AttributionReport` | 数据对象设计冻结的报告 |
| `evidences` | `Evidence[]` | 报告引用到的完整 Evidence 集合 |

### 为什么 report 仍返回 evidences

`AttributionReport` 内主要使用 `evidence_ids` 引用证据。

最终 `report` 事件附带完整 Evidence，可确保：

- 前端即使只保留最终事件，也能展示报告证据摘要；
- Evidence 仍通过 `observation_ids` 追溯之前的 `query_result`；
- 不需要在 Report 对象中重复 SQL 和原始结果。

### partial 报告

`partial` 仍然发送 `report`：

```json
{
  "type": "report",
  "analysis_id": "an_002",
  "report": {
    "analysis_id": "an_002",
    "status": "partial",
    "question_definition": "...",
    "core_conclusion": "现有数据只能确认……",
    "metric_overview": [],
    "drivers": [],
    "offsets": [],
    "evidence_ids": [],
    "data_boundaries": [
      "连续两次下钻未取得有效数据，无法进一步确认主要产品因素。"
    ],
    "recommendations": []
  },
  "evidences": []
}
```

`partial` 不等于接口失败，它表示有部分有效证据，但不足以形成完整归因。

---

# 13. error 事件

## 13.1 设计目标

区分：

- 当前某一步失败但系统可能继续；
- 整个请求已经无法继续。

## 13.2 Schema

```json
{
  "type": "error",
  "analysis_id": "an_001",
  "code": "QUERY_EXECUTION_FAILED",
  "message": "当前分析动作查询失败。",
  "phase": "sql_execution",
  "action_id": "a3",
  "retryable": false,
  "fatal": false
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | `string` | 是 | 稳定错误码 |
| `message` | `string` | 是 | 可展示错误说明 |
| `phase` | `string/null` | 是 | 对应 stage_code |
| `action_id` | `string/null` | 是 | 归因步骤错误时填写 |
| `retryable` | `bool` | 是 | 是否适合由客户端重新发起整个请求 |
| `fatal` | `bool` | 是 | 是否导致当前请求无法继续 |

### 第一版错误码

| code | 常见场景 | fatal 默认值 |
| --- | --- | --- |
| `ROUTE_FAILED` | 自动路由无法完成 | `true` |
| `TARGET_PARSE_FAILED` | 无法解析归因指标或比较期间 | `true` |
| `PLANNER_INVALID_ACTION` | Planner Action 非法 | `false` |
| `QUERY_GENERATION_FAILED` | 无法生成有效 SQL | 视当前归因状态 |
| `QUERY_VALIDATION_FAILED` | SQL 校验/修正失败 | 视当前归因状态 |
| `QUERY_EXECUTION_FAILED` | SQL 执行失败 | 视当前归因状态 |
| `RESULT_NORMALIZATION_FAILED` | 查询结果无法按 Action 归一化 | `false` |
| `CALCULATION_FAILED` | 确定性计算失败 | 视证据状态 |
| `REPORT_GENERATION_FAILED` | 报告生成失败 | `true` |
| `INTERNAL_ERROR` | 未分类内部异常 | `true` |

### Planner 非法 Action

概要设计已经冻结：

1. Planner 输出非法时携带校验错误重试一次；
2. 再次失败采用通用降级路线。

因此：

- 第一次 Planner Schema 校验失败可以只在后端处理，不必立即向前端发送 error；
- 降级路线成功后，无需把模型原始非法输出暴露给前端；
- 只有已经影响当前执行的失败才发送 `PLANNER_INVALID_ACTION`。

### 安全约束

`message` 禁止包含：

- 数据库密码；
- LLM API Key；
- 完整异常堆栈；
- 内部绝对文件路径；
- Prompt 原文；
- 模型隐藏推理。

详细异常写服务端日志，SSE 只返回可控错误信息。

---

# 14. done 事件

## 14.1 用途

表示本次 HTTP/SSE 业务请求已经结束。

只要 SSE 已成功建立，后端应尽量保证最后发送一次 `done`。

## 14.2 Schema

```json
{
  "type": "done",
  "analysis_id": "an_001",
  "mode": "attribution",
  "status": "completed",
  "query_count": 4,
  "has_report": true,
  "message": null
}
```

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mode` | `query/attribution` | 最终模式 |
| `status` | `completed/partial/failed` | 整体状态 |
| `query_count` | `int` | 本次请求实际调用 QueryService 的次数 |
| `has_report` | `bool` | 是否已经成功发送 `report` |
| `message` | `string/null` | 结束补充说明 |

### 普通问数

普通问数：

- 查询成功、有数据 → `completed`
- 查询成功、空数据 → `completed`
- SQL 无法完成 → `failed`
- 第一版普通问数不使用 `partial`

### 经营归因

经营归因：

- 证据充分 → `completed`
- 有部分有效证据但触发停止 → `partial`
- 无法形成有效报告 → `failed`

### done 与 HTTP 状态码

SSE 已经建立后，业务失败不能再通过 HTTP 500 改变已发送的响应状态。

因此：

```text
HTTP 200 + error event + done(status=failed)
```

是正常的流式失败表达。

---

# 15. 普通问数完整事件流

问题：

```text
统计2025年各月销售额
```

请求：

```http
POST /api/query
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "query": "统计2025年各月销售额",
  "mode": "auto"
}
```

推荐事件顺序：

```text
route
→ stage(query_retrieval)
→ stage(sql_generation)
→ stage(sql_validation)
→ stage(sql_execution)
→ query_result
→ done
```

示意：

```text
data: {"type":"route","analysis_id":"an_q1","requested_mode":"auto","resolved_mode":"query","source":"rule","rule":"统计类关键词"}

data: {"type":"stage","analysis_id":"an_q1","stage_code":"query_retrieval","stage":"正在召回相关字段和指标","status":"running"}

data: {"type":"stage","analysis_id":"an_q1","stage_code":"sql_generation","stage":"正在生成查询 SQL","status":"running"}

data: {"type":"query_result","analysis_id":"an_q1","mode":"query","action_id":null,"observation_id":null,"query":"统计2025年各月销售额","sql":"SELECT ...","table":{"columns":["月份","销售额"],"rows":[{"月份":1,"销售额":109030.5},{"月份":2,"销售额":80009.0},{"月份":3,"销售额":90120.0}],"row_count":3},"status":"success","error":null}

data: {"type":"done","analysis_id":"an_q1","mode":"query","status":"completed","query_count":1,"has_report":false,"message":null}

```

---

# 16. 归因场景一完整事件流

问题：

```text
为什么2025年2月销售额较1月明显下降？
```

请求：

```json
{
  "query": "为什么2025年2月销售额较1月明显下降？",
  "mode": "auto"
}
```

高层事件顺序：

```text
route
→ stage(target_parsing)

→ stage(planning)
→ action_start(compare_period)
→ query_result(o1)
→ calculation(c1/e1)

→ stage(planning)
→ action_start(breakdown_region)
→ query_result(o2)
→ calculation(contribution/evidence)

→ stage(planning)
→ action_start(breakdown_category)
→ query_result(o3)
→ calculation(contribution/evidence)

→ 可选重点下钻

→ stage(report_generation)
→ report
→ done
```

约束：

- 查询 Action 不超过 6；
- Action 不重复；
- 连续两次 `empty/failed` 必须停止继续查询；
- 至少两个不同维度有效拆解才允许正常 `completed`；
- 步数到上限但证据不足，应 `partial`，不是强行 `completed`。

---

# 17. 归因场景二关键事件要求

问题：

```text
为什么2025年3月销售数量大幅增长，但销售额增长有限？
```

至少应通过事件取得并可复算：

```text
2月销售数量 = 151
3月销售数量 = 322
数量变化率 ≈ 1.1325

2月销售额 = 80009.0
3月销售额 = 90120.0
销售额变化率 ≈ 0.1264

平均单件销售额：
529.86 → 279.88
```

其中平均单件销售额必须通过：

```text
query_result
→ calculation(type=unit_price)
```

产生，不能只存在于报告 LLM 的文本中。

---

# 18. 前端状态处理建议

API 契约只要求前端按 `type` 处理，不要求特定组件实现。

推荐映射：

| type | 前端用途 |
| --- | --- |
| `route` | 设置当前模式标签 |
| `stage` | 更新时间线 |
| `action_start` | 增加分析动作卡片 |
| `query_result` | 显示 SQL、表格、Observation 状态 |
| `calculation` | 更新指标卡、贡献数据、Evidence |
| `report` | 渲染最终归因报告 |
| `error` | 展示步骤错误或终止错误 |
| `done` | 结束 loading，冻结当前结果 |

重要：

前端不能以 HTTP 连接关闭作为唯一完成信号，应优先使用 `done`。

若网络异常导致连接中断且未收到 `done`，前端再将请求视为“连接异常终止”。

---

# 19. 与现有前端的兼容策略

现有前端目前识别：

```javascript
data.stage
data.error
data.result
```

新接口冻结后，前端应升级为：

```javascript
switch (data.type) {
  case "route":
  case "stage":
  case "action_start":
  case "query_result":
  case "calculation":
  case "report":
  case "error":
  case "done":
}
```

## 19.1 stage 兼容

新 `stage` 事件继续保留：

```json
"stage": "正在生成查询 SQL"
```

旧时间线逻辑可以临时继续工作。

## 19.2 result 不继续作为正式字段

现有：

```json
{"result":[...]}
```

不进入新的正式 API 契约。

新接口统一使用：

```json
"table": {
  "columns": [...],
  "rows": [...],
  "row_count": 3
}
```

原因：

- 明确列顺序；
- 空结果时仍可表达 columns；
- 与数据对象 `QueryTable` 一致；
- 归因 Observation 需要同一结果结构。

因此前后端应在同一实施阶段完成迁移，不长期维护 `result` 与 `table.rows` 两套重复字段。

---

# 20. HTTP 与 SSE 错误边界

## 20.1 SSE 建立前

使用 HTTP 状态表达：

| HTTP 状态 | 含义 |
| --- | --- |
| `422` | 请求参数校验失败 |
| `500` | 在建立流之前发生不可恢复服务异常 |

## 20.2 SSE 建立后

HTTP 已经是 200，只通过事件表达：

```text
error
→ 继续执行

或

error(fatal=true)
→ done(status=failed)
```

## 20.3 partial 不属于 HTTP 错误

```text
report(status=partial)
→ done(status=partial)
```

仍然是正常完成的一次流式请求。

---

# 21. 超时与断开

第一版不引入异步任务和结果恢复，因此：

- 用户关闭页面或主动中止 fetch：允许服务端取消本次运行；
- 不提供断点续传；
- 不提供按 `analysis_id` 恢复；
- 不提供后台继续执行后再拉取结果；
- 连接断开后用户如需结果，只能重新提交问题。

后端应在客户端取消后尽量停止尚未执行的 Planner/QueryService 调用，具体取消实现由模块级 SPEC 冻结。

---

# 22. 安全边界

接口不得返回：

- MySQL 密码；
- Elasticsearch/Qdrant 凭证；
- LLM API Key；
- 完整 Prompt；
- 模型隐藏思考过程；
- Python traceback；
- 服务器绝对路径。

允许返回：

- 用户问题；
- 受控 Action；
- 最终只读 SQL；
- 查询结果；
- 确定性计算公式和结果；
- Evidence；
- 结构化报告；
- 经过清洗的错误信息。

SQL 仍保留现有校验/修正流程，并限制为只读查询。

---

# 23. API 与内部服务边界

HTTP/SSE 层建议只依赖 `AnalysisService`：

```text
query_router
  │
  ▼
AnalysisService.stream(query, mode)
  │
  ├── Intent Router
  │
  ├── QueryService
  │
  └── Attribution Graph
```

`query_router` 不应：

- 自己判断 query/attribution；
- 自己执行 Planner；
- 自己解析 Graph 内部 state；
- 自己做变化率或贡献计算。

`QueryService` 应同时支持：

1. 普通问数的流式阶段输出；
2. 归因内部取得结构化 `QueryExecutionResult`。

具体 Python 方法名由模块级 SPEC 定义，本接口文档只冻结对外行为。

---

# 24. API 验收标准

## 24.1 请求

1. 旧请求 `{ "query": "..." }` 仍合法，默认 `mode=auto`；
2. `mode=query` 可强制普通问数；
3. `mode=attribution` 可强制归因；
4. 非法 mode 返回 HTTP 422。

## 24.2 SSE 基础

5. HTTP 成功时返回 `text/event-stream`；
6. 每个事件 JSON 都包含 `type` 和 `analysis_id`；
7. 每次正常建立的业务流最终应发送 `done`；
8. 前端无需依赖 SSE `event:` 字段。

## 24.3 普通问数

9. 第一条业务判定事件包含 `route(resolved_mode=query)`；
10. `query_result` 包含最终 SQL、`QueryTable` 和状态；
11. 空结果使用 `status=empty`，不是伪造一行 0；
12. 普通问数成功最终 `done.status=completed`；
13. 五个冻结普通问数问题结果与独立 SQL 复算一致。

## 24.4 经营归因

14. 路由为 `attribution`；
15. 每个查询 Action 开始前发送 `action_start`；
16. 每个查询 Action 对应一个 `query_result`；
17. Attribution `query_result` 包含 `observation_id` 和 `normalized_rows`；
18. 变化/贡献/平均单件销售额通过 `calculation` 返回；
19. 主要 Evidence 可关联 Observation 和 Calculation；
20. 查询次数不超过 6；
21. 最终 `report` 结构与数据对象设计一致；
22. `partial` 正常发送 report + done(partial)；
23. `failed` 不得伪造完整 report；
24. 两个冻结归因场景的核心固定数值可通过事件流复算。

## 24.5 安全

25. SSE 不包含数据库密码、API Key、Prompt 或隐藏思考；
26. `error` 不返回 traceback；
27. SQL 保持只读。

---

# 25. 第一版冻结契约

第一版前后端正式契约归纳为：

```text
POST /api/query
{
  query,
  mode=auto
}

        │
        ▼

text/event-stream

route
stage*
[action_start
 query_result
 calculation*]*
report?
error*
done
```

其中：

```text
query 模式：
route → stage* → query_result → done

attribution 模式：
route
→ stage*
→ (action_start → query_result → calculation*)*
→ report
→ done
```

核心边界：

```text
route 冻结最终模式
action_start 展示受控决策
query_result 提供 SQL 和真实查询事实
calculation 提供 Python 确定性计算
report 只汇总 Evidence 支撑的结论
done 冻结整体 completed / partial / failed 状态
```

该接口设计不增加新基础设施、不引入服务端会话持久化、不拆分多套 API，直接服务于第一版普通问数和两个固定归因场景的端到端实现。
