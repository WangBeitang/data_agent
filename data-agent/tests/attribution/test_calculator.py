"""Stage 4：Calculator 测试。

覆盖（Stage 4 指令 §十一 / SPEC §13.5）：
- 固定场景一：109030.5 → 80009.0，delta=-29021.5，change_rate≈-0.2662；
- 固定场景二：数量 151→322（rate≈1.1325）、金额 80009→90120（rate≈0.1264）、
  平均单件销售额 529.86→279.88；
- 边界：comparison=0、total_delta=0、quantity=0；
- contribution_rate > 1 / < 0；driver / offset / neutral。
"""

from datetime import date

import pytest

from app.attribution.action_router import build_result_contract, build_sub_query
from app.attribution.calculator import (
    contribution,
    period_change,
    unit_price,
)
from app.attribution.normalizer import Normalizer
from app.models.analysis import (
    Action,
    ActionType,
    DimensionKey,
    FactorDirection,
    MetricKey,
    Observation,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))
MAR = Period(label="2025年3月", start_date=date(2025, 3, 1), end_date=date(2025, 3, 31))


def _normalize_observation(
    action_type: ActionType,
    columns: list[str],
    rows: list[dict],
    dimension: DimensionKey | None = None,
    metrics: list[MetricKey] | None = None,
) -> Observation:
    action = Action(
        action_id="a1",
        type=action_type,
        metrics=metrics or [MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
        dimension=dimension,
        reason="测试",
    )
    result = QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=QueryTable(columns=columns, rows=rows, row_count=len(rows)),
        status=ObservationStatus.success,
        error=None,
    )
    return Normalizer.normalize("o1", action, build_sub_query(action), result, build_result_contract(action))


# ==================== 固定场景一：销售额下降 ====================

def test_scenario_one_period_change():
    obs = _normalize_observation(
        ActionType.compare_period,
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 109030.5},
            {"period_key": "current", "sales_amount": 80009.0},
        ],
    )
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    assert calc.type.value == "period_change"
    assert calc.metric == MetricKey.sales_amount
    assert calc.current_value == 80009.0
    assert calc.comparison_value == 109030.5
    assert calc.delta == -29021.5
    assert calc.change_rate == pytest.approx(-0.2662, abs=0.0001)
    assert calc.source_observation_ids == ["o1"]


# ==================== 固定场景二：量额背离 ====================

def test_scenario_two_quantity_change_rate():
    obs = _normalize_observation(
        ActionType.compare_period,
        ["period_key", "sales_quantity"],
        [
            {"period_key": "comparison", "sales_quantity": 151},
            {"period_key": "current", "sales_quantity": 322},
        ],
        metrics=[MetricKey.sales_quantity],
    )
    calc = period_change(obs, MetricKey.sales_quantity, calculation_id="c1")
    assert calc.delta == 171.0
    assert calc.change_rate == pytest.approx(1.1325, abs=0.0001)


def test_scenario_two_amount_change_rate():
    obs = _normalize_observation(
        ActionType.compare_period,
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 80009.0},
            {"period_key": "current", "sales_amount": 90120.0},
        ],
    )
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    assert calc.delta == 10111.0
    assert calc.change_rate == pytest.approx(0.1264, abs=0.0001)


def test_scenario_two_unit_price():
    obs = _normalize_observation(
        ActionType.analyze_unit_price,
        ["period_key", "sales_amount", "sales_quantity"],
        [
            {"period_key": "comparison", "sales_amount": 80009.0, "sales_quantity": 151},
            {"period_key": "current", "sales_amount": 90120.0, "sales_quantity": 322},
        ],
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    calc = unit_price(obs, calculation_id="c2")
    assert calc.current_sales_amount == 90120.0
    assert calc.current_sales_quantity == 322.0
    assert calc.comparison_sales_amount == 80009.0
    assert calc.comparison_sales_quantity == 151.0
    assert calc.current_unit_price == pytest.approx(279.88, abs=0.01)
    assert calc.comparison_unit_price == pytest.approx(529.86, abs=0.01)
    assert calc.delta == pytest.approx(-249.98, abs=0.01)
    assert calc.change_rate is not None
    assert calc.metric == MetricKey.avg_unit_sales_amount


# ==================== 边界：comparison=0 ====================

def test_period_change_comparison_zero_returns_none_rate():
    obs = _normalize_observation(
        ActionType.compare_period,
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 0},
            {"period_key": "current", "sales_amount": 100.0},
        ],
    )
    calc = period_change(obs, MetricKey.sales_amount)
    assert calc.delta == 100.0
    assert calc.change_rate is None


# ==================== 边界：total_delta=0 ====================

def test_contribution_total_delta_zero_all_rates_none_and_neutral():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "类别A", "period_key": "comparison", "sales_amount": 100.0},
            {"dimension_value": "类别A", "period_key": "current", "sales_amount": 80.0},
            {"dimension_value": "类别B", "period_key": "comparison", "sales_amount": 50.0},
            {"dimension_value": "类别B", "period_key": "current", "sales_amount": 70.0},
        ],
        dimension=DimensionKey.category,
    )
    calc = contribution(obs, MetricKey.sales_amount, total_delta=0.0, calculation_id="c1")
    assert calc.total_delta == 0.0
    assert all(item.contribution_rate is None for item in calc.items)
    assert all(item.direction == FactorDirection.neutral for item in calc.items)
    assert calc.dimension == DimensionKey.category


# ==================== 边界：quantity=0 ====================

def test_unit_price_quantity_zero_returns_none():
    obs = _normalize_observation(
        ActionType.analyze_unit_price,
        ["period_key", "sales_amount", "sales_quantity"],
        [
            {"period_key": "comparison", "sales_amount": 80009.0, "sales_quantity": 151},
            {"period_key": "current", "sales_amount": 90120.0, "sales_quantity": 0},
        ],
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    calc = unit_price(obs)
    assert calc.current_unit_price is None
    assert calc.comparison_unit_price == pytest.approx(529.86, abs=0.01)
    # 任一期间无法计算 → delta / change_rate 置 None，禁止补值
    assert calc.delta is None
    assert calc.change_rate is None


# ==================== contribution_rate > 1 / < 0 ====================

def test_contribution_rate_can_exceed_one():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "类别A", "period_key": "comparison", "sales_amount": 200.0},
            {"dimension_value": "类别A", "period_key": "current", "sales_amount": 100.0},
        ],
        dimension=DimensionKey.category,
    )
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-40.0)
    item = calc.items[0]
    assert item.delta == -100.0
    # -100 / -40 = 2.5 > 1，不 clamp 到 [0,1]
    assert item.contribution_rate > 1
    assert item.direction == FactorDirection.driver


def test_contribution_rate_can_be_negative():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "类别B", "period_key": "comparison", "sales_amount": 50.0},
            {"dimension_value": "类别B", "period_key": "current", "sales_amount": 80.0},
        ],
        dimension=DimensionKey.category,
    )
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-40.0)
    item = calc.items[0]
    assert item.delta == 30.0
    # 30 / -40 = -0.75 < 0
    assert item.contribution_rate < 0
    assert item.direction == FactorDirection.offset


# ==================== driver / offset / neutral ====================

def test_contribution_directions():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "下降类", "period_key": "comparison", "sales_amount": 100.0},
            {"dimension_value": "下降类", "period_key": "current", "sales_amount": 60.0},
            {"dimension_value": "上升类", "period_key": "comparison", "sales_amount": 50.0},
            {"dimension_value": "上升类", "period_key": "current", "sales_amount": 80.0},
            {"dimension_value": "持平类", "period_key": "comparison", "sales_amount": 30.0},
            {"dimension_value": "持平类", "period_key": "current", "sales_amount": 30.0},
        ],
        dimension=DimensionKey.category,
    )
    # 总体下降：total_delta < 0
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-40.0)
    by_member = {item.member: item for item in calc.items}
    assert by_member["下降类"].direction == FactorDirection.driver
    assert by_member["上升类"].direction == FactorDirection.offset
    assert by_member["持平类"].direction == FactorDirection.neutral
    assert by_member["持平类"].delta == 0.0


def test_contribution_driver_when_total_delta_positive():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "增长类", "period_key": "comparison", "sales_amount": 100.0},
            {"dimension_value": "增长类", "period_key": "current", "sales_amount": 150.0},
        ],
        dimension=DimensionKey.category,
    )
    calc = contribution(obs, MetricKey.sales_amount, total_delta=50.0)
    item = calc.items[0]
    assert item.direction == FactorDirection.driver
    assert item.contribution_rate == pytest.approx(1.0, abs=0.0001)


# ==================== 计算错误边界 ====================

def test_period_change_requires_overall_row():
    obs = _normalize_observation(
        ActionType.breakdown_category,
        ["dimension_value", "period_key", "sales_amount"],
        [
            {"dimension_value": "类别A", "period_key": "comparison", "sales_amount": 100.0},
            {"dimension_value": "类别A", "period_key": "current", "sales_amount": 80.0},
        ],
        dimension=DimensionKey.category,
    )
    with pytest.raises(ValueError):
        period_change(obs, MetricKey.sales_amount)


def test_contribution_requires_dimension_observation():
    obs = _normalize_observation(
        ActionType.compare_period,
        ["period_key", "sales_amount"],
        [
            {"period_key": "comparison", "sales_amount": 100.0},
            {"period_key": "current", "sales_amount": 80.0},
        ],
    )
    with pytest.raises(ValueError):
        contribution(obs, MetricKey.sales_amount, total_delta=-20.0)
