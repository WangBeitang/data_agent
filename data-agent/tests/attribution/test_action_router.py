"""Stage 4：Action Router 测试。

覆盖（Stage 4 指令 §十一 / SPEC §13.3）：
- 6 类查询 Action / 2 类本地 Action；
- dimension 白名单；
- filter 白名单（operator / values / 不允许 SQL 表达式字段）；
- 重复 Action 与规范化后重复 Action；
- query_action_count=6 上限；
- sub_query 确定性模板；
- result_contract 生成；
- premature finish；
- calculate_contribution 只能引用成功 breakdown Observation 且不得重复计算。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.attribution.action_router import (
    ActionRouter,
    MAX_QUERY_ACTIONS,
    QUERY_ACTION_TYPES,
    action_dedup_key,
    build_result_contract,
    build_sub_query,
    can_start_query,
    is_duplicate,
    is_local_action,
    is_query_action,
)
from app.models.analysis import (
    Action,
    ActionType,
    CalculationType,
    ComparisonRow,
    ContributionCalculation,
    DimensionKey,
    Evidence,
    FilterCondition,
    FilterOperator,
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
MAR = Period(label="2025年3月", start_date=date(2025, 3, 1), end_date=date(2025, 3, 31))


def _action(action_id="a1", type=ActionType.compare_period, **kwargs) -> Action:
    defaults = {
        "metrics": [MetricKey.sales_amount],
        "current_period": FEB,
        "comparison_period": JAN,
        "reason": "测试动作",
    }
    defaults.update(kwargs)
    return Action(action_id=action_id, type=type, **defaults)


def _breakdown_observation(
    observation_id: str,
    dimension: DimensionKey,
    rows: list[dict],
) -> Observation:
    table = QueryTable(columns=list(rows[0].keys()), rows=rows, row_count=len(rows))
    result = QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=table,
        status=ObservationStatus.success,
        error=None,
    )
    return Observation(
        observation_id=observation_id,
        action_id="a1",
        sub_query="s",
        query_result=result,
        dimension=dimension,
        normalized_rows=[
            ComparisonRow(
                dimension_value="成员A",
                metric_values={
                    MetricKey.sales_amount: MetricPeriodValue(current_value=2.0, comparison_value=1.0)
                },
            )
        ],
        status=ObservationStatus.success,
        error=None,
    )


def _success_observation(
    observation_id: str,
    dimension: DimensionKey | None = None,
    action_id: str = "a1",
) -> Observation:
    rows = [{"period_key": "comparison", "sales_amount": 1.0}, {"period_key": "current", "sales_amount": 2.0}]
    if dimension is not None:
        for row in rows:
            row["dimension_value"] = "成员A"
    table = QueryTable(columns=list(rows[0].keys()), rows=rows, row_count=len(rows))
    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        sub_query="s",
        query_result=QueryExecutionResult(
            query="q", sql="SELECT ...", table=table,
            status=ObservationStatus.success, error=None,
        ),
        dimension=dimension,
        normalized_rows=[
            ComparisonRow(
                dimension_value="成员A" if dimension is not None else None,
                metric_values={
                    MetricKey.sales_amount: MetricPeriodValue(current_value=2.0, comparison_value=1.0)
                },
            )
        ],
        status=ObservationStatus.success,
        error=None,
    )


# ==================== 查询 / 本地 Action 判断 ====================

def test_six_query_action_types():
    for action_type in (
        ActionType.compare_period,
        ActionType.breakdown_region,
        ActionType.breakdown_category,
        ActionType.breakdown_product,
        ActionType.breakdown_customer,
        ActionType.analyze_unit_price,
    ):
        assert is_query_action(action_type) is True
        assert action_type in QUERY_ACTION_TYPES
        assert is_local_action(action_type) is False


def test_two_local_action_types():
    assert is_query_action(ActionType.calculate_contribution) is False
    assert is_query_action(ActionType.finish_analysis) is False
    assert is_local_action(ActionType.calculate_contribution) is True
    assert is_local_action(ActionType.finish_analysis) is True


# ==================== dimension 白名单 ====================

def test_action_rejects_non_whitelist_dimension():
    with pytest.raises(ValidationError):
        _action(type=ActionType.breakdown_product, dimension="production")


def test_breakdown_region_requires_region_dimension():
    with pytest.raises(ValidationError):
        _action(type=ActionType.breakdown_region, dimension=DimensionKey.category)


def test_breakdown_customer_only_customer_or_customer_level():
    with pytest.raises(ValidationError):
        _action(type=ActionType.breakdown_customer, dimension=DimensionKey.product)
    _action(type=ActionType.breakdown_customer, dimension=DimensionKey.customer)
    _action(type=ActionType.breakdown_customer, dimension=DimensionKey.customer_level)


def test_compare_period_forbids_dimension():
    with pytest.raises(ValidationError):
        _action(type=ActionType.compare_period, dimension=DimensionKey.region)


# ==================== filter 白名单 ====================

def test_filter_rejects_non_whitelist_operator():
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator="gt", values=["类别A"])


def test_filter_requires_values():
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.eq, values=[])
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=[""])


def test_filter_accepts_eq_and_in():
    eq = FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.eq, values=["类别A"])
    in_ = FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=["类别A", "类别B"])
    assert eq.operator == FilterOperator.eq
    assert in_.operator == FilterOperator.in_


def test_action_rejects_sql_expression_fields():
    """Action 不允许携带任意 SQL：where_sql / sql / 表达式字段不存在。"""
    with pytest.raises(ValidationError):
        _action(
            type=ActionType.breakdown_product,
            dimension=DimensionKey.product,
            filters=[{"dimension": "category", "operator": "eq", "values": ["A"], "where_sql": "1=1"}],
        )


# ==================== 去重 ====================

def test_duplicate_action_detected():
    a1 = _action(action_id="a1", type=ActionType.breakdown_region, dimension=DimensionKey.region)
    a2 = _action(action_id="a2", type=ActionType.breakdown_region, dimension=DimensionKey.region)
    assert is_duplicate(a2, [a1]) is True


def test_duplicate_ignores_action_id_and_reason():
    a1 = _action(action_id="a1", reason="原因一")
    a2 = _action(action_id="x99", reason="完全不同的理由文本")
    assert action_dedup_key(a1) == action_dedup_key(a2)
    assert is_duplicate(a2, [a1]) is True


def test_duplicate_normalizes_metric_order():
    a1 = _action(type=ActionType.compare_period, metrics=[MetricKey.sales_amount, MetricKey.sales_quantity])
    a2 = _action(type=ActionType.compare_period, metrics=[MetricKey.sales_quantity, MetricKey.sales_amount])
    assert action_dedup_key(a1) == action_dedup_key(a2)
    assert is_duplicate(a2, [a1]) is True


def test_duplicate_normalizes_in_filter_values_order():
    f1 = [FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=["B", "A"])]
    f2 = [FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=["A", "B"])]
    a1 = _action(type=ActionType.breakdown_product, dimension=DimensionKey.product, filters=f1)
    a2 = _action(type=ActionType.breakdown_product, dimension=DimensionKey.product, filters=f2)
    assert action_dedup_key(a1) == action_dedup_key(a2)
    assert is_duplicate(a2, [a1]) is True


def test_different_metrics_not_duplicate():
    a1 = _action(metrics=[MetricKey.sales_amount])
    a2 = _action(metrics=[MetricKey.sales_quantity])
    assert is_duplicate(a2, [a1]) is False


def test_different_periods_not_duplicate():
    a1 = _action(current_period=FEB)
    a2 = _action(current_period=MAR)
    assert is_duplicate(a2, [a1]) is False


# ==================== query_action_count 上限 ====================

def test_can_start_query_below_limit():
    assert can_start_query(0) is True
    assert can_start_query(5) is True
    assert can_start_query(6) is False
    assert can_start_query(6, max_query_actions=6) is False


def test_router_rejects_query_action_at_limit():
    router = ActionRouter()
    action = _action(type=ActionType.breakdown_region, dimension=DimensionKey.region)
    result = router.validate(
        action,
        seen_actions=[],
        query_action_count=MAX_QUERY_ACTIONS,
        observations=[],
        calculations=[],
        evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "ACTION_LIMIT_REACHED"


def test_router_rejects_duplicate_without_incrementing_count():
    """重复 Action 被拒绝，不增加 query_action_count。"""
    router = ActionRouter()
    a1 = _action(action_id="a1", type=ActionType.breakdown_region, dimension=DimensionKey.region)
    a2 = _action(action_id="a2", type=ActionType.breakdown_region, dimension=DimensionKey.region)
    assert router.validate(
        a1, seen_actions=[], query_action_count=0,
        observations=[], calculations=[], evidences=[],
    ).ok is True
    result = router.validate(
        a2, seen_actions=[a1], query_action_count=0,
        observations=[], calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "ACTION_DUPLICATE"


def test_local_actions_do_not_hit_query_limit():
    """calculate_contribution / finish_analysis 不受查询上限约束（但受各自规则约束）。"""
    router = ActionRouter()
    finish = _action(type=ActionType.finish_analysis)
    result = router.validate(
        finish,
        seen_actions=[],
        query_action_count=6,
        observations=[],
        calculations=[],
        evidences=[],
    )
    # 无查询 Action 不受限，但 premature finish 仍被拒绝
    assert result.ok is False
    assert result.error_code == "PREMATURE_FINISH"


# ==================== sub_query ====================

def test_sub_query_compare_period():
    action = _action(type=ActionType.compare_period, metrics=[MetricKey.sales_amount])
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月的销售额。"


def test_sub_query_breakdown_region():
    action = _action(type=ActionType.breakdown_region, dimension=DimensionKey.region, metrics=[MetricKey.sales_amount])
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月各销售区域的销售额。"


def test_sub_query_breakdown_category():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category, metrics=[MetricKey.sales_amount])
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月各产品类别的销售额。"


def test_sub_query_breakdown_product_with_filters():
    action = _action(
        type=ActionType.breakdown_product,
        dimension=DimensionKey.product,
        metrics=[MetricKey.sales_amount],
        filters=[FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.eq, values=["负向类别"])],
    )
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月各产品的销售额，并限定产品类别为「负向类别」。"


def test_sub_query_breakdown_product_without_filters():
    action = _action(type=ActionType.breakdown_product, dimension=DimensionKey.product, metrics=[MetricKey.sales_amount])
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月各产品的销售额。"


def test_sub_query_breakdown_customer():
    action = _action(
        type=ActionType.breakdown_customer,
        dimension=DimensionKey.customer_level,
        metrics=[MetricKey.sales_amount],
    )
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月各客户/客户等级的销售额。"


def test_sub_query_analyze_unit_price():
    action = _action(
        type=ActionType.analyze_unit_price,
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月的销售额和销售数量。"


def test_sub_query_multi_metric():
    action = _action(metrics=[MetricKey.sales_amount, MetricKey.sales_quantity])
    assert build_sub_query(action) == "分别统计2025年1月和2025年2月的销售额、销售数量。"


def test_sub_query_forbidden_for_local_action():
    with pytest.raises(ValueError):
        build_sub_query(_action(type=ActionType.finish_analysis))


# ==================== result_contract ====================

def test_contract_compare_period():
    action = _action(type=ActionType.compare_period, metrics=[MetricKey.sales_amount])
    assert build_result_contract(action) == {
        "period_alias": "period_key",
        "dimension_alias": None,
        "metric_aliases": {"sales_amount": "sales_amount"},
        "period_values": ["comparison", "current"],
    }


def test_contract_breakdown():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category, metrics=[MetricKey.sales_amount])
    contract = build_result_contract(action)
    assert contract["dimension_alias"] == "dimension_value"
    assert contract["metric_aliases"] == {"sales_amount": "sales_amount"}


def test_contract_multi_metric():
    action = _action(metrics=[MetricKey.sales_amount, MetricKey.sales_quantity])
    contract = build_result_contract(action)
    assert contract["metric_aliases"] == {"sales_amount": "sales_amount", "sales_quantity": "sales_quantity"}


def test_contract_analyze_unit_price_includes_quantity():
    action = _action(
        type=ActionType.analyze_unit_price,
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    contract = build_result_contract(action)
    assert "sales_amount" in contract["metric_aliases"]
    assert "sales_quantity" in contract["metric_aliases"]


def test_execution_spec_query_action():
    router = ActionRouter()
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    spec = router.execution_spec(action)
    assert spec.sub_query.startswith("分别统计")
    assert spec.result_contract["dimension_alias"] == "dimension_value"


def test_execution_spec_forbidden_for_local_action():
    router = ActionRouter()
    with pytest.raises(ValueError):
        router.execution_spec(_action(type=ActionType.calculate_contribution, metrics=[MetricKey.sales_amount], source_observation_ids=["o1"]))


# ==================== premature finish ====================

def test_finish_rejected_without_evidence():
    router = ActionRouter()
    finish = _action(type=ActionType.finish_analysis)
    result = router.validate(
        finish, seen_actions=[], query_action_count=1,
        observations=[], calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "PREMATURE_FINISH"


def test_finish_rejected_with_only_overall_comparison():
    router = ActionRouter()
    finish = _action(type=ActionType.finish_analysis)
    observations = [_success_observation("o1", dimension=None)]
    result = router.validate(
        finish, seen_actions=[], query_action_count=1,
        observations=observations, calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "PREMATURE_FINISH"


def test_finish_rejected_without_driver_evidence():
    router = ActionRouter()
    finish = _action(type=ActionType.finish_analysis)
    observations = [
        _success_observation("o1", dimension=None),
        _success_observation("o2", dimension=DimensionKey.region),
        _success_observation("o3", dimension=DimensionKey.category),
    ]
    evidences = [
        Evidence(
            evidence_id="e1", action_id="a1", observation_ids=["o2"],
            title="t", statement="s", metric=MetricKey.sales_amount,
            dimension=DimensionKey.region, member="成员A",
        )
    ]
    result = router.validate(
        finish, seen_actions=[], query_action_count=1,
        observations=observations, calculations=[], evidences=evidences,
    )
    assert result.ok is False
    assert result.error_code == "PREMATURE_FINISH"


def test_finish_accepted_when_conditions_met():
    router = ActionRouter()
    finish = _action(type=ActionType.finish_analysis)
    compare_action = _action(action_id="a1", type=ActionType.compare_period)
    observations = [
        _success_observation("o1", dimension=None, action_id="a1"),  # compare_period 结果
        _success_observation("o2", dimension=DimensionKey.region, action_id="a2"),
        _success_observation("o3", dimension=DimensionKey.category, action_id="a3"),
    ]
    evidences = [
        Evidence(
            evidence_id="e0", action_id="a1", observation_ids=["o1"],
            title="总体比较", statement="s", metric=MetricKey.sales_amount,
        ),
        Evidence(
            evidence_id="e1", action_id="a2", observation_ids=["o2"],
            title="t", statement="s", metric=MetricKey.sales_amount,
            dimension=DimensionKey.region, member="成员A", direction="driver",
        ),
    ]
    result = router.validate(
        finish, seen_actions=[compare_action], query_action_count=1,
        observations=observations, calculations=[], evidences=evidences,
    )
    assert result.ok is True


# ==================== calculate_contribution 来源限制 ====================

def test_contribution_requires_source():
    router = ActionRouter()
    action = _action(
        type=ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["missing"],
    )
    result = router.validate(
        action, seen_actions=[], query_action_count=0,
        observations=[], calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "INVALID_CONTRIBUTION_SOURCE"


def test_contribution_requires_success_breakdown_observation():
    router = ActionRouter()
    failed_obs = _breakdown_observation("o_f", DimensionKey.region, [{"period_key": "comparison", "sales_amount": 1.0}])
    failed_obs.status = ObservationStatus.failed
    failed_obs.error = "失败"
    action = _action(
        type=ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["o_f"],
    )
    result = router.validate(
        action, seen_actions=[], query_action_count=0,
        observations=[failed_obs], calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "INVALID_CONTRIBUTION_SOURCE"


def test_contribution_rejects_overall_comparison_observation():
    router = ActionRouter()
    action = _action(
        type=ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["o1"],
    )
    result = router.validate(
        action, seen_actions=[], query_action_count=0,
        observations=[_success_observation("o1", dimension=None)],
        calculations=[], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "INVALID_CONTRIBUTION_SOURCE"


def test_contribution_rejects_duplicate_calculation():
    router = ActionRouter()
    action = _action(
        type=ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["o2"],
    )
    existing = ContributionCalculation(
        calculation_id="c1",
        source_observation_ids=["o2"],
        metric=MetricKey.sales_amount,
        formula="f",
        dimension=DimensionKey.region,
        total_delta=-29021.5,
        items=[],
    )
    result = router.validate(
        action, seen_actions=[], query_action_count=0,
        observations=[_success_observation("o2", dimension=DimensionKey.region)],
        calculations=[existing], evidences=[],
    )
    assert result.ok is False
    assert result.error_code == "CONTRIBUTION_ALREADY_CALCULATED"


def test_contribution_accepted_when_valid():
    router = ActionRouter()
    action = _action(
        type=ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["o2"],
    )
    result = router.validate(
        action, seen_actions=[], query_action_count=0,
        observations=[_success_observation("o2", dimension=DimensionKey.region)],
        calculations=[], evidences=[],
    )
    assert result.ok is True
