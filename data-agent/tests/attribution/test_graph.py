"""Stage 5：Attribution Graph 测试。

覆盖（Stage 5 指令 §十五）：
- 查询 Action 只 execute 一次；
- query_action_count 递增 / duplicate 不增加；
- success 重置 consecutive；
- empty + failed 强制停止；
- 6 次强制停止；
- completed / partial / failed；
- calculate_contribution 本地 Action 不增加查询次数；
- 自动 Calculation（period_change / contribution / unit_price / raw breakdown）；
- Planner 非法第一次重试（Router 反馈）后成功。
"""

from datetime import date

from app.attribution.graph import AttributionGraph
from app.attribution.planner import Planner
from app.attribution.report_generator import ReportGenerator
from app.attribution.state import initial_state
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisMode,
    AnalysisStatus,
    AttributionTarget,
    CalculationType,
    ContributionCalculation,
    DimensionKey,
    MetricKey,
    ObservationStatus,
    Period,
    PeriodChangeCalculation,
    QueryExecutionResult,
    QueryTable,
    RequestMode,
    RouteResult,
    RouteSource,
    UnitPriceCalculation,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))
MAR = Period(label="2025年3月", start_date=date(2025, 3, 1), end_date=date(2025, 3, 31))

TARGET_AMOUNT = AttributionTarget(metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN)
TARGET_DIVERGENCE = AttributionTarget(
    metrics=[MetricKey.sales_quantity, MetricKey.sales_amount],
    current_period=MAR,
    comparison_period=FEB,
)

# 总体期间数据（sales_amount: 2025-01 vs 2025-02）
OVERALL_AMOUNT = [
    {"period_key": "comparison", "sales_amount": 109030.5},
    {"period_key": "current", "sales_amount": 80009.0},
]
# 量额背离总体数据（2025-02 vs 2025-03）
OVERALL_DIVERGENCE = [
    {"period_key": "comparison", "sales_amount": 80009.0, "sales_quantity": 151.0},
    {"period_key": "current", "sales_amount": 90120.0, "sales_quantity": 322.0},
]
REGION_ROWS = [
    ("华东", 35000.0, 60000.0),
    ("华南", 25000.0, 30000.0),
    ("西南", 10000.0, 12030.5),
    ("华北", 10009.0, 7000.0),
]
CATEGORY_ROWS = [
    ("手机数码", 40000.0, 60000.0),
    ("家用电器", 20000.0, 25000.0),
    ("食品饮料", 20009.0, 24030.5),
]


def _action(
    action_type: ActionType,
    action_id: str = "a1",
    dimension: DimensionKey | None = None,
    metrics=None,
    current_period=FEB,
    comparison_period=JAN,
    source_observation_ids=None,
) -> Action:
    return Action(
        action_id=action_id,
        type=action_type,
        metrics=metrics or [MetricKey.sales_amount],
        current_period=current_period,
        comparison_period=comparison_period,
        dimension=dimension,
        source_observation_ids=source_observation_ids or [],
        reason="测试动作",
    )


def _result_success(rows) -> QueryExecutionResult:
    table = QueryTable(columns=list(rows[0].keys()), rows=rows, row_count=len(rows))
    return QueryExecutionResult(query="q", sql="SELECT 1", table=table, status=ObservationStatus.success, error=None)


def _result_empty() -> QueryExecutionResult:
    return QueryExecutionResult(
        query="q", sql="SELECT 1",
        table=QueryTable(columns=["period_key"], rows=[], row_count=0),
        status=ObservationStatus.empty, error=None,
    )


def _result_failed() -> QueryExecutionResult:
    return QueryExecutionResult(
        query="q", sql=None,
        table=QueryTable(columns=[], rows=[], row_count=0),
        status=ObservationStatus.failed, error="查询失败",
    )


class ScriptedQueryService:
    """按调用顺序返回预置结果；耗尽后默认 failed。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[str] = []

    async def execute(self, query, result_contract=None):
        self.calls.append(query)
        if self._results:
            return self._results.pop(0)
        return _result_failed()


class ScriptedPlanner(Planner):
    """可编程 Planner：按序输出 Action；耗尽返回 None；fallback 默认 None。"""

    def __init__(self, actions=None, fallback=None):
        super().__init__(llm=None)
        self._actions = list(actions or [])
        self._fallback = fallback

    def plan(self, state_view, feedback=None):
        return self._actions.pop(0) if self._actions else None

    def fallback_action(self, state_view, tried_keys):
        if callable(self._fallback):
            return self._fallback(state_view, tried_keys)
        return None


class _FakeLLM:
    def invoke(self, prompt):
        raise RuntimeError("report llm unavailable")


def _state(question: str, target: AttributionTarget) -> dict:
    return initial_state(
        analysis_id="an1",
        question=question,
        requested_mode=RequestMode.auto,
        route=RouteResult(
            requested_mode=RequestMode.auto,
            resolved_mode=AnalysisMode.attribution,
            source=RouteSource.rule,
        ),
        target=target,
    )


async def _run(planner, results, state):
    graph = AttributionGraph(
        query_service=ScriptedQueryService(results),
        planner=planner,
        report_generator=ReportGenerator(llm=_FakeLLM()),
    )
    events = []
    async for event in graph.run(state):
        events.append(event)
    return graph, events


# ==================== 查询 Action 只 execute 一次 / 计数 ====================


def test_query_action_executes_exactly_once_and_counts():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.breakdown_category, "a3", DimensionKey.category),
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
        _result_success(_dimension_rows(CATEGORY_ROWS)),
    ]
    graph, events = asyncio_run(_run(planner, results, state))

    qs = graph._query_service
    assert len(qs.calls) == 3  # 每个查询 Action 只 execute 一次
    assert state["query_action_count"] == 3
    assert state["status"] == AnalysisStatus.completed
    assert [e["type"] for e in events].count("query_result") == 3
    assert [e["type"] for e in events].count("action_start") == 4  # 3 查询 + 1 finish


def test_duplicate_action_does_not_increase_query_count():
    """Planner 输出重复 compare → Router 拒绝 → 重试输出 region → 通过。"""
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.compare_period, "a1_dup"),  # duplicate
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
    ]
    graph, _ = asyncio_run(_run(planner, results, state))
    # duplicate 未执行：只有 compare + region 两次查询
    assert len(graph._query_service.calls) == 2
    assert state["query_action_count"] == 2
    # 但只有 1 个 breakdown 维度，finish 被拒 → fallback 耗尽 → partial
    assert state["status"] == AnalysisStatus.partial


def test_duplicate_action_still_needs_finish_conditions():
    """duplicate 不增加次数；但仅 1 个维度拆解时 finish 拒绝 → partial。"""
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.compare_period, "a1_dup"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
    ]
    graph, _ = asyncio_run(_run(planner, results, state))
    assert state["query_action_count"] == 2  # compare + region（duplicate 未计）
    assert len(graph._query_service.calls) == 2
    # finish 被拒（只有 region 一个维度）→ fallback None → partial
    assert state["status"] == AnalysisStatus.partial


# ==================== success 重置 consecutive ====================


def test_success_resets_consecutive_counter():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_empty(),  # compare empty → consecutive=1
        _result_success(_dimension_rows(REGION_ROWS)),  # region success → 重置为 0
    ]
    graph, _ = asyncio_run(_run(planner, results, state))
    assert state["consecutive_empty_or_failed"] == 0
    # region 无总体 period_change → raw breakdown evidence → partial（有证据）
    assert state["status"] == AnalysisStatus.partial
    assert any(len(ev.calculation_ids) == 0 for ev in state["evidences"])


# ==================== empty + failed 强制停止 ====================


def test_empty_then_failed_force_stop_failed():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.breakdown_category, "a3", DimensionKey.category),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_empty(),  # consecutive=1
        _result_failed(),  # consecutive=2 → 强制停止
    ]
    graph, events = asyncio_run(_run(planner, results, state))
    assert state["consecutive_empty_or_failed"] == 2
    assert state["query_action_count"] == 2  # 第三次查询未执行
    assert len(graph._query_service.calls) == 2
    assert state["status"] == AnalysisStatus.failed  # 无有效 evidence
    assert any(e["type"] == "report" for e in events)
    assert state["report"] is not None
    assert state["report"].status == AnalysisStatus.failed


def test_force_stop_after_six_queries_is_partial():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.breakdown_category, "a3", DimensionKey.category),
        _action(ActionType.breakdown_product, "a4", DimensionKey.product),
        _action(ActionType.breakdown_customer, "a5", DimensionKey.customer),
        _action(ActionType.breakdown_customer, "a6", DimensionKey.customer_level),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
        _result_success(_dimension_rows(CATEGORY_ROWS)),
        _result_success(_dimension_rows([("P1", 5000.0, 6000.0)])),
        _result_success(_dimension_rows([("C1", 3000.0, 1000.0)])),
        _result_success(_dimension_rows([("黄金", 4000.0, 2000.0)])),
    ]
    graph, _ = asyncio_run(_run(planner, results, state))
    assert state["query_action_count"] == 6
    assert state["status"] == AnalysisStatus.partial  # 达上限不自动 completed
    assert state["report"] is not None


# ==================== completed ====================


def test_completed_with_full_evidence_chain():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1"),
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
        _action(ActionType.breakdown_category, "a3", DimensionKey.category),
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
        _result_success(_dimension_rows(CATEGORY_ROWS)),
    ]
    graph, events = asyncio_run(_run(planner, results, state))
    assert state["status"] == AnalysisStatus.completed
    assert state["query_action_count"] == 3
    assert state["report"] is not None
    assert state["report"].status == AnalysisStatus.completed
    # 总体比较证据链：compare_period → success obs → evidence 引用
    from app.attribution.action_router import has_overall_comparison_evidence

    assert has_overall_comparison_evidence(state["actions"], state["observations"], state["evidences"]) is True
    # 自动计算：period_change + 2 个 contribution
    types = [c.type for c in state["calculations"]]
    assert CalculationType.period_change in types
    assert types.count(CalculationType.contribution) == 2
    assert any(e["type"] == "report" for e in events)


# ==================== calculate_contribution 本地 Action ====================


def test_calculate_contribution_does_not_increase_query_count():
    """calculate_contribution 是本地 Action：不查询、不增加 query_action_count。"""
    compare = _action(ActionType.compare_period, "a1")
    region = _action(ActionType.breakdown_region, "a2", DimensionKey.region)
    calc_contribution = _action(
        ActionType.calculate_contribution, "a7", source_observation_ids=["o2"]
    )
    planner = ScriptedPlanner([
        compare,
        region,
        calc_contribution,
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
    ]
    graph, events = asyncio_run(_run(planner, results, state))
    # compare(1) + region(1) + calculate_contribution(本地，不增加) + finish
    assert state["query_action_count"] == 2
    assert len(graph._query_service.calls) == 2
    # calculate_contribution 执行时自动生成贡献计算（不重复：region 自动计算已生成，
    # Router 会拒绝重复 → 该 Action 未执行，不影响计数）
    assert all(e["type"] != "error" for e in events)


# ==================== 自动 Calculation：量额背离 ====================


def test_analyze_unit_price_auto_calculation():
    planner = ScriptedPlanner([
        _action(ActionType.compare_period, "a1", metrics=[MetricKey.sales_quantity, MetricKey.sales_amount],
                current_period=MAR, comparison_period=FEB),
        _action(ActionType.analyze_unit_price, "a2", metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
                current_period=MAR, comparison_period=FEB),
        _action(ActionType.breakdown_category, "a3", DimensionKey.category,
                metrics=[MetricKey.sales_quantity, MetricKey.sales_amount],
                current_period=MAR, comparison_period=FEB),
        _action(ActionType.finish_analysis, "a9"),
    ])
    state = _state("为什么 3 月数量增长但金额有限？", TARGET_DIVERGENCE)
    results = [
        _result_success(OVERALL_DIVERGENCE),
        _result_success(OVERALL_DIVERGENCE),
        _result_success(_dimension_rows(CATEGORY_ROWS, quantity="sales_quantity")),
    ]
    graph, _ = asyncio_run(_run(planner, results, state))

    unit_calcs = [c for c in state["calculations"] if isinstance(c, UnitPriceCalculation)]
    assert len(unit_calcs) == 1
    up = unit_calcs[0]
    assert up.comparison_unit_price == 529.86
    assert up.current_unit_price == 279.88

    quantity_change = [c for c in state["calculations"] if isinstance(c, PeriodChangeCalculation) and c.metric == MetricKey.sales_quantity]
    amount_change = [c for c in state["calculations"] if isinstance(c, PeriodChangeCalculation) and c.metric == MetricKey.sales_amount]
    assert quantity_change[0].comparison_value == 151.0
    assert quantity_change[0].current_value == 322.0
    assert quantity_change[0].change_rate == 1.1325
    assert amount_change[0].comparison_value == 80009.0
    assert amount_change[0].current_value == 90120.0
    assert amount_change[0].change_rate == 0.1264
    # unit_price 数值来自 Calculation（Evidence 引用 unit calc）
    up_evidence = [ev for ev in state["evidences"] if up.calculation_id in ev.calculation_ids]
    assert up_evidence and "529.86" in up_evidence[0].statement and "279.88" in up_evidence[0].statement


def test_breakdown_without_period_change_keeps_raw_evidence():
    """breakdown 先于 compare：无总体变化 → 不生成伪 Contribution，保留原始拆解事实。"""
    planner = ScriptedPlanner([
        _action(ActionType.breakdown_region, "a2", DimensionKey.region),
    ])
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [_result_success(_dimension_rows(REGION_ROWS))]
    graph, _ = asyncio_run(_run(planner, results, state))
    assert not any(isinstance(c, ContributionCalculation) for c in state["calculations"])
    raw = [ev for ev in state["evidences"] if not ev.calculation_ids]
    assert len(raw) == 1
    # statement 数值全部来自 Observation（无 Calculation 引用）
    assert "华东" in raw[0].statement
    assert state["status"] == AnalysisStatus.partial


# ==================== Planner 非法重试（Router 反馈） ====================


def test_planner_invalid_gets_router_feedback_and_retries():
    class RetryPlanner(Planner):
        def __init__(self):
            super().__init__(llm=None)
            self.calls = 0
            self.feedbacks: list[str] = []
            self.queue = [
                _action(ActionType.compare_period, "a1"),
                _action(ActionType.compare_period, "a1_dup"),  # 非法（duplicate）
                _action(ActionType.breakdown_region, "a2", DimensionKey.region),
                _action(ActionType.breakdown_category, "a3", DimensionKey.category),
                _action(ActionType.finish_analysis, "a9"),
            ]

        def plan(self, state_view, feedback=None):
            self.calls += 1
            if feedback is not None:
                self.feedbacks.append(feedback)
            return self.queue.pop(0) if self.queue else None

    planner = RetryPlanner()
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    results = [
        _result_success(OVERALL_AMOUNT),
        _result_success(_dimension_rows(REGION_ROWS)),
        _result_success(_dimension_rows(CATEGORY_ROWS)),
    ]
    graph, _ = asyncio_run(_run(planner, results, state))
    # 第一次非法（calls=2 duplicate）→ Router 反馈（calls=3 重试成功）→
    # 后续正常执行：compare + region + category + finish
    assert planner.calls == 5
    assert planner.feedbacks and "重复" in planner.feedbacks[0]  # 反馈的是可读 error_message
    assert state["query_action_count"] == 3
    assert state["status"] == AnalysisStatus.completed


def test_calculate_contribution_local_action_executes_without_query():
    """calculate_contribution 本地 Action：真正执行（生成贡献）且不增加查询次数。"""
    from app.attribution.calculator import period_change as _period_change
    from app.models.analysis import (
        ComparisonRow,
        MetricPeriodValue,
        Observation,
    )

    # 手动构造前置状态：compare + region 已执行（region 尚无贡献计算）
    state = _state("为什么 2 月销售额下降？", TARGET_AMOUNT)
    compare = _action(ActionType.compare_period, "a1")
    region = _action(ActionType.breakdown_region, "a2", DimensionKey.region)
    state["actions"] = [compare, region]

    o1 = _build_observation("o1", "a1", None, OVERALL_AMOUNT)
    o2 = _build_observation("o2", "a2", DimensionKey.region, _dimension_rows(REGION_ROWS))
    state["observations"] = [o1, o2]
    state["calculations"] = [_period_change(o1, MetricKey.sales_amount)]

    planner = ScriptedPlanner([
        _action(ActionType.calculate_contribution, "a7", source_observation_ids=["o2"]),
        _action(ActionType.finish_analysis, "a9"),
    ])
    graph, events = asyncio_run(_run(planner, [], state))
    # 无任何查询：calculate_contribution 与 finish 都是本地 Action
    assert state["query_action_count"] == 0
    assert len(graph._query_service.calls) == 0
    contributions = [c for c in state["calculations"] if isinstance(c, ContributionCalculation)]
    assert len(contributions) == 1
    assert contributions[0].dimension == DimensionKey.region
    assert any(e["type"] == "report" for e in events)


# ==================== helpers ====================


def _build_observation(observation_id: str, action_id: str, dimension, rows) -> "Observation":
    from app.models.analysis import (
        ComparisonRow,
        MetricPeriodValue,
        Observation,
    )

    by_member: dict = {}
    for row in rows:
        period = row["period_key"]
        member = row.get("dimension_value")
        metrics: dict = {k: v for k, v in row.items() if k not in ("period_key", "dimension_value")}
        entry = by_member.setdefault(member, {})
        for key, value in metrics.items():
            period_values = entry.setdefault(key, {})
            period_values[period] = value
    normalized = []
    for member in sorted(by_member, key=lambda k: (k is None, k or "")):
        metric_values = {}
        for key, period_values in by_member[member].items():
            metric_values[MetricKey(key)] = MetricPeriodValue(
                current_value=period_values.get("current", 0.0),
                comparison_value=period_values.get("comparison", 0.0),
            )
        normalized.append(ComparisonRow(dimension_value=member, metric_values=metric_values))
    table = QueryTable(columns=list(rows[0].keys()) if rows else ["period_key"], rows=rows, row_count=len(rows))
    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        sub_query="s",
        query_result=QueryExecutionResult(
            query="q", sql="SELECT 1", table=table,
            status=ObservationStatus.success, error=None,
        ),
        dimension=dimension,
        normalized_rows=normalized,
        status=ObservationStatus.success,
        error=None,
    )


def _dimension_rows(data, amount="sales_amount", quantity=None):
    """构造维度拆解原始行（period_key/dimension_value/metrics）。"""
    rows = []
    for member, cur, cmp_ in data:
        comparison_row = {"period_key": "comparison", "dimension_value": member, amount: cmp_}
        current_row = {"period_key": "current", "dimension_value": member, amount: cur}
        if quantity is not None:
            comparison_row[quantity] = cmp_
            current_row[quantity] = cur
        rows.append(comparison_row)
        rows.append(current_row)
    return rows


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
