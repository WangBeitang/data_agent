"""Stage 2 + Stage 4：公共分析数据对象测试。

覆盖：
- QueryTable：success / empty / row_count / 空结果保留 columns / Decimal 转换；
- QueryExecutionResult：success / empty / failed 及状态一致性校验；
- Stage 4：Period / FilterCondition / AttributionTarget / Action 8 类条件 /
  Observation / Calculation / Evidence。
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.analysis import (
    Action,
    ActionType,
    AttributionTarget,
    ComparisonRow,
    ContributionCalculation,
    ContributionItem,
    DimensionKey,
    Evidence,
    FactorDirection,
    FilterCondition,
    FilterOperator,
    MetricKey,
    MetricPeriodValue,
    Observation,
    ObservationStatus,
    Period,
    PeriodChangeCalculation,
    QueryExecutionResult,
    QueryTable,
    UnitPriceCalculation,
)


# ==================== QueryTable ====================

def test_query_table_success():
    table = QueryTable(
        columns=["月份", "销售额"],
        rows=[{"月份": 1, "销售额": 109030.5}],
        row_count=1,
    )
    assert table.columns == ["月份", "销售额"]
    assert table.rows == [{"月份": 1, "销售额": 109030.5}]
    assert table.row_count == 1


def test_query_table_empty_keeps_columns():
    """SQL 成功但 0 行：rows=[]、row_count=0，columns 仍保存数据库真实返回列名。"""
    table = QueryTable(columns=["销售额"], rows=[], row_count=0)
    assert table.columns == ["销售额"]
    assert table.rows == []
    assert table.row_count == 0


def test_query_table_row_count_must_match_len_rows():
    with pytest.raises(ValidationError):
        QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=2)


def test_query_table_row_count_zero_with_non_empty_rows_fails():
    with pytest.raises(ValidationError):
        QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=0)


def test_query_table_rejects_non_json_scalar():
    """数据库值归一化发生在 repository 边界，QueryTable 不接受非 JsonScalar 值。"""
    with pytest.raises(ValidationError):
        QueryTable(
            columns=["销售额"],
            rows=[{"销售额": Decimal("109030.5")}],
            row_count=1,
        )


# ==================== QueryExecutionResult ====================

def test_execution_result_success():
    result = QueryExecutionResult(
        query="统计2025年各月销售额",
        sql="SELECT ...",
        table=QueryTable(columns=["月份", "销售额"], rows=[{"月份": 1, "销售额": 109030.5}], row_count=1),
        status=ObservationStatus.success,
        error=None,
    )
    assert result.status == ObservationStatus.success
    assert result.error is None
    assert result.table.row_count == 1


def test_execution_result_empty():
    result = QueryExecutionResult(
        query="查询2028年销售额",
        sql="SELECT ...",
        table=QueryTable(columns=["销售额"], rows=[], row_count=0),
        status=ObservationStatus.empty,
        error=None,
    )
    assert result.status == ObservationStatus.empty
    assert result.error is None
    assert result.table.columns == ["销售额"]
    assert result.table.rows == []
    assert result.table.row_count == 0


def test_execution_result_failed():
    result = QueryExecutionResult(
        query="查询无法执行的问题",
        sql=None,
        table=QueryTable(columns=[], rows=[], row_count=0),
        status=ObservationStatus.failed,
        error="安全错误信息",
    )
    assert result.status == ObservationStatus.failed
    assert result.error == "安全错误信息"
    assert result.sql is None


def test_execution_result_success_requires_rows():
    """success 状态不允许 row_count == 0。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.success,
            error=None,
        )


def test_execution_result_failed_requires_error():
    """failed 状态必须有 error。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=[], rows=[], row_count=0),
            status=ObservationStatus.failed,
            error=None,
        )


def test_execution_result_success_requires_sql():
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=1),
            status=ObservationStatus.success,
            error=None,
        )


def test_execution_result_empty_requires_no_error():
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.empty,
            error="不应出现",
        )


def test_execution_result_empty_requires_sql():
    """empty = SQL 成功执行但无数据，sql 不能为 null。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=["销售额"], rows=[], row_count=0),
            status=ObservationStatus.empty,
            error=None,
        )


def test_execution_result_empty_requires_zero_rows():
    """empty 状态要求 row_count == 0。"""
    with pytest.raises(ValidationError):
        QueryExecutionResult(
            query="q",
            sql="SELECT ...",
            table=QueryTable(columns=["销售额"], rows=[{"销售额": 1}], row_count=1),
            status=ObservationStatus.empty,
            error=None,
        )


# ==================== Stage 4：Period ====================

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))


def _action(type=ActionType.compare_period, **kwargs) -> Action:
    defaults = {
        "metrics": [MetricKey.sales_amount],
        "current_period": FEB,
        "comparison_period": JAN,
        "reason": "测试动作",
    }
    defaults.update(kwargs)
    return Action(action_id="a1", type=type, **defaults)


def _result_success(rows: list[dict], columns: list[str]) -> QueryExecutionResult:
    return QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=QueryTable(columns=columns, rows=rows, row_count=len(rows)),
        status=ObservationStatus.success,
        error=None,
    )


def test_period_rejects_reversed_dates():
    with pytest.raises(ValidationError):
        Period(label="反转", start_date=date(2025, 2, 1), end_date=date(2025, 1, 31))


def test_period_accepts_same_day_and_normal_range():
    Period(label="同日", start_date=date(2025, 1, 1), end_date=date(2025, 1, 1))
    Period(label="正常", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))


# ==================== Stage 4：FilterCondition / FilterOperator ====================

def test_filter_accepts_eq_and_in_operators():
    eq = FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.eq, values=["机床"])
    in_ = FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=["机床", "刀具"])
    assert eq.operator == FilterOperator.eq
    assert in_.operator == FilterOperator.in_


def test_filter_rejects_sql_operator():
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator="gt", values=["机床"])
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator="like", values=["%机床%"])


def test_filter_rejects_empty_values():
    with pytest.raises(ValidationError):
        FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.in_, values=[])


def test_filter_rejects_unknown_dimension():
    with pytest.raises(ValidationError):
        FilterCondition(dimension="production", operator=FilterOperator.eq, values=["机床"])


def test_filter_has_no_sql_fields():
    """FilterCondition 不存在 where_sql / sql / expression 字段（白名单封闭）。"""
    with pytest.raises(ValidationError):
        FilterCondition(
            dimension=DimensionKey.category,
            operator=FilterOperator.eq,
            values=["机床"],
            where_sql="1=1",
        )


# ==================== Stage 4：AttributionTarget ====================

def test_target_requires_metrics():
    with pytest.raises(ValidationError):
        AttributionTarget(metrics=[], current_period=FEB, comparison_period=JAN)


def test_target_limits_metrics_to_three():
    with pytest.raises(ValidationError):
        AttributionTarget(
            metrics=[MetricKey.sales_amount, MetricKey.sales_quantity, MetricKey.order_count, MetricKey.avg_unit_sales_amount],
            current_period=FEB,
            comparison_period=JAN,
        )


def test_target_two_metrics_allowed():
    target = AttributionTarget(
        metrics=[MetricKey.sales_quantity, MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
    )
    assert len(target.metrics) == 2


# ==================== Stage 4：Action 8 类条件 ====================

def test_action_compare_period_conditions():
    _action(ActionType.compare_period, metrics=[MetricKey.sales_amount])
    # 缺指标
    with pytest.raises(ValidationError):
        _action(ActionType.compare_period, metrics=[])
    # 缺期间
    with pytest.raises(ValidationError):
        _action(ActionType.compare_period, current_period=None)
    # 不允许 dimension
    with pytest.raises(ValidationError):
        _action(ActionType.compare_period, dimension=DimensionKey.region)


def test_action_breakdown_region_conditions():
    _action(ActionType.breakdown_region, dimension=DimensionKey.region)
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_region, dimension=DimensionKey.category)
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_region, dimension=DimensionKey.region, metrics=[])
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_region, dimension=DimensionKey.region, comparison_period=None)


def test_action_breakdown_category_conditions():
    _action(ActionType.breakdown_category, dimension=DimensionKey.category)
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_category, dimension=DimensionKey.product)


def test_action_breakdown_product_conditions():
    _action(ActionType.breakdown_product, dimension=DimensionKey.product)
    # 允许携带 category 过滤做重点下钻
    _action(
        ActionType.breakdown_product,
        dimension=DimensionKey.product,
        filters=[FilterCondition(dimension=DimensionKey.category, operator=FilterOperator.eq, values=["负向类别"])],
    )
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_product, dimension=DimensionKey.region)


def test_action_breakdown_customer_conditions():
    _action(ActionType.breakdown_customer, dimension=DimensionKey.customer)
    _action(ActionType.breakdown_customer, dimension=DimensionKey.customer_level)
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_customer, dimension=DimensionKey.product)
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_customer, dimension=DimensionKey.customer, metrics=[])


def test_action_analyze_unit_price_conditions():
    _action(ActionType.analyze_unit_price, metrics=[MetricKey.sales_amount, MetricKey.sales_quantity])
    # 必须同时包含销售额与销售数量
    with pytest.raises(ValidationError):
        _action(ActionType.analyze_unit_price, metrics=[MetricKey.sales_amount])
    with pytest.raises(ValidationError):
        _action(ActionType.analyze_unit_price, metrics=[MetricKey.sales_quantity])
    with pytest.raises(ValidationError):
        _action(ActionType.analyze_unit_price, metrics=[MetricKey.sales_amount, MetricKey.sales_quantity], current_period=None)


def test_action_calculate_contribution_conditions():
    _action(
        ActionType.calculate_contribution,
        metrics=[MetricKey.sales_amount],
        source_observation_ids=["o1"],
    )
    # 必须引用至少 1 个 source Observation
    with pytest.raises(ValidationError):
        _action(ActionType.calculate_contribution, metrics=[MetricKey.sales_amount])
    # 只能针对 1 个目标指标
    with pytest.raises(ValidationError):
        _action(
            ActionType.calculate_contribution,
            metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
            source_observation_ids=["o1"],
        )


def test_action_finish_analysis_conditions():
    _action(ActionType.finish_analysis)
    # reason 必填（字段层 min_length=1）
    with pytest.raises(ValidationError):
        _action(ActionType.finish_analysis, reason="")


def test_action_rejects_non_whitelist_type():
    with pytest.raises(ValidationError):
        _action("breakdown_production")


def test_action_rejects_non_whitelist_dimension():
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_product, dimension="production")
    with pytest.raises(ValidationError):
        _action(ActionType.breakdown_product, dimension="inventory")


def test_action_reason_length_limits():
    _action(ActionType.compare_period, reason="一")  # 1 字
    _action(ActionType.compare_period, reason="测" * 200)  # 200 字
    with pytest.raises(ValidationError):
        _action(ActionType.compare_period, reason="测" * 201)  # 201 字


# ==================== Stage 4：Observation ====================

def test_observation_success_requires_normalized_rows():
    result = _result_success(
        [{"period_key": "comparison", "sales_amount": 1.0}, {"period_key": "current", "sales_amount": 2.0}],
        ["period_key", "sales_amount"],
    )
    with pytest.raises(ValidationError):
        Observation(
            observation_id="o1",
            action_id="a1",
            sub_query="s",
            query_result=result,
            dimension=None,
            normalized_rows=[],
            status=ObservationStatus.success,
            error=None,
        )


def test_observation_success_requires_success_query_result():
    with pytest.raises(ValidationError):
        Observation(
            observation_id="o1",
            action_id="a1",
            sub_query="s",
            query_result=QueryExecutionResult(
                query="q",
                sql=None,
                table=QueryTable(columns=[], rows=[], row_count=0),
                status=ObservationStatus.failed,
                error="失败",
            ),
            dimension=None,
            normalized_rows=[
                ComparisonRow(
                    dimension_value=None,
                    metric_values={MetricKey.sales_amount: MetricPeriodValue(current_value=1.0, comparison_value=2.0)},
                )
            ],
            status=ObservationStatus.success,
            error=None,
        )


def test_observation_empty_requires_no_rows():
    result = QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=QueryTable(columns=["销售额"], rows=[], row_count=0),
        status=ObservationStatus.empty,
        error=None,
    )
    Observation(
        observation_id="o1",
        action_id="a1",
        sub_query="s",
        query_result=result,
        dimension=None,
        normalized_rows=[],
        status=ObservationStatus.empty,
        error=None,
    )
    with pytest.raises(ValidationError):
        Observation(
            observation_id="o1",
            action_id="a1",
            sub_query="s",
            query_result=result,
            dimension=None,
            normalized_rows=[],  # empty 要求无行；此处故意传成功行
            status=ObservationStatus.success,
            error=None,
        )


def test_observation_failed_requires_error():
    result = _result_success(
        [{"period_key": "comparison", "sales_amount": 1.0}],
        ["period_key", "sales_amount"],
    )
    with pytest.raises(ValidationError):
        Observation(
            observation_id="o1",
            action_id="a1",
            sub_query="s",
            query_result=result,
            dimension=None,
            normalized_rows=[],
            status=ObservationStatus.failed,
            error=None,
        )


# ==================== Stage 4：Calculation ====================

def test_period_change_calculation_common_fields():
    calc = PeriodChangeCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.sales_amount,
        current_value=80009.0,
        comparison_value=109030.5,
        delta=-29021.5,
        change_rate=-0.2662,
    )
    assert calc.type.value == "period_change"
    assert calc.formula
    assert calc.delta == -29021.5


def test_contribution_calculation_and_item():
    calc = ContributionCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.sales_amount,
        dimension=DimensionKey.region,
        total_delta=-29021.5,
        items=[
            ContributionItem(
                member="华东",
                current_value=40000.0,
                comparison_value=50000.0,
                delta=-10000.0,
                contribution_rate=0.3446,
                direction=FactorDirection.driver,
            )
        ],
    )
    assert calc.type.value == "contribution"
    assert calc.items[0].direction == FactorDirection.driver
    assert calc.dimension == DimensionKey.region


def test_unit_price_calculation_allows_none_rates():
    calc = UnitPriceCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.avg_unit_sales_amount,
        current_sales_amount=90120.0,
        current_sales_quantity=322.0,
        current_unit_price=279.88,
        comparison_sales_amount=80009.0,
        comparison_sales_quantity=0,
        comparison_unit_price=None,
        delta=None,
        change_rate=None,
    )
    assert calc.type.value == "unit_price"
    assert calc.comparison_unit_price is None
    assert calc.delta is None


# ==================== Stage 4：Evidence ====================

def test_evidence_requires_observation_ids():
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="e1",
            action_id="a1",
            observation_ids=[],
            title="t",
            statement="s",
            metric=MetricKey.sales_amount,
        )


def test_evidence_allows_empty_calculation_ids_and_null_direction():
    evidence = Evidence(
        evidence_id="e1",
        action_id="a1",
        observation_ids=["o1"],
        title="t",
        statement="s",
        metric=MetricKey.sales_amount,
    )
    assert evidence.calculation_ids == []
    assert evidence.direction is None
