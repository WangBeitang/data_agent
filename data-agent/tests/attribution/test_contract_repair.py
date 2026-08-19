"""Stage 7：硬执行契约 + 一次受控内部 SQL 修复 测试。

覆盖（Stage 7 指令 §四）：
1. SQL 语法正确但缺少契约 alias → 触发一次内部修复；
2. period 值违反 comparison/current 契约 → 触发修复；
3. 修复后契约正确 → QueryExecutionResult success；
4. 修复后仍错误 → failed，且不会无限重试；
5. result_contract=None 普通问数不进入契约修复；
6. 内部 SQL 修复不增加 Attribution query_action_count；
7. 不产生额外 attribution Action / action_start；
（8. Normalizer 不猜列测试继续通过 —— 见 test_normalizer.py；
 9. 现有全量测试继续通过 —— 整个 pytest 套件。）

所有测试使用 mock，不依赖真实 MySQL/Qdrant/ES/LLM。
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.state import DataAgentState
from app.attribution.graph import AttributionGraph
from app.attribution.normalizer import validate_contract_result
from app.attribution.planner import Planner
from app.attribution.report_generator import ReportGenerator
from app.attribution.state import initial_state
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisMode,
    AnalysisStatus,
    AttributionTarget,
    DimensionKey,
    MetricKey,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
    RequestMode,
    RouteResult,
    RouteSource,
)
from app.services.query_service import QueryService

CONTRACT = {
    "period_alias": "period_key",
    "dimension_alias": None,
    "metric_aliases": {"sales_amount": "sales_amount"},
    "period_values": ["comparison", "current"],
}
CONTRACT_DIM = {
    "period_alias": "period_key",
    "dimension_alias": "dimension_value",
    "metric_aliases": {"sales_amount": "sales_amount"},
    "period_values": ["comparison", "current"],
}

REPAIRED_SQL = "SELECT period_key, sales_amount FROM dw"
REPAIRED_COLS = ["period_key", "sales_amount"]
REPAIRED_ROWS = [
    {"period_key": "comparison", "sales_amount": 109030.5},
    {"period_key": "current", "sales_amount": 80009.0},
]


def _make_service() -> QueryService:
    return QueryService(
        dw_mysql_repo=MagicMock(),
        meta_mysql_repo=MagicMock(),
        value_es_repo=MagicMock(),
        column_qdrant_repo=MagicMock(),
        metric_qdrant_repo=MagicMock(),
        embedding_client=MagicMock(),
    )


async def _repair_ok(**kwargs):
    return REPAIRED_SQL


async def _repair_still_wrong(**kwargs):
    # 修复后 SQL 仍缺 period_key（列漂移）
    return "SELECT sales_amount FROM dw"


# ==================== 1. 缺少契约 alias → 触发一次内部修复 ====================


def test_missing_contract_alias_triggers_one_repair():
    async def fake_ainvoke(state, **kwargs):
        # 漂移输出：缺 period_key 契约列
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 100.0}, {"sales_amount": 200.0}],
        }

    service = _make_service()
    service.dw_mysql_repo.validate_sql = AsyncMock()
    service.dw_mysql_repo.execute_query = AsyncMock(return_value=(REPAIRED_COLS, REPAIRED_ROWS))

    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return await _repair_ok(**kwargs)

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q", CONTRACT, contract_repair=repair))

    assert len(calls) == 1
    assert result.status == ObservationStatus.success
    assert result.sql == REPAIRED_SQL
    assert result.table.columns == REPAIRED_COLS


# ==================== 2. period 值违反契约 → 触发修复 ====================


def test_period_value_violation_triggers_repair():
    async def fake_ainvoke(state, **kwargs):
        # 漂移输出：period_key 取值 2025-01/2025-02，不在 {comparison,current}
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["period_key", "sales_amount"],
            "result_rows": [
                {"period_key": "2025-01", "sales_amount": 100.0},
                {"period_key": "2025-02", "sales_amount": 200.0},
            ],
        }

    service = _make_service()
    service.dw_mysql_repo.validate_sql = AsyncMock()
    service.dw_mysql_repo.execute_query = AsyncMock(return_value=(REPAIRED_COLS, REPAIRED_ROWS))

    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return await _repair_ok(**kwargs)

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q", CONTRACT, contract_repair=repair))

    assert len(calls) == 1
    # 修复路径必须能看到契约失败原因
    assert "reason" in calls[0]
    assert "2025-01" in calls[0]["reason"]
    # 修复路径必须能看到原 query / 原 SQL / result_contract / 上下文
    assert calls[0]["query"] == "q"
    assert calls[0]["sql"] == "SELECT 1"
    assert calls[0]["result_contract"] == CONTRACT
    assert result.status == ObservationStatus.success


# ==================== 3. 修复后契约正确 → success（见 1/2） ====================


def test_repair_recovers_breakdown_dimension_contract():
    """维度拆解契约（含 dimension_value）缺失列 → 修复后 success。"""

    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 10.0}, {"sales_amount": 20.0}],
        }

    service = _make_service()
    service.dw_mysql_repo.validate_sql = AsyncMock()
    dim_rows = [
        {"period_key": "comparison", "dimension_value": "华东", "sales_amount": 10.0},
        {"period_key": "current", "dimension_value": "华东", "sales_amount": 20.0},
    ]
    service.dw_mysql_repo.execute_query = AsyncMock(
        return_value=(["period_key", "dimension_value", "sales_amount"], dim_rows)
    )

    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return await _repair_ok(**kwargs)

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q", CONTRACT_DIM, contract_repair=repair))

    assert len(calls) == 1
    assert result.status == ObservationStatus.success


# ==================== 4. 修复后仍错误 → failed，不无限重试 ====================


def test_repair_still_wrong_returns_failed_no_retry():
    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 100.0}, {"sales_amount": 200.0}],
        }

    service = _make_service()
    service.dw_mysql_repo.validate_sql = AsyncMock()
    # 修复后执行仍返回漂移列（缺 period_key）
    service.dw_mysql_repo.execute_query = AsyncMock(
        return_value=(["sales_amount"], [{"sales_amount": 100.0}, {"sales_amount": 200.0}])
    )

    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return await _repair_still_wrong(**kwargs)

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q", CONTRACT, contract_repair=repair))

    assert len(calls) == 1  # 只修复一次，不无限重试
    assert result.status == ObservationStatus.failed


def test_repair_returns_none_failed_safe():
    """修复函数返回 None（修复失败）→ failed，且错误信息安全（不泄露原始细节）。"""

    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 100.0}, {"sales_amount": 200.0}],
        }

    service = _make_service()

    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return None  # 修复失败

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("q", CONTRACT, contract_repair=repair))

    assert len(calls) == 1
    assert result.status == ObservationStatus.failed
    assert result.error is not None
    assert "password" not in result.error
    assert "SELECT" not in result.error


# ==================== 5. result_contract=None 普通问数不进入修复 ====================


def test_plain_query_no_contract_no_repair():
    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["月份", "销售额"],
            "result_rows": [{"月份": 1, "销售额": 109030.5}],
        }

    service = _make_service()
    calls = []
    async def repair(**kwargs):
        calls.append(kwargs)
        return await _repair_ok(**kwargs)

    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke
        result = asyncio.run(service.execute("统计2025年各月销售额", contract_repair=repair))

    assert len(calls) == 0  # 未进入契约修复
    assert result.status == ObservationStatus.success
    assert result.table.columns == ["月份", "销售额"]


# ==================== 6/7. 内部修复不增加 query_action_count / Action / action_start ====================


class _FakeLLM:
    def invoke(self, prompt):
        raise RuntimeError("report llm unavailable")


class _ScriptedPlanner(Planner):
    """复用 Stage 5 测试的可编程 Planner（不依赖真实 LLM）。"""

    def __init__(self, actions):
        super().__init__(llm=None)
        self._actions = list(actions)

    def plan(self, state_view, feedback=None, validator=None):
        for _ in range(2):
            if not self._actions:
                return None
            candidate = self._actions.pop(0)
            if candidate is None:
                return None
            if validator is not None:
                validation = validator(candidate)
                if not validation.ok:
                    continue
            return candidate
        return None

    def fallback_action(self, state_view, tried_keys):
        return None


def _repair_service(service: QueryService, repair):
    """包装真实 QueryService：在 execute 内注入 contract_repair（调用原始方法，避免递归）。"""

    original = QueryService.execute

    async def _execute(query, result_contract=None):
        return await original(service, query, result_contract, contract_repair=repair)

    service.execute = _execute  # type: ignore[assignment]
    return service


def test_internal_repair_does_not_increase_query_action_count_or_action_start():
    JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
    FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))
    target = AttributionTarget(metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN)

    # 漂移结果（缺 period_key）→ 触发内部修复；修复后统一返回含 dimension_value 的合法行
    async def fake_ainvoke(state, **kwargs):
        return {
            **state,
            "sql": "SELECT 1",
            "result_columns": ["sales_amount"],
            "result_rows": [{"sales_amount": 109030.5}, {"sales_amount": 80009.0}],
        }

    repaired_rows = [
        {"period_key": "comparison", "dimension_value": "华东", "sales_amount": 109030.5},
        {"period_key": "current", "dimension_value": "华东", "sales_amount": 80009.0},
    ]

    base = _make_service()
    base.dw_mysql_repo.validate_sql = AsyncMock()
    base.dw_mysql_repo.execute_query = AsyncMock(
        return_value=(["period_key", "dimension_value", "sales_amount"], repaired_rows)
    )

    repair_calls = []
    async def repair(**kwargs):
        repair_calls.append(kwargs)
        return REPAIRED_SQL

    service = _repair_service(base, repair)

    planner = _ScriptedPlanner([
        Action(action_id="a1", type=ActionType.compare_period, metrics=[MetricKey.sales_amount],
               current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a2", type=ActionType.breakdown_region, dimension=DimensionKey.region,
               metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a3", type=ActionType.breakdown_category, dimension=DimensionKey.category,
               metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN, reason="t"),
        Action(action_id="a9", type=ActionType.finish_analysis, reason="t"),
    ])
    state = initial_state(
        "an1", "为什么2月销售额下降？", RequestMode.auto,
        RouteResult(requested_mode=RequestMode.auto, resolved_mode=AnalysisMode.attribution,
                    source=RouteSource.rule), target,
    )

    graph = AttributionGraph(
        query_service=service, planner=planner, report_generator=ReportGenerator(llm=_FakeLLM())
    )
    events = []
    with patch("app.services.query_service.graph") as g:
        g.ainvoke = fake_ainvoke

        async def _collect():
            async for e in graph.run(state):
                events.append(e)

        asyncio.run(_collect())

    # 每个查询 Action 内部各触发一次修复（共 3 个查询 Action）
    assert len(repair_calls) == 3
    # 内部修复不增加 Attribution query_action_count
    assert state["query_action_count"] == 3
    # 不产生额外 action_start：3 查询 + 1 finish = 4
    assert [e["type"] for e in events].count("action_start") == 4
    # 整体完成（证明内部修复让所有 Observation 成功，形成完整证据链）
    assert state["status"] == AnalysisStatus.completed


# ==================== validate_contract_result 硬契约守卫直接测试 ====================


def test_validate_contract_result_conformant_is_none():
    assert validate_contract_result(
        ["period_key", "sales_amount"],
        [{"period_key": "comparison", "sales_amount": 1.0}, {"period_key": "current", "sales_amount": 2.0}],
        CONTRACT,
    ) is None


def test_validate_contract_result_none_contract_is_none():
    assert validate_contract_result(["x"], [{"x": 1}], None) is None


def test_validate_contract_result_missing_period_column():
    reason = validate_contract_result(
        ["sales_amount"], [{"sales_amount": 1.0}, {"sales_amount": 2.0}], CONTRACT
    )
    assert reason is not None
    assert "period_key" in reason


def test_validate_contract_result_missing_metric_alias():
    reason = validate_contract_result(
        ["period_key", "销售额"],
        [{"period_key": "comparison", "销售额": 1.0}, {"period_key": "current", "销售额": 2.0}],
        CONTRACT,
    )
    assert reason is not None
    assert "sales_amount" in reason


def test_validate_contract_result_bad_period_value():
    reason = validate_contract_result(
        ["period_key", "sales_amount"],
        [{"period_key": "2025-01", "sales_amount": 1.0}, {"period_key": "2025-02", "sales_amount": 2.0}],
        CONTRACT,
    )
    assert reason is not None
    assert "comparison/current" in reason


def test_validate_contract_result_empty_rows_not_failure():
    """空结果但列名符合契约 → 不触发修复（合法 empty）。"""
    assert validate_contract_result(
        ["period_key", "sales_amount"], [], CONTRACT
    ) is None


def test_validate_contract_result_non_numeric_metric():
    reason = validate_contract_result(
        ["period_key", "sales_amount"],
        [{"period_key": "comparison", "sales_amount": "一百"}, {"period_key": "current", "sales_amount": 2.0}],
        CONTRACT,
    )
    assert reason is not None
    assert "sales_amount" in reason
