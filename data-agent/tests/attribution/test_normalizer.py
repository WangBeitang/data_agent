"""Stage 4：Normalizer 测试。

覆盖（Stage 4 指令 §十一 / SPEC §13.4）：
- compare_period / region / category / product / customer / customer_level；
- 多 metric；
- 某 member 仅一个期间出现时另一期间补 0；
- empty / provider failed；
- 缺 period_key / 缺 dimension_value / 缺 metric alias / 非法 period value；
- 不接受自由列名猜测。
"""

from datetime import date

from app.attribution.action_router import build_result_contract, build_sub_query
from app.attribution.normalizer import Normalizer
from app.models.analysis import (
    Action,
    ActionType,
    DimensionKey,
    MetricKey,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))


def _action(type=ActionType.compare_period, dimension=None, metrics=None) -> Action:
    return Action(
        action_id="a1",
        type=type,
        metrics=metrics or [MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
        dimension=dimension,
        reason="测试",
    )


def _result(columns, rows, status=ObservationStatus.success, error=None) -> QueryExecutionResult:
    return QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=QueryTable(columns=columns, rows=rows, row_count=len(rows)),
        status=status,
        error=error,
    )


def _normalize(action, result, contract=None):
    contract = contract if contract is not None else build_result_contract(action)
    return Normalizer.normalize("o1", action, build_sub_query(action), result, contract)


# ==================== 各 Action 类型归一化 ====================

def test_normalize_compare_period():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 109030.5},
            {"period_key": "current", "sales_amount": 80009.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.dimension is None
    assert len(obs.normalized_rows) == 1
    row = obs.normalized_rows[0]
    assert row.dimension_value is None
    metric = row.metric_values[MetricKey.sales_amount]
    assert metric.comparison_value == 109030.5
    assert metric.current_value == 80009.0


def test_normalize_breakdown_region():
    action = _action(type=ActionType.breakdown_region, dimension=DimensionKey.region)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "华东", "period_key": "comparison", "sales_amount": 50000.0},
            {"dimension_value": "华东", "period_key": "current", "sales_amount": 40000.0},
            {"dimension_value": "华南", "period_key": "comparison", "sales_amount": 30000.0},
            {"dimension_value": "华南", "period_key": "current", "sales_amount": 25000.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.dimension == DimensionKey.region
    members = {row.dimension_value: row for row in obs.normalized_rows}
    assert set(members) == {"华东", "华南"}
    assert members["华东"].metric_values[MetricKey.sales_amount].current_value == 40000.0
    assert members["华南"].metric_values[MetricKey.sales_amount].comparison_value == 30000.0


def test_normalize_breakdown_category():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "机床", "period_key": "comparison", "sales_amount": 10.0},
            {"dimension_value": "机床", "period_key": "current", "sales_amount": 20.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.normalized_rows[0].dimension_value == "机床"


def test_normalize_breakdown_product():
    action = _action(type=ActionType.breakdown_product, dimension=DimensionKey.product)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "产品X", "period_key": "comparison", "sales_amount": 10.0},
            {"dimension_value": "产品X", "period_key": "current", "sales_amount": 20.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.normalized_rows[0].dimension_value == "产品X"


def test_normalize_breakdown_customer():
    action = _action(type=ActionType.breakdown_customer, dimension=DimensionKey.customer)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "客户A", "period_key": "comparison", "sales_amount": 10.0},
            {"dimension_value": "客户A", "period_key": "current", "sales_amount": 20.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.dimension == DimensionKey.customer


def test_normalize_breakdown_customer_level():
    action = _action(type=ActionType.breakdown_customer, dimension=DimensionKey.customer_level)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "黄金", "period_key": "comparison", "sales_amount": 10.0},
            {"dimension_value": "黄金", "period_key": "current", "sales_amount": 20.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    assert obs.dimension == DimensionKey.customer_level


def test_normalize_multi_metric():
    action = _action(
        type=ActionType.analyze_unit_price,
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    result = _result(
        ["period_key", "sales_amount", "sales_quantity"],
        [
            {"period_key": "comparison", "sales_amount": 80009.0, "sales_quantity": 151},
            {"period_key": "current", "sales_amount": 90120.0, "sales_quantity": 322},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    row = obs.normalized_rows[0]
    assert set(row.metric_values) == {MetricKey.sales_amount, MetricKey.sales_quantity}
    assert row.metric_values[MetricKey.sales_quantity].current_value == 322.0
    assert row.metric_values[MetricKey.sales_quantity].comparison_value == 151.0


# ==================== 单期间成员补 0 ====================

def test_member_missing_in_one_period_fills_zero():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            # 新类别只在本期出现
            {"dimension_value": "新品", "period_key": "current", "sales_amount": 100.0},
            # 旧类别只在对比期出现
            {"dimension_value": "老品", "period_key": "comparison", "sales_amount": 50.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.success
    members = {row.dimension_value: row for row in obs.normalized_rows}
    assert members["新品"].metric_values[MetricKey.sales_amount].comparison_value == 0.0
    assert members["新品"].metric_values[MetricKey.sales_amount].current_value == 100.0
    assert members["老品"].metric_values[MetricKey.sales_amount].comparison_value == 50.0
    assert members["老品"].metric_values[MetricKey.sales_amount].current_value == 0.0


# ==================== empty / provider failed ====================

def test_normalize_empty():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period_key", "sales_amount"],
        [],
        status=ObservationStatus.empty,
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.empty
    assert obs.normalized_rows == []
    assert obs.error is None
    # 原始 QueryExecutionResult 保留
    assert obs.query_result.status == ObservationStatus.empty


def test_normalize_provider_failed_keeps_query_result():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period_key", "sales_amount"],
        [],
        status=ObservationStatus.failed,
        error="安全错误信息",
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert obs.normalized_rows == []
    assert obs.error == "安全错误信息"
    assert obs.query_result is result  # 原样保留


# ==================== contract 不匹配 ====================

def test_normalize_missing_period_column():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["sales_amount"],
        [{"sales_amount": 100.0}, {"sales_amount": 200.0}],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error
    assert "period_key" in obs.error


def test_normalize_missing_dimension_column():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    result = _result(
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 100.0},
            {"period_key": "current", "sales_amount": 200.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error
    assert "dimension_value" in obs.error


def test_normalize_missing_metric_alias():
    action = _action(type=ActionType.compare_period)
    contract = build_result_contract(action)
    result = _result(
        ["period_key", "销售额"],  # 数据库返回中文列，不是契约 alias
        [
            {"period_key": "comparison", "销售额": 100.0},
            {"period_key": "current", "销售额": 200.0},
        ],
    )
    obs = _normalize(action, result, contract)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error
    assert "sales_amount" in obs.error


def test_normalize_invalid_period_value():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period_key", "sales_amount"],
        [
            {"period_key": "last_year", "sales_amount": 100.0},
            {"period_key": "current", "sales_amount": 200.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error
    assert "comparison/current" in obs.error


def test_normalize_rejects_free_column_name_guessing():
    """SQL 返回任意列名（如 period）时不允许猜测：必须按契约列名失败。"""
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period", "sales_amount"],
        [
            {"period": "2025-01", "sales_amount": 100.0},
            {"period": "2025-02", "sales_amount": 200.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error


def test_normalize_missing_contract_key():
    action = _action(type=ActionType.compare_period)
    result = _result(
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 100.0},
            {"period_key": "current", "sales_amount": 200.0},
        ],
    )
    contract = {"period_alias": "period_key", "dimension_alias": None, "period_values": ["comparison", "current"]}
    obs = _normalize(action, result, contract)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error


def test_normalize_rejects_duplicate_member_period_rows():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    result = _result(
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "机床", "period_key": "comparison", "sales_amount": 10.0},
            {"dimension_value": "机床", "period_key": "comparison", "sales_amount": 99.0},
            {"dimension_value": "机床", "period_key": "current", "sales_amount": 20.0},
        ],
    )
    obs = _normalize(action, result)
    assert obs.status == ObservationStatus.failed
    assert "RESULT_NORMALIZATION_FAILED" in obs.error
