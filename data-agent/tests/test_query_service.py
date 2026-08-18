"""Stage 2：QueryService 结构化执行边界测试。

覆盖：
- QueryService.execute：success / empty（columns 不丢失）/ failed（Graph 异常、无结果）
- QueryService.stream：透传 Graph custom 事件
- QueryService.search：SSE 兼容层 + 错误安全（不暴露 str(e)）
- result_contract：传入 State / 进入 generate_sql Prompt / 固定 alias 要求
- 全部使用 mock，不依赖真实 MySQL/Qdrant/ES/LLM
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.state import DataAgentState
from app.models.analysis import ObservationStatus
from app.services.query_service import QueryService

CONTRACT = {
    "period_alias": "period_key",
    "dimension_alias": None,
    "metric_aliases": {"sales_amount": "sales_amount"},
    "period_values": ["comparison", "current"],
}


def _make_service() -> QueryService:
    return QueryService(
        dw_mysql_repo=MagicMock(),
        meta_mysql_repo=MagicMock(),
        value_es_repo=MagicMock(),
        column_qdrant_repo=MagicMock(),
        metric_qdrant_repo=MagicMock(),
        embedding_client=MagicMock(),
    )


# ==================== execute: success / empty / failed ====================

def test_execute_success_returns_structured_result():
    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT ...",
            "result_columns": ["月份", "销售额"],
            "result_rows": [{"月份": 1, "销售额": 109030.5}],
        }

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("统计2025年各月销售额"))

    assert result.query == "统计2025年各月销售额"
    assert result.sql is not None
    assert result.status == ObservationStatus.success
    assert result.error is None
    assert result.table.columns == ["月份", "销售额"]
    assert result.table.row_count == 1


def test_execute_empty_keeps_columns():
    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT ...",
            "result_columns": ["销售额"],
            "result_rows": [],
        }

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("查询2028年销售额"))

    assert result.status == ObservationStatus.empty
    assert result.error is None
    assert result.table.columns == ["销售额"]  # empty 不丢失 columns
    assert result.table.rows == []
    assert result.table.row_count == 0


def test_execute_failed_on_graph_exception_returns_safe_error():
    async def fake_ainvoke(**kwargs):
        raise RuntimeError("DB connection failed at /var/lib/mysql with password secret")

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("无法执行的问题"))

    assert result.status == ObservationStatus.failed
    assert result.sql is None
    assert result.table.row_count == 0
    assert result.error is not None
    # 安全：不暴露原始 str(e) / 路径 / 凭据
    assert "/var/lib/mysql" not in result.error
    assert "password" not in result.error
    assert "secret" not in result.error


def test_execute_failed_when_no_structured_result():
    """Graph 正常返回但 execute_sql 未写入 result_columns/result_rows → failed。"""
    async def fake_ainvoke(state, **kwargs):
        return {**state, "sql": "SELECT ...", "error": "SQL语法错误：xxx"}

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q"))

    assert result.status == ObservationStatus.failed
    assert result.error is not None
    assert result.table.row_count == 0
    # 不把 state.error 的原始内容直接暴露
    assert "xxx" not in result.error


# ==================== stream ====================

def test_stream_yields_graph_custom_chunks():
    async def fake_astream(**kwargs):
        yield {"stage": "执行SQL", "stage_code": "sql_execution"}
        yield {"result": [{"销售额": 109030.5}]}

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.astream = fake_astream

        async def _collect():
            return [c async for c in service.stream("统计2025年各月销售额")]

        chunks = asyncio.run(_collect())

    assert len(chunks) == 2
    assert chunks[0] == {"stage": "执行SQL", "stage_code": "sql_execution"}
    assert chunks[1] == {"result": [{"销售额": 109030.5}]}


# ==================== search（SSE 兼容层） ====================

def test_search_returns_sse_format_and_keeps_legacy_result():
    async def fake_astream(**kwargs):
        yield {"stage": "生成SQL", "stage_code": "sql_generation"}
        yield {"result": [{"销售额": 109030.5}]}

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.astream = fake_astream

        async def _collect():
            return [e async for e in service.search("统计2025年各月销售额")]

        events = asyncio.run(_collect())

    assert len(events) == 2
    assert events[0].startswith("data: ")
    payload = json.loads(events[0][6:])
    assert payload["stage_code"] == "sql_generation"
    assert payload["stage"] == "生成SQL"
    assert json.loads(events[1][6:])["result"] == [{"销售额": 109030.5}]


def test_search_hides_raw_exception():
    async def fake_astream(**kwargs):
        raise RuntimeError("secret db password leaked")
        yield  # unreachable：使函数成为 async generator

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.astream = fake_astream

        async def _collect():
            return [e async for e in service.search("q")]

        events = asyncio.run(_collect())

    assert len(events) == 1
    payload = json.loads(events[0][6:])
    assert "secret" not in payload["error"]
    assert "password" not in payload["error"]
    assert payload["error"]  # 有稳定安全信息


# ==================== result_contract ====================

def test_execute_passes_result_contract_into_state():
    captured = {}

    async def fake_ainvoke(state, **kwargs):
        captured["state"] = state
        return {
            **state,
            "sql": "SELECT ...",
            "result_columns": ["period_key", "sales_amount"],
            "result_rows": [{"period_key": "comparison", "sales_amount": 109030.5}],
        }

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("分别统计2025年1月和2月的销售额", result_contract=CONTRACT))

    assert captured["state"]["result_contract"] == CONTRACT
    assert result.status == ObservationStatus.success


def test_execute_without_contract_keeps_plain_query_behavior():
    captured = {}

    async def fake_ainvoke(state, **kwargs):
        captured["state"] = state
        return {
            **state,
            "sql": "SELECT ...",
            "result_columns": ["月份", "销售额"],
            "result_rows": [{"月份": 1, "销售额": 109030.5}],
        }

    service = _make_service()
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("统计2025年各月销售额"))

    assert captured["state"]["result_contract"] is None
    assert result.status == ObservationStatus.success


# ==================== generate_sql 节点：result_contract 进入 Prompt ====================

class _CaptureLLM:
    """可被 langchain 接受的 callable：捕获渲染后的完整 prompt 文本。"""

    def __init__(self):
        self.captured_text = None

    def __call__(self, input_data):
        self.captured_text = str(input_data)
        return "SELECT 1"


_TEMPLATE = "{table_infos}\n{metric_infos}\n{date_info}\n{db_info}\n{result_contract}\n{query}"


def _generate_sql_state(result_contract):
    return DataAgentState(
        query="分别统计2025年1月和2月的销售额",
        keywords=["销售额"],
        table_infos=[],
        metric_infos=[],
        date_info={"date": "2026-08-18", "weekday": "Tuesday", "quarter": "Q3"},
        db_info={"version": "8.4", "dialect": "mysql"},
        result_contract=result_contract,
    )


def test_generate_sql_passes_contract_into_prompt():
    from app.agent.nodes.generate_sql import generate_sql

    fake_llm = _CaptureLLM()

    class FakeRuntime:
        def __init__(self):
            self.context = {}
            self.events = []

        def stream_writer(self, event):
            self.events.append(event)

    with patch("app.agent.nodes.generate_sql.llm", fake_llm), \
         patch("app.agent.nodes.generate_sql.load_prompt", lambda name: _TEMPLATE):
        asyncio.run(generate_sql(_generate_sql_state(CONTRACT), FakeRuntime()))

    contract_text = fake_llm.captured_text
    # 契约文本进入 Prompt，包含固定 alias 要求
    assert contract_text is not None
    assert "period_key" in contract_text
    assert "sales_amount" in contract_text
    assert "comparison" in contract_text
    assert "current" in contract_text


def test_generate_sql_without_contract_keeps_plain_behavior():
    from app.agent.nodes.generate_sql import generate_sql

    fake_llm = _CaptureLLM()

    class FakeRuntime:
        def __init__(self):
            self.context = {}
            self.events = []

        def stream_writer(self, event):
            self.events.append(event)

    with patch("app.agent.nodes.generate_sql.llm", fake_llm), \
         patch("app.agent.nodes.generate_sql.load_prompt", lambda name: _TEMPLATE):
        asyncio.run(generate_sql(_generate_sql_state(None), FakeRuntime()))

    contract_text = fake_llm.captured_text
    # 普通问数：不约束输出列名
    assert contract_text is not None
    assert "无" in contract_text


def test_generate_sql_prompt_file_contains_contract_rules():
    from pathlib import Path

    prompt_path = Path(__file__).parents[1] / "prompts" / "generate_sql.prompt"
    content = prompt_path.read_text("utf-8")
    assert "{result_contract}" in content
    assert "period_values" in content
    assert "不得增加、删除或修改契约输出列" in content
    # Stage 1 GROUP BY 规则必须保留
    assert "GROUP BY" in content
    assert "region_name" in content
