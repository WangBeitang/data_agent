"""Stage 3：AnalysisService 事件状态机测试。

覆盖（Stage 3 指令 §二十）：
- route 是第一类业务结果；
- stage 转换；相同连续 stage_code 去重；running -> success；
- 查询异常时 running -> failed；
- legacy result 被忽略；内部 query_result 转正式 query_result；
- empty 保留 columns；
- success -> done completed；empty -> done completed；failed -> error + done failed；
- done 只出现一次；所有正式事件都有 type + analysis_id；
- attribution -> route + NOT_IMPLEMENTED + done failed；
- error 不泄露原始异常。

QueryService 与 Intent Router 全部 mock。
"""

import asyncio

from app.api.schemas.query_schema import QuerySchema
from app.models.analysis import (
    AnalysisMode,
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


def _service(chunks, route_result=None) -> AnalysisService:
    return AnalysisService(
        query_service=_FakeQueryService(chunks),
        intent_router=_FakeRouter(route_result or _query_router()),
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
    for chunks, router in (
        (_QUERY_CHUNKS, _query_router()),
        (_EMPTY_CHUNKS, _query_router()),
        (None, _attribution_router()),
    ):
        service = _service(chunks, router)
        events = _collect(service)
        done = [e for e in events if e["type"] == "done"]
        assert len(done) == 1, f"done 出现 {len(done)} 次: {events}"


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


# ==================== attribution 过渡行为 ====================

def test_attribution_route_not_implemented_done_failed():
    events = _collect(_service(None, _attribution_router()), query="为什么2月销售额下降？")
    types = [e["type"] for e in events]
    assert types == ["route", "error", "done"]

    route = events[0]
    assert route["resolved_mode"] == "attribution"
    assert route["source"] == "rule"

    err = events[1]
    assert err["code"] == "NOT_IMPLEMENTED"
    assert err["fatal"] is True
    assert err["phase"] is None

    done = events[2]
    assert done["mode"] == "attribution"
    assert done["status"] == "failed"
    assert done["query_count"] == 0
    assert done["has_report"] is False


def test_attribution_does_not_fake_query_result():
    events = _collect(_service(None, _attribution_router()), query="为什么2月销售额下降？")
    assert not any(e["type"] == "query_result" for e in events)
    assert not any(e["type"] == "report" for e in events)


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
