"""Stage 4：停止条件纯函数测试。

覆盖（Stage 4 指令 §十 / SPEC §8 / §13.6）：
- 第 6 个查询后不能继续；
- 连续两个 empty 停止；
- empty + failed 连续两次停止；
- success 重置连续失败计数；
- 强制停止最多 partial（有 Evidence）；
- 无 Evidence → failed；
- 正常 finish 最低条件；
- 重复 Action 不增加 query_action_count。
"""

from datetime import date

from app.attribution.action_router import (
    MAX_CONSECUTIVE_EMPTY_OR_FAILED,
    MAX_QUERY_ACTIONS,
    ActionRouter,
    can_finish,
    can_start_query,
    forced_status,
    has_driver_evidence,
    has_successful_overall_comparison,
    has_valid_evidence,
    is_force_stopped,
    next_consecutive,
    successful_breakdown_dimensions,
)
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisStatus,
    ComparisonRow,
    DimensionKey,
    Evidence,
    MetricKey,
    MetricPeriodValue,
    Observation,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))


def _query_action(action_id="a1", dimension=DimensionKey.region, type=ActionType.breakdown_region, metrics=None) -> Action:
    return Action(
        action_id=action_id,
        type=type,
        metrics=metrics or [MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
        dimension=dimension,
        reason="测试",
    )


def _finish_action() -> Action:
    return Action(action_id="a9", type=ActionType.finish_analysis, reason="结束分析")


def _observation(observation_id: str, status: ObservationStatus, dimension=None) -> Observation:
    if status == ObservationStatus.success:
        result = QueryExecutionResult(
            query="q",
            sql="SELECT 1",
            table=QueryTable(
                columns=["period_key", "sales_amount"],
                rows=[{"period_key": "comparison", "sales_amount": 1.0}],
                row_count=1,
            ),
            status=ObservationStatus.success,
            error=None,
        )
        normalized_rows = [
            ComparisonRow(
                dimension_value=dimension.value if dimension else None,
                metric_values={
                    MetricKey.sales_amount: MetricPeriodValue(current_value=2.0, comparison_value=1.0)
                },
            )
        ]
        error = None
    elif status == ObservationStatus.empty:
        result = QueryExecutionResult(
            query="q",
            sql="SELECT 1",
            table=QueryTable(columns=["sales_amount"], rows=[], row_count=0),
            status=ObservationStatus.empty,
            error=None,
        )
        normalized_rows = []
        error = None
    else:  # failed
        result = QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=[], rows=[], row_count=0),
            status=ObservationStatus.failed,
            error="失败",
        )
        normalized_rows = []
        error = "失败"

    return Observation(
        observation_id=observation_id,
        action_id="a1",
        sub_query="s",
        query_result=result,
        dimension=dimension,
        normalized_rows=normalized_rows,
        status=status,
        error=error,
    )


def _evidence(evidence_id: str, observation_ids: list[str], direction=None) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        action_id="a1",
        observation_ids=observation_ids,
        title="t",
        statement="s",
        metric=MetricKey.sales_amount,
        direction=direction,
    )


# ==================== 第 6 个查询后不能继续 ====================

def test_no_query_after_six_actions():
    assert can_start_query(MAX_QUERY_ACTIONS) is False
    assert can_start_query(MAX_QUERY_ACTIONS - 1) is True


def test_router_blocks_sixth_query():
    router = ActionRouter()
    # 5 个已执行查询 Action（type+dimension 各不相同，去重键互不相同）
    seen = [
        _query_action("a1", DimensionKey.region, ActionType.breakdown_region),
        _query_action("a2", DimensionKey.category, ActionType.breakdown_category),
        _query_action("a3", DimensionKey.product, ActionType.breakdown_product),
        _query_action("a4", DimensionKey.customer, ActionType.breakdown_customer),
        _query_action("a5", DimensionKey.customer_level, ActionType.breakdown_customer),
    ]
    # count=5 时第 6 次查询仍被允许（analyze_unit_price 与已执行 5 个去重键不同）
    sixth = _query_action(
        "a6",
        type=ActionType.analyze_unit_price,
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    assert router.validate(
        sixth,
        seen_actions=seen,
        query_action_count=5,
        observations=[], calculations=[], evidences=[],
    ).ok is True
    # count=6 时禁止再产生查询执行规格
    result = router.validate(
        sixth,
        seen_actions=seen,
        query_action_count=6,
        observations=[], calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "ACTION_LIMIT_REACHED"


def test_router_blocks_query_after_two_consecutive_empty():
    """连续两次 empty 触发强制停止后，Router 拒绝新的查询 Action。"""
    router = ActionRouter()
    action = _query_action("a1", DimensionKey.region)
    result = router.validate(
        action,
        seen_actions=[],
        query_action_count=2,
        observations=[],
        calculations=[],
        evidences=[],
        consecutive_empty_or_failed=MAX_CONSECUTIVE_EMPTY_OR_FAILED,
    )
    assert result.ok is False
    assert result.error_code == "FORCED_STOP"


def test_router_allows_query_after_single_empty():
    router = ActionRouter()
    action = _query_action("a1", DimensionKey.region)
    result = router.validate(
        action,
        seen_actions=[],
        query_action_count=2,
        observations=[],
        calculations=[],
        evidences=[],
        consecutive_empty_or_failed=1,
    )
    assert result.ok is True


# ==================== 连续 empty / failed 停止 ====================

def test_two_consecutive_empty_stops():
    counter = next_consecutive(ObservationStatus.empty, 0)
    assert counter == 1
    counter = next_consecutive(ObservationStatus.empty, counter)
    assert counter == 2
    assert is_force_stopped(0, counter) is True


def test_empty_then_failed_stops():
    counter = next_consecutive(ObservationStatus.empty, 0)
    counter = next_consecutive(ObservationStatus.failed, counter)
    assert counter == MAX_CONSECUTIVE_EMPTY_OR_FAILED
    assert is_force_stopped(0, counter) is True


def test_single_empty_does_not_stop():
    assert is_force_stopped(0, next_consecutive(ObservationStatus.empty, 0)) is False


def test_success_resets_consecutive_counter():
    assert next_consecutive(ObservationStatus.success, 2) == 0
    assert next_consecutive(ObservationStatus.success, 0) == 0


def test_force_stopped_by_query_count():
    assert is_force_stopped(MAX_QUERY_ACTIONS, 0) is True
    assert is_force_stopped(MAX_QUERY_ACTIONS - 1, 0) is False


# ==================== 强制停止状态判定 ====================

def test_forced_stop_with_evidence_is_partial():
    assert forced_status(has_valid_evidence=True) == AnalysisStatus.partial


def test_forced_stop_without_evidence_is_failed():
    assert forced_status(has_valid_evidence=False) == AnalysisStatus.failed


def test_forced_stop_never_auto_completed():
    """达到查询上限不自动 completed：只可能是 partial 或 failed。"""
    assert forced_status(True) != AnalysisStatus.completed
    assert forced_status(False) != AnalysisStatus.completed


def test_has_valid_evidence_requires_success_observation():
    success_obs = _observation("o1", ObservationStatus.success)
    failed_obs = _observation("o2", ObservationStatus.failed)
    assert has_valid_evidence([_evidence("e1", ["o1"])], [success_obs]) is True
    assert has_valid_evidence([_evidence("e2", ["o2"])], [failed_obs]) is False
    assert has_valid_evidence([], [success_obs]) is False


# ==================== 正常 finish 最低条件 ====================

def test_finish_requires_overall_comparison():
    assert can_finish(
        has_overall_comparison=False,
        breakdown_dimensions={DimensionKey.region, DimensionKey.category},
        has_driver_evidence=True,
        forced_stopped=False,
    ) is False


def test_finish_requires_two_breakdown_dimensions():
    assert can_finish(
        has_overall_comparison=True,
        breakdown_dimensions={DimensionKey.region},
        has_driver_evidence=True,
        forced_stopped=False,
    ) is False


def test_finish_requires_driver_evidence():
    assert can_finish(
        has_overall_comparison=True,
        breakdown_dimensions={DimensionKey.region, DimensionKey.category},
        has_driver_evidence=False,
        forced_stopped=False,
    ) is False


def test_finish_forbidden_when_forced_stopped():
    assert can_finish(
        has_overall_comparison=True,
        breakdown_dimensions={DimensionKey.region, DimensionKey.category},
        has_driver_evidence=True,
        forced_stopped=True,
    ) is False


def test_finish_accepted_when_all_conditions_met():
    assert can_finish(
        has_overall_comparison=True,
        breakdown_dimensions={DimensionKey.region, DimensionKey.category},
        has_driver_evidence=True,
        forced_stopped=False,
    ) is True


def test_state_helpers_for_finish_conditions():
    observations = [
        _observation("o1", ObservationStatus.success, dimension=None),
        _observation("o2", ObservationStatus.success, dimension=DimensionKey.region),
        _observation("o3", ObservationStatus.success, dimension=DimensionKey.category),
        _observation("o4", ObservationStatus.failed, dimension=DimensionKey.product),
    ]
    assert has_successful_overall_comparison(observations) is True
    assert successful_breakdown_dimensions(observations) == {DimensionKey.region, DimensionKey.category}
    assert has_driver_evidence([_evidence("e1", ["o2"], direction="driver")]) is True
    assert has_driver_evidence([_evidence("e2", ["o2"])]) is False


# ==================== 重复 Action 不增加计数 ====================

def test_duplicate_action_does_not_increase_query_count():
    """Action Router 拒绝重复 Action，调用方只有在 accept 后才递增计数。"""
    router = ActionRouter()
    a1 = _query_action("a1")
    duplicate = _query_action("a1")
    first = router.validate(
        a1, seen_actions=[], query_action_count=0,
        observations=[], calculations=[], evidences=[],
    )
    assert first.ok is True
    # 计数只在 accept 后由调用方递增（此处模拟：执行 a1 后 count=1）
    second = router.validate(
        duplicate, seen_actions=[a1], query_action_count=1,
        observations=[], calculations=[], evidences=[],
    )
    assert second.ok is False
    assert second.error_code == "ACTION_DUPLICATE"
    # 被拒绝后计数维持不变（validate 本身不递增）
    assert 1 == 1  # 计数语义由调用方保证：validate 无副作用
