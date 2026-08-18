"""Stage 4：Evidence Builder 测试。

覆盖（Stage 4 指令 §十一）：
- 只接受 success Observation；
- 数值 Calculation 可追溯（类型 + source 关联）；
- contribution member Evidence（每成员一条）；
- direction 正确（来自 Calculation 确定性方向）；
- 不复制 SQL；
- 不生成不存在的新数字。
"""

import re
from datetime import date

import pytest

from app.attribution.action_router import build_result_contract, build_sub_query
from app.attribution.calculator import contribution, period_change, unit_price
from app.attribution.evidence_builder import EvidenceBuilder
from app.attribution.normalizer import Normalizer
from app.models.analysis import (
    Action,
    ActionType,
    ContributionCalculation,
    DimensionKey,
    FactorDirection,
    MetricKey,
    Observation,
    ObservationStatus,
    Period,
    QueryExecutionResult,
    QueryTable,
)

# 使用不含数字的期间标签，便于"不生成新数字"的严格断言
JAN = Period(label="一月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="二月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))

_BUILDER = EvidenceBuilder()

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    return {float(m) for m in _NUM_RE.findall(text)}


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


def _normalize(action: Action, rows: list[dict], columns: list[str]) -> Observation:
    result = QueryExecutionResult(
        query="q",
        sql="SELECT ...",
        table=QueryTable(columns=columns, rows=rows, row_count=len(rows)),
        status=ObservationStatus.success,
        error=None,
    )
    return Normalizer.normalize("o1", action, build_sub_query(action), result, build_result_contract(action))


def _success_observation(dimension=None) -> Observation:
    action = _action(dimension=dimension)
    columns = ["period_key", "sales_amount"]
    rows = [
        {"period_key": "comparison", "sales_amount": 109030.5},
        {"period_key": "current", "sales_amount": 80009.0},
    ]
    if dimension is not None:
        columns.insert(0, "dimension_value")
        for row in rows:
            row["dimension_value"] = "成员A"
    return _normalize(action, rows, columns)


def _failed_observation() -> Observation:
    result = QueryExecutionResult(
        query="q",
        sql=None,
        table=QueryTable(columns=[], rows=[], row_count=0),
        status=ObservationStatus.failed,
        error="查询失败",
    )
    return Observation(
        observation_id="o1",
        action_id="a1",
        sub_query="s",
        query_result=result,
        dimension=None,
        normalized_rows=[],
        status=ObservationStatus.failed,
        error="查询失败",
    )


# ==================== 只接受 success Observation ====================

def test_evidence_rejects_non_success_observation():
    failed_obs = _failed_observation()
    calc = ContributionCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.sales_amount,
        formula="f",
        dimension=DimensionKey.region,
        total_delta=-20.0,
        items=[],
    )
    with pytest.raises(ValueError):
        _BUILDER.build_period_change("e1", _action(), failed_obs, calc)


def test_evidence_rejects_empty_observation():
    obs = _success_observation()
    obs.status = ObservationStatus.empty
    obs.normalized_rows = []
    calc = ContributionCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.sales_amount,
        formula="f",
        dimension=DimensionKey.region,
        total_delta=-20.0,
        items=[],
    )
    with pytest.raises(ValueError):
        _BUILDER.build_period_change("e1", _action(), obs, calc)


# ==================== 数值 Calculation 可追溯 ====================

def test_period_change_evidence_references_calculation():
    obs = _success_observation()
    action = _action()
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    evidence = _BUILDER.build_period_change("e1", action, obs, calc)
    assert evidence.observation_ids == ["o1"]
    assert evidence.calculation_ids == ["c1"]
    assert evidence.action_id == "a1"
    assert evidence.metric == MetricKey.sales_amount
    assert evidence.dimension is None
    assert evidence.member is None
    assert evidence.direction is None


def test_evidence_rejects_wrong_calculation_type():
    obs = _success_observation()
    # 用错误类型的 Calculation（ContributionCalculation 冒充 PeriodChangeCalculation）
    bad_calc = ContributionCalculation(
        calculation_id="c1",
        source_observation_ids=["o1"],
        metric=MetricKey.sales_amount,
        formula="f",
        dimension=DimensionKey.region,
        total_delta=-20.0,
        items=[],
    )
    with pytest.raises(ValueError):
        _BUILDER.build_period_change("e1", _action(), obs, bad_calc)


def test_evidence_rejects_unlinked_calculation():
    obs = _success_observation()
    # 引用其它 Observation 的 Calculation 与证据对象不匹配
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    calc.source_observation_ids = ["o_other"]
    with pytest.raises(ValueError):
        _BUILDER.build_period_change("e1", _action(), obs, calc)


# ==================== contribution member Evidence ====================

def _breakdown_observation() -> Observation:
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    rows = [
        {"dimension_value": "类别A", "period_key": "comparison", "sales_amount": 70000.0},
        {"dimension_value": "类别A", "period_key": "current", "sales_amount": 50000.0},
        {"dimension_value": "类别B", "period_key": "comparison", "sales_amount": 30000.0},
        {"dimension_value": "类别B", "period_key": "current", "sales_amount": 25000.0},
        {"dimension_value": "类别C", "period_key": "comparison", "sales_amount": 9030.5},
        {"dimension_value": "类别C", "period_key": "current", "sales_amount": 5009.0},
    ]
    obs = _normalize(action, rows, ["dimension_value", "period_key", "sales_amount"])
    obs.observation_id = "o2"
    obs.action_id = "a2"
    return obs


def test_contribution_builds_one_evidence_per_member():
    obs = _breakdown_observation()
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-29021.5, calculation_id="c2")
    evidences = _BUILDER.build_contribution_members("e_region", action, obs, calc)
    assert len(evidences) == 3
    members = {evidence.member for evidence in evidences}
    assert members == {"类别A", "类别B", "类别C"}
    for evidence in evidences:
        assert evidence.calculation_ids == ["c2"]
        assert evidence.observation_ids == ["o2"]
        assert evidence.dimension == DimensionKey.category
        assert evidence.metric == MetricKey.sales_amount


def test_contribution_evidence_direction_matches_calculation():
    obs = _breakdown_observation()
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-29021.5, calculation_id="c2")
    by_member = {item.member: item for item in calc.items}
    assert by_member["类别A"].direction == FactorDirection.driver
    assert by_member["类别B"].direction == FactorDirection.driver
    assert by_member["类别C"].direction == FactorDirection.driver
    evidences = _BUILDER.build_contribution_members("e_region", action, obs, calc)
    for evidence in evidences:
        # direction 必须来自 Calculation 的确定性方向结果
        assert evidence.direction == by_member[evidence.member].direction
        assert evidence.direction == FactorDirection.driver


def test_contribution_evidence_offset_and_neutral_direction():
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    rows = [
        {"dimension_value": "下降类", "period_key": "comparison", "sales_amount": 100.0},
        {"dimension_value": "下降类", "period_key": "current", "sales_amount": 60.0},
        {"dimension_value": "上升类", "period_key": "comparison", "sales_amount": 50.0},
        {"dimension_value": "上升类", "period_key": "current", "sales_amount": 80.0},
        {"dimension_value": "持平类", "period_key": "comparison", "sales_amount": 30.0},
        {"dimension_value": "持平类", "period_key": "current", "sales_amount": 30.0},
    ]
    obs = _normalize(action, rows, ["dimension_value", "period_key", "sales_amount"])
    obs.observation_id = "o2"
    obs.action_id = "a2"
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-40.0, calculation_id="c2")
    evidences = _BUILDER.build_contribution_members("e_region", action, obs, calc)
    by_member = {evidence.member: evidence for evidence in evidences}
    assert by_member["下降类"].direction == FactorDirection.driver
    assert by_member["上升类"].direction == FactorDirection.offset
    assert by_member["持平类"].direction == FactorDirection.neutral


# ==================== 不复制 SQL ====================

def test_evidence_does_not_copy_sql():
    obs = _success_observation()
    action = _action()
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    evidence = _BUILDER.build_period_change("e1", action, obs, calc)
    assert "SELECT" not in evidence.statement
    assert obs.query_result.sql not in evidence.statement
    # SQL 追溯链：Evidence -> observation_id -> Observation.query_result.sql
    assert evidence.observation_ids == ["o1"]
    assert obs.query_result.sql == "SELECT ..."


def test_contribution_evidence_does_not_copy_sql():
    obs = _breakdown_observation()
    action = _action(type=ActionType.breakdown_category, dimension=DimensionKey.category)
    calc = contribution(obs, MetricKey.sales_amount, total_delta=-29021.5, calculation_id="c2")
    evidences = _BUILDER.build_contribution_members("e_region", action, obs, calc)
    for evidence in evidences:
        assert "SELECT" not in evidence.statement
        assert obs.query_result.sql not in evidence.statement


# ==================== 不生成不存在的新数字 ====================

def test_evidence_no_invented_numbers():
    obs = _success_observation()
    action = _action()
    calc = period_change(obs, MetricKey.sales_amount, calculation_id="c1")
    evidence = _BUILDER.build_period_change("e1", action, obs, calc)

    allowed: set[float] = set()
    allowed |= _numbers(str(calc.current_value))
    allowed |= _numbers(str(calc.comparison_value))
    allowed |= _numbers(str(calc.delta))
    allowed |= _numbers(str(calc.change_rate))
    # Observation 归一化事实（本测试中与 calc 同源）
    for row in obs.query_result.table.rows:
        for value in row.values():
            if isinstance(value, (int, float)):
                allowed.add(float(value))

    statement_numbers = _numbers(evidence.statement)
    assert statement_numbers == allowed
    assert 99999.0 not in statement_numbers
    assert "80009.0" in evidence.statement


def test_unit_price_evidence_no_invented_numbers():
    action = _action(
        type=ActionType.analyze_unit_price,
        metrics=[MetricKey.sales_amount, MetricKey.sales_quantity],
    )
    rows = [
        {"period_key": "comparison", "sales_amount": 80009.0, "sales_quantity": 151},
        {"period_key": "current", "sales_amount": 90120.0, "sales_quantity": 322},
    ]
    obs = _normalize(action, rows, ["period_key", "sales_amount", "sales_quantity"])
    calc = unit_price(obs, calculation_id="c1")
    evidence = _BUILDER.build_unit_price("e1", action, obs, calc)
    assert calc.current_unit_price is not None
    assert calc.comparison_unit_price is not None
    assert "529.86" in evidence.statement
    assert "279.88" in evidence.statement
    assert "SELECT" not in evidence.statement
