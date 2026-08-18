"""Stage 3 + Stage 5：AnalysisService 事件状态机测试。

覆盖（Stage 3 指令 §二十 + Stage 5 指令 §十五）：
- route 是第一类业务结果；
- stage 转换；相同连续 stage_code 去重；running -> success；
- 查询异常时 running -> failed；
- legacy result 被忽略；内部 query_result 转正式 query_result；
- empty 保留 columns；
- success -> done completed；empty -> done completed；failed -> error + done failed；
- done 只出现一次；所有正式事件都有 type + analysis_id；
- attribution -> 真实 Graph 链路（不再 NOT_IMPLEMENTED）；
- attribution SSE 事件顺序与字段；
- attribution error 安全；done 始终收尾；
- 普通 query 模式既有测试全部保持通过。

QueryService / Intent Router / Target Parser / Attribution Graph 全部 mock。
"""

import asyncio
from datetime import date

from app.api.schemas.query_schema import QuerySchema
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisMode,
    AnalysisStatus,
    AttributionReport,
    AttributionTarget,
    MetricKey,
    Observation,
    Period,
    QueryExecutionResult,
    QueryTable,
    RequestMode,
    RouteResult,
    RouteSource,
)
from app.services.analysis_service import AnalysisService

# 典型问数内部事件流（对应现有 Graph custom 事件）
_QUERY_CHUNKS = [
    {"stage": "提取关键字", "stage_code": "query_retrieval"},
    {"stage": "召回字段", "stage_code": "query_retrieval"},
    {"stage": "合并召回", "stage_code": "query_retrieval"},
    {"stage": "生成SQL", "stage_code": "sql_generation"},
    {"stage": "校验SQL", "stage_code": "sql_validation"},
    {"stage": "执行SQL", "stage_code": "sql_execution"},
    {"query_result": {
        "sql": "SELECT 月份, SUM(销售额) AS 销售额 FROM fact_order GROUP BY 月份",
        "columns": ["月份", "销售额"],
        "rows": [{"月份": 1, "销售额": 109030.5}],
    }},
]

_EMPTY_CHUNKS = [
    {"stage": "执行SQL", "stage_code": "sql_execution"},
    {"query_result": {
        "sql": "SELECT 销售额 FROM fact_order WHERE 年份 = 2028",
        "columns": ["销售额"],
        "rows": [],
    }},
]

# 归因测试目标（为什么 2025 年 2 月销售额较 1 月明显下降）
_JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
_FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))
_ATTR_TARGET = AttributionTarget(
    metrics=[MetricKey.sales_amount],
    current_period=_FEB,
    comparison_period=_JAN,
)


class _FakeQueryService:
    """chunks 可以是普通列表或 callable（后者可模拟异常）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self, query):
        if callable(self._chunks):
            async for chunk in self._chunks(query):
                yield chunk
            return
        for chunk in self._chunks:
            yield chunk


class _FakeRouter:
    def __init__(self, route_result):
        self._route_result = route_result

    def route(self, query, requested_mode):
        return self._route_result


class _FakeTargetParser:
    def __init__(self, target):
        self._target = target

    def parse(self, query):
        return self._target


class _FakeAttributionGraph:
    """预置事件流 + 最终 state；run() 模拟 Graph 原地更新 state。"""

    def __init__(self, events, final_state):
        self._events = events
        self._final_state = final_state

    async def run(self, state):
        for key, value in self._final_state.items():
            state[key] = value
        for event in self._events:
            yield event


def _query_router() -> RouteResult:
    return RouteResult(
        requested_mode=RequestMode.auto,
        resolved_mode=AnalysisMode.query,
        source=RouteSource.rule,
        rule="统计类关键词",
    )


def _attribution_router() -> RouteResult:
    return RouteResult(
        requested_mode=RequestMode.auto,
        resolved_mode=AnalysisMode.attribution,
        source=RouteSource.rule,
        rule="原因类关键词",
    )


def _attribution_report(status=AnalysisStatus.completed) -> AttributionReport:
    return AttributionReport(
        analysis_id="an1",
        status=status,
        question_definition="分析2月销售额下降",
        core_conclusion="数据显示2月销售额较1月下降约26.62%。",
        metric_overview=[],
        drivers=[],
        offsets=[],
        evidence_ids=[],
        data_boundaries=[],
        recommendations=[],
    )


# 哨兵：区分「未注入 target」与「显式注入 None（解析失败）」
_MISSING = object()


def _service(chunks, route_result=None, target=_MISSING, graph_events=None, graph_state=None) -> AnalysisService:
    """构造 AnalysisService；attribution 场景注入 fake target parser / graph。"""
    if route_result is None or route_result.resolved_mode == AnalysisMode.query:
        return AnalysisService(
            query_service=_FakeQueryService(chunks),
            intent_router=_FakeRouter(route_result or _query_router()),
        )
    # attribution 场景：注入 fake 组件
    final_state = {
        "status": AnalysisStatus.completed,
        "query_action_count": 3,
        "report": _attribution_report(),
        "failure_reason": None,
    }
    if graph_state is not None:
        final_state.update(graph_state)
    parser_target = _ATTR_TARGET if target is _MISSING else target
    return AnalysisService(
        query_service=_FakeQueryService(chunks),
        intent_router=_FakeRouter(route_result),
        target_parser=_FakeTargetParser(parser_target),
        attribution_graph=_FakeAttributionGraph(graph_events or [], final_state),
    )


def _collect(service: AnalysisService, query: str = "统计2025年各月销售额"):
    async def _run():
        return [e async for e in service.iter_events(QuerySchema(query=query))]

    return asyncio.run(_run())


# ==================== route ====================

def test_route_is_first_business_event():
    events = _collect(_service(_QUERY_CHUNKS))
    first = events[0]
    assert first["type"] == "route"
    assert first["requested_mode"] == "auto"
    assert first["resolved_mode"] == "query"
    assert first["source"] == "rule"
    assert first["rule"] == "统计类关键词"
    assert first["analysis_id"]


# ==================== stage 状态机 ====================

def test_stage_running_conversion():
    events = _collect(_service(_QUERY_CHUNKS))
    stages = [e for e in events if e["type"] == "stage"]
    assert stages[0]["type"] == "stage"
    assert stages[0]["status"] == "running"
    assert stages[0]["stage_code"] == "query_retrieval"
    assert stages[0]["stage"]  # 中文文案保留
    assert stages[0]["analysis_id"]


def test_consecutive_same_stage_code_dedup():
    """连续多个 query_retrieval 只发一次 running。"""
    events = _collect(_service(_QUERY_CHUNKS))
    running = [e for e in events if e["type"] == "stage" and e["status"] == "running"]
    codes = [e["stage_code"] for e in running]
    assert codes == ["query_retrieval", "sql_generation", "sql_validation", "sql_execution"]


def test_stage_running_then_success_ordering():
    """进入新阶段前旧阶段 success；query_result 前当前阶段 success。"""
    events = _collect(_service(_QUERY_CHUNKS))
    stages = [e for e in events if e["type"] == "stage"]
    # 序列为 running(success) 成对出现：每个 stage_code 先 running 后 success
    assert len(stages) % 2 == 0
    for i in range(0, len(stages), 2):
        assert stages[i]["status"] == "running"
        assert stages[i + 1]["status"] == "success"
        assert stages[i]["stage_code"] == stages[i + 1]["stage_code"]
    # query_result 前必须有 sql_execution success
    idx = next(i for i, e in enumerate(events) if e["type"] == "query_result")
    assert events[idx - 1]["type"] == "stage"
    assert events[idx - 1]["stage_code"] == "sql_execution"
    assert events[idx - 1]["status"] == "success"


def test_stage_failed_on_exception():
    async def _raising(query):
        yield {"stage": "执行SQL", "stage_code": "sql_execution"}
        raise RuntimeError("boom")

    events = _collect(_service(_raising))
    failed = [e for e in events if e["type"] == "stage" and e["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["stage_code"] == "sql_execution"


# ==================== query_result ====================

def test_legacy_result_ignored():
    chunks = [
        {"stage": "执行SQL", "stage_code": "sql_execution"},
        {"result": [{"销售额": 109030.5}]},  # legacy：必须忽略
        {"query_result": {
            "sql": "SELECT 1",
            "columns": ["销售额"],
            "rows": [{"销售额": 109030.5}],
        }},
    ]
    events = _collect(_service(chunks))
    payloads = [e for e in events if e["type"] != "route"]
    assert not any(e.get("result") for e in payloads)
    # 正式事件中不出现 legacy result 键
    import json
    dumped = json.dumps(events, ensure_ascii=False)
    assert '"result":' not in dumped.replace('"query_result"', "")


def test_internal_query_result_mapped_to_formal():
    events = _collect(_service(_QUERY_CHUNKS))
    qr = [e for e in events if e["type"] == "query_result"]
    assert len(qr) == 1
    event = qr[0]
    assert event["mode"] == "query"
    assert event["action_id"] is None
    assert event["observation_id"] is None
    assert event["query"] == "统计2025年各月销售额"
    assert event["sql"] is not None
    assert event["table"]["columns"] == ["月份", "销售额"]
    assert event["table"]["row_count"] == 1
    assert event["status"] == "success"
    assert event["error"] is None
    assert event["analysis_id"]


def test_empty_query_result_keeps_columns():
    events = _collect(_service(_EMPTY_CHUNKS))
    qr = [e for e in events if e["type"] == "query_result"][0]
    assert qr["status"] == "empty"
    assert qr["error"] is None
    assert qr["table"]["columns"] == ["销售额"]
    assert qr["table"]["rows"] == []
    assert qr["table"]["row_count"] == 0


def test_no_query_result_is_internal_error():
    chunks = [{"stage": "生成SQL", "stage_code": "sql_generation"}]
    events = _collect(_service(chunks))
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "INTERNAL_ERROR"
    assert errors[0]["fatal"] is True
    done = [e for e in events if e["type"] == "done"][0]
    assert done["status"] == "failed"


# ==================== done ====================

def test_success_done_completed():
    events = _collect(_service(_QUERY_CHUNKS))
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["mode"] == "query"
    assert done[0]["status"] == "completed"
    assert done[0]["query_count"] == 1
    assert done[0]["has_report"] is False
    assert done[0]["message"] is None
    assert done[0]["analysis_id"]


def test_empty_done_completed():
    events = _collect(_service(_EMPTY_CHUNKS))
    done = [e for e in events if e["type"] == "done"][0]
    assert done["status"] == "completed"


def test_failed_error_and_done_failed():
    async def _raising(query):
        raise RuntimeError("boom")
        yield  # unreachable：使函数成为 async generator

    events = _collect(_service(_raising))
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "INTERNAL_ERROR"
    assert errors[0]["fatal"] is True
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["status"] == "failed"
    assert done[0]["query_count"] == 1


def test_done_appears_exactly_once():
    """正常 / 异常 / attribution 三条路径 done 都只出现一次。"""
    attr_events = [
        {"type": "action_start", "action": _attr_action(), "query_action_count": 0, "max_query_actions": 6},
    ]
    for chunks, router, kwargs in (
        (_QUERY_CHUNKS, _query_router(), {}),
        (_EMPTY_CHUNKS, _query_router(), {}),
        (None, _attribution_router(), {"graph_events": attr_events}),
    ):
        service = _service(chunks, router, **kwargs)
        events = _collect(service)
        done = [e for e in events if e["type"] == "done"]
        assert len(done) == 1, f"done 出现 {len(done)} 次: {events}"


# ==================== attribution 真实链路（Stage 5） ====================


def _attr_action(action_id="a1") -> Action:
    return Action(
        action_id=action_id,
        type=ActionType.compare_period,
        metrics=[MetricKey.sales_amount],
        current_period=_FEB,
        comparison_period=_JAN,
        reason="比较总体销售额",
    )


def _attr_observation(observation_id="o1", action_id="a1", dimension=None, status="success") -> Observation:
    from app.models.analysis import ComparisonRow, MetricPeriodValue

    rows = [{"period_key": "comparison", "sales_amount": 109030.5}, {"period_key": "current", "sales_amount": 80009.0}]
    table = QueryTable(columns=["period_key", "sales_amount"], rows=rows, row_count=2)
    normalized = [
        ComparisonRow(
            dimension_value=None,
            metric_values={
                MetricKey.sales_amount: MetricPeriodValue(
                    current_value=80009.0, comparison_value=109030.5
                )
            },
        )
    ]
    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        sub_query="分别统计2025年1月和2025年2月的销售额。",
        query_result=QueryExecutionResult(
            query="q",
            sql="SELECT period_key, SUM(销售额) AS sales_amount FROM ...",
            table=table,
            status=status,
            error=None if status == "success" else "错误",
        ),
        dimension=dimension,
        normalized_rows=normalized if status == "success" else [],
        status=status,
        error=None if status == "success" else "错误",
    )


def _attr_events():
    return [
        {"type": "action_start", "action": _attr_action("a1"), "query_action_count": 1, "max_query_actions": 6},
        {"type": "query_result", "observation": _attr_observation("o1", "a1"), "sub_query": "s"},
        {
            "type": "calculation",
            "action_id": "a1",
            "calculations": [],
            "evidences": [],
        },
        {
            "type": "report",
            "report": _attribution_report(),
            "evidences": [],
        },
    ]


def test_attribution_not_implemented_replaced_by_real_graph():
    """attribution 不再 NOT_IMPLEMENTED：走真实 Graph 事件流并 done(completed)。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    types = [e["type"] for e in events]
    assert types[0] == "route"
    assert types[-1] == "done"
    assert "action_start" in types
    assert "query_result" in types
    assert "calculation" in types
    assert "report" in types
    assert "error" not in types

    route = events[0]
    assert route["resolved_mode"] == "attribution"

    done = events[-1]
    assert done["mode"] == "attribution"
    assert done["status"] == "completed"
    assert done["query_count"] == 3
    assert done["has_report"] is True


def test_attribution_event_order_matches_contract():
    """归因事件顺序：route → stage(target_parsing) → stage(planning) →
    action_start → query_result → calculation → stage(report_generation)
    → report → done。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    types = [e["type"] for e in events]
    # 按 API §16 约束的顺序校验（过滤 route 后的关键顺序）
    assert types.index("action_start") < types.index("query_result") < types.index("calculation") < types.index("report") < types.index("done")
    stages = [e for e in events if e["type"] == "stage"]
    codes = [s["stage_code"] for s in stages]
    assert codes[0] == "target_parsing"
    assert "planning" in codes
    assert codes[-1] == "report_generation"
    # stage 状态成对（running -> success）
    for i in range(0, len(stages) - 1):
        if stages[i]["stage_code"] == stages[i + 1]["stage_code"]:
            assert stages[i]["status"] == "running"
            assert stages[i + 1]["status"] == "success"


def test_attribution_query_result_carries_observation():
    """attribution query_result 必须包含 action_id / observation_id / sub_query /
    sql / table / dimension / normalized_rows / status / error。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    qr = [e for e in events if e["type"] == "query_result"][0]
    assert qr["mode"] == "attribution"
    assert qr["action_id"] == "a1"
    assert qr["observation_id"] == "o1"
    assert qr["sub_query"]
    assert qr["query"]
    assert qr["sql"] is not None
    assert qr["table"]["row_count"] == 2
    assert qr["dimension"] is None
    assert isinstance(qr["normalized_rows"], list)
    assert qr["status"] == "success"
    assert qr["error"] is None


def test_attribution_action_start_after_router_validation():
    """action_start 携带已通过校验的 Action + 计数。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    action_start = [e for e in events if e["type"] == "action_start"][0]
    assert action_start["action"]["type"] == "compare_period"
    assert action_start["action"]["action_id"] == "a1"
    assert action_start["query_action_count"] == 1
    assert action_start["max_query_actions"] == 6


def test_attribution_target_parse_failed():
    """TARGET_PARSE_FAILED：error + done(failed)，不进入 Graph。"""
    events = _collect(
        _service(None, _attribution_router(), target=None),
        query="为什么销售额下降？",
    )
    types = [e["type"] for e in events]
    assert types == ["route", "stage", "stage", "error", "done"]
    err = [e for e in events if e["type"] == "error"][0]
    assert err["code"] == "TARGET_PARSE_FAILED"
    assert err["fatal"] is True
    assert err["phase"] == "target_parsing"
    done = events[-1]
    assert done["status"] == "failed"
    assert done["query_count"] == 0
    assert done["has_report"] is False


def test_attribution_partial_done_with_report():
    """partial：有报告，done.status=partial。"""
    graph_state = {
        "status": AnalysisStatus.partial,
        "query_action_count": 2,
        "report": _attribution_report(AnalysisStatus.partial),
        "failure_reason": "已触发强制停止",
    }
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events(), graph_state=graph_state),
        query="为什么2025年2月销售额较1月下降？",
    )
    done = events[-1]
    assert done["status"] == "partial"
    assert done["has_report"] is True
    assert done["message"] == "已触发强制停止"


def test_attribution_failed_no_fake_conclusion():
    """failed：无报告，error 安全 + done(failed)。"""
    graph_state = {
        "status": AnalysisStatus.failed,
        "query_action_count": 0,
        "report": None,
        "failure_reason": "无有效数据",
    }
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events(), graph_state=graph_state),
        query="为什么2025年2月销售额较1月下降？",
    )
    done = events[-1]
    assert done["status"] == "failed"
    assert done["has_report"] is False


def test_attribution_error_is_safe_and_done_always_ends():
    """Graph 抛异常：error 不泄露内部信息，done 仍收尾。"""

    class _RaisingGraph:
        async def run(self, state):
            raise RuntimeError("secret password /var/lib/mysql api-key-xxx")
            yield  # pragma: no cover

    service = AnalysisService(
        query_service=_FakeQueryService(None),
        intent_router=_FakeRouter(_attribution_router()),
        target_parser=_FakeTargetParser(_ATTR_TARGET),
        attribution_graph=_RaisingGraph(),
    )
    events = _collect(service, query="为什么2025年2月销售额较1月下降？")
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    message = errors[0]["message"]
    assert "password" not in message
    assert "/var/lib/mysql" not in message
    assert "api-key" not in message
    assert errors[0]["code"] == "INTERNAL_ERROR"
    done = events[-1]
    assert done["type"] == "done"
    assert done["status"] == "failed"


def test_attribution_done_is_last_event():
    """所有归因路径 done 都是最后一个事件。"""
    for graph_state in (
        {"status": AnalysisStatus.completed, "query_action_count": 3, "report": _attribution_report(), "failure_reason": None},
        {"status": AnalysisStatus.partial, "query_action_count": 2, "report": _attribution_report(AnalysisStatus.partial), "failure_reason": "x"},
        {"status": AnalysisStatus.failed, "query_action_count": 1, "report": None, "failure_reason": "y"},
    ):
        events = _collect(
            _service(None, _attribution_router(), graph_events=_attr_events(), graph_state=graph_state),
            query="为什么2025年2月销售额较1月下降？",
        )
        assert events[-1]["type"] == "done"


# ==================== 公共字段 ====================

def test_all_events_have_type_and_analysis_id():
    for chunks in (_QUERY_CHUNKS, _EMPTY_CHUNKS):
        events = _collect(_service(chunks))
        for event in events:
            assert "type" in event
            assert event.get("analysis_id"), f"事件缺少 analysis_id: {event}"


def test_analysis_id_fallback_uuid_when_no_req_id():
    # 脱离 HTTP 时 get_req_id() 为空 → 使用 uuid4 fallback（格式形如 8-4-4-4-12）
    from app.services import analysis_service as mod

    original = mod.get_req_id
    mod.get_req_id = lambda: ""
    try:
        events = _collect(_service(_QUERY_CHUNKS))
    finally:
        mod.get_req_id = original
    aid = events[0]["analysis_id"]
    parts = aid.split("-")
    assert len(parts) == 5


# ==================== attribution 过渡行为（已替换为真实链路） ====================

def test_attribution_route_not_implemented_removed():
    """NOT_IMPLEMENTED 已移除：attribution 不再以受控失败结束，而是执行真实链路。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    errors = [e for e in events if e["type"] == "error"]
    assert all(e["code"] != "NOT_IMPLEMENTED" for e in errors)


def test_attribution_emits_real_query_result_and_report():
    """attribution 现在产出真实的 query_result 与 report（不伪造、也不缺失）。"""
    events = _collect(
        _service(None, _attribution_router(), graph_events=_attr_events()),
        query="为什么2025年2月销售额较1月下降？",
    )
    assert any(e["type"] == "query_result" for e in events)
    assert any(e["type"] == "report" for e in events)
    report_event = [e for e in events if e["type"] == "report"][0]
    assert report_event["report"]["status"] == "completed"
    assert isinstance(report_event["evidences"], list)


# ==================== 错误安全 ====================

def test_error_hides_raw_exception():
    async def _raising(query):
        yield {"stage": "执行SQL", "stage_code": "sql_execution"}
        raise RuntimeError("secret db password /var/lib/mysql/api-key-xxxx")

    events = _collect(_service(_raising))
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    message = errors[0]["message"]
    assert "secret" not in message
    assert "password" not in message
    assert "/var/lib/mysql" not in message
    assert "api-key" not in message
    assert errors[0]["code"] == "QUERY_EXECUTION_FAILED"


def test_error_code_mapping_by_phase():
    async def _raising(query):
        yield {"stage": "生成SQL", "stage_code": "sql_generation"}
        raise RuntimeError("boom")

    events = _collect(_service(_raising))
    errors = [e for e in events if e["type"] == "error"]
    assert errors[0]["code"] == "QUERY_GENERATION_FAILED"
    assert errors[0]["phase"] == "sql_generation"


# ==================== SSE 序列化 ====================

def test_stream_outputs_sse_strings():
    import json

    service = _service(_QUERY_CHUNKS)

    async def _collect_sse():
        return [s async for s in service.stream(QuerySchema(query="统计2025年各月销售额"))]

    lines = asyncio.run(_collect_sse())
    assert lines and all(line.startswith("data: ") and line.endswith(" \n\n") for line in lines)
    first = json.loads(lines[0][6:].strip())
    assert first["type"] == "route"
    # 正式 SSE 不包含 legacy result 和内部 query_result wrapper
    joined = "".join(lines)
    assert '"result":' not in joined.replace('"query_result"', "")
    assert '"type": "query_result"' in joined or '"type":"query_result"' in joined
