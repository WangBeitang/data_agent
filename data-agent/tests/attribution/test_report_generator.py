"""Stage 5：Report Generator 测试。

覆盖（Stage 5 指令 §十五）：
- metric_overview 数值来自 Calculation；
- drivers 只来自 driver Evidence / offsets 只来自 offset Evidence；
- FactorItem 至少有 Evidence；
- Top3 按 abs(delta) DESC，完整 Evidence 仍保留；
- partial 明确缺口（data_boundaries）；
- LLM 失败 deterministic fallback（不丢 Evidence）；
- Report 不产生 Evidence 外数字；
- failed 极简报告不伪造结论。
"""

from datetime import date

from app.attribution.calculator import contribution as _contribution
from app.attribution.calculator import period_change as _period_change
from app.attribution.evidence_builder import EvidenceBuilder
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
    Observation,
    Period,
    RequestMode,
    RouteResult,
    RouteSource,
)
from tests.attribution.test_graph import (
    _build_observation,
    _dimension_rows,
    REGION_ROWS,
    CATEGORY_ROWS,
    OVERALL_AMOUNT,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))

TARGET = AttributionTarget(metrics=[MetricKey.sales_amount], current_period=FEB, comparison_period=JAN)


class _FakeLLM:
    """默认失败：触发确定性模板。"""

    def __init__(self, output=None):
        self._output = output
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if self._output is None:
            raise RuntimeError("llm unavailable")
        return type("R", (), {"content": self._output})


def _build_state(status=AnalysisStatus.completed) -> dict:
    """构造完整证据状态：compare + region + category，全部成功。"""
    compare = Action(
        action_id="a1", type=ActionType.compare_period, metrics=[MetricKey.sales_amount],
        current_period=FEB, comparison_period=JAN, reason="总体比较",
    )
    region = Action(
        action_id="a2", type=ActionType.breakdown_region, metrics=[MetricKey.sales_amount],
        current_period=FEB, comparison_period=JAN, dimension=DimensionKey.region, reason="区域拆解",
    )
    category = Action(
        action_id="a3", type=ActionType.breakdown_category, metrics=[MetricKey.sales_amount],
        current_period=FEB, comparison_period=JAN, dimension=DimensionKey.category, reason="类别拆解",
    )
    o1 = _build_observation("o1", "a1", None, OVERALL_AMOUNT)
    o2 = _build_observation("o2", "a2", DimensionKey.region, _dimension_rows(REGION_ROWS))
    o3 = _build_observation("o3", "a3", DimensionKey.category, _dimension_rows(CATEGORY_ROWS))

    calc1 = _period_change(o1, MetricKey.sales_amount)
    # 使用与 Graph 一致的维度后缀 calculation_id，避免 region/category 冲突
    calc2 = _contribution(
        o2, MetricKey.sales_amount, calc1.delta,
        calculation_id="c_contribution_sales_amount_region",
    )
    calc3 = _contribution(
        o3, MetricKey.sales_amount, calc1.delta,
        calculation_id="c_contribution_sales_amount_category",
    )

    builder = EvidenceBuilder()
    ev1 = builder.build_period_change("ev1", compare, o1, calc1)
    ev2 = builder.build_contribution_members("ev2", region, o2, calc2)
    ev3 = builder.build_contribution_members("ev6", category, o3, calc3)

    state = initial_state(
        analysis_id="an1",
        question="为什么 2025 年 2 月销售额较 1 月明显下降？",
        requested_mode=RequestMode.auto,
        route=RouteResult(
            requested_mode=RequestMode.auto,
            resolved_mode=AnalysisMode.attribution,
            source=RouteSource.rule,
        ),
        target=TARGET,
    )
    state["actions"] = [compare, region, category]
    state["observations"] = [o1, o2, o3]
    state["calculations"] = [calc1, calc2, calc3]
    state["evidences"] = [ev1] + ev2 + ev3
    state["status"] = status
    return state


def _report(state=None, llm=None):
    generator = ReportGenerator(llm=llm or _FakeLLM())
    return generator.generate(state or _build_state())


# ==================== metric_overview 数值来自 Calculation ====================


def test_metric_overview_values_from_calculation():
    report = _report()
    assert len(report.metric_overview) == 1
    item = report.metric_overview[0]
    assert item.metric == MetricKey.sales_amount
    assert item.current_value == 80009.0
    assert item.comparison_value == 109030.5
    assert item.delta == -29021.5
    assert item.change_rate == -0.2662
    assert item.evidence_ids == ["ev1"]  # 引用 period_change Evidence


def test_metric_overview_only_for_target_metrics_with_calc():
    """无期间变化计算的指标不生成条目。"""
    state = _build_state()
    state["calculations"] = state["calculations"][1:]  # 去掉 period_change
    report = _report(state)
    assert report.metric_overview == []


# ==================== drivers / offsets 来源 ====================


def test_drivers_only_from_driver_evidence():
    state = _build_state()
    report = _report(state)
    by_id = {ev.evidence_id: ev for ev in state["evidences"]}
    assert report.drivers
    for factor in report.drivers:
        assert set(factor.evidence_ids).issubset(set(by_id))
        assert all(by_id[eid].direction.value == "driver" for eid in factor.evidence_ids)
        assert len(factor.evidence_ids) >= 1


def test_offsets_only_from_offset_evidence():
    state = _build_state()
    report = _report(state)
    by_id = {ev.evidence_id: ev for ev in state["evidences"]}
    for factor in report.offsets:
        assert set(factor.evidence_ids).issubset(set(by_id))
        assert all(by_id[eid].direction.value == "offset" for eid in factor.evidence_ids)


def test_factor_item_values_match_contribution_calculation():
    report = _report()
    state = _build_state()
    top = report.drivers[0]
    # 华东 delta = 35000 - 60000 = -25000
    region_calc = next(c for c in state["calculations"] if getattr(c, "dimension", None) == DimensionKey.region)
    expected = next(i for i in region_calc.items if i.member == top.member)
    assert top.delta == expected.delta
    assert top.contribution_rate == expected.contribution_rate
    assert top.dimension == DimensionKey.region


# ==================== Top3 排序 ====================


def test_drivers_sorted_by_abs_delta_desc_and_top3():
    report = _report()
    deltas = [abs(f.delta) for f in report.drivers if f.delta is not None]
    assert deltas == sorted(deltas, reverse=True)
    assert len(report.drivers) <= 3
    assert len(report.offsets) <= 3


def test_full_evidences_kept_despite_top3_display():
    state = _build_state()
    report = _report(state)
    # 展示 Top3，但证据明细保留全部 Evidence
    assert set(report.evidence_ids) == {ev.evidence_id for ev in state["evidences"]}
    assert len(report.evidence_ids) == len(state["evidences"])


# ==================== LLM 失败 fallback ====================


def test_llm_failure_uses_deterministic_template_keeping_evidence():
    state = _build_state()
    report = _report(state, llm=_FakeLLM())  # LLM 抛异常
    assert report is not None
    assert report.status == AnalysisStatus.completed
    assert report.metric_overview  # 数值未丢
    assert report.drivers and report.offsets
    assert set(report.evidence_ids) == {ev.evidence_id for ev in state["evidences"]}
    assert report.question_definition
    assert report.core_conclusion


def test_llm_invalid_output_falls_back_to_template():
    state = _build_state()
    report = _report(state, llm=_FakeLLM(output="not json"))
    assert report is not None
    assert report.metric_overview


# ==================== LLM 成功：有限语言组织 ====================


def test_llm_success_applies_summaries_and_recommendations():
    # 数值守卫要求：LLM 文案只能使用 Evidence/Calculation/原问题/目标期间中的数字
    llm = _FakeLLM(output=(
        '{"question_definition": "分析2025年2月销售额下降原因", '
        '"core_conclusion": "数据显示2025年2月销售额为80009.0，较2025年1月的109030.5减少29021.5。", '
        '"factor_summaries": {"华东的销售区域贡献": "华东变化贡献最大。"}, '
        '"recommendations": [{"factor_title": "华东的销售区域贡献", "text": "建议关注华东区域。"}]}'
    ))
    report = _report(llm=llm)
    assert report.question_definition == "分析2025年2月销售额下降原因"
    assert report.core_conclusion
    assert llm.calls == 1
    # factor_summaries 按 title 匹配并合入
    assert any(f.summary == "华东变化贡献最大。" for f in report.drivers)
    assert report.recommendations and report.recommendations[0].evidence_ids


def test_llm_recommendation_with_unknown_factor_dropped():
    llm = _FakeLLM(output=(
        '{"question_definition": "q", "core_conclusion": "c", "factor_summaries": {}, '
        '"recommendations": [{"factor_title": "不存在的因素", "text": "凭空建议"}]}'
    ))
    report = _report(llm=llm)
    assert report.recommendations == []  # 无对应证据的建议被丢弃


# ==================== partial / failed ====================


def test_partial_report_mentions_boundary_gaps():
    state = _build_state(status=AnalysisStatus.partial)
    report = _report(state)
    joined = "".join(report.data_boundaries)
    assert "当前归因仅基于现有销售订单数据" in joined
    assert "当前数据不支持库存、成本、利润、生产、设备、质量、产能等原因验证" in joined
    assert "拆解覆盖不完整" in joined


def test_partial_with_query_gaps_appends_specific_gap():
    from app.models.analysis import (
        ObservationStatus,
        QueryExecutionResult,
        QueryTable,
    )

    state = _build_state(status=AnalysisStatus.partial)
    # 模拟 product 维度查询失败
    failed_obs = Observation(
        observation_id="o4",
        action_id="a4",
        sub_query="s",
        query_result=QueryExecutionResult(
            query="q",
            sql=None,
            table=QueryTable(columns=[], rows=[], row_count=0),
            status=ObservationStatus.failed,
            error="失败",
        ),
        dimension=DimensionKey.product,
        normalized_rows=[],
        status=ObservationStatus.failed,
        error="失败",
    )
    state["observations"].append(failed_obs)
    report = _report(state)
    joined = "".join(report.data_boundaries)
    assert "产品" in joined


def test_failed_report_is_minimal_no_fabricated_conclusion():
    state = _build_state()
    # 清空证据/计算，模拟无有效证据
    state["evidences"] = []
    state["calculations"] = []
    state["observations"] = []
    state["status"] = AnalysisStatus.failed
    report = _report(state)
    assert report.status == AnalysisStatus.failed
    assert report.drivers == [] and report.offsets == []
    assert report.recommendations == []
    assert report.metric_overview == []
    assert any("未获得任何有效查询证据" in b for b in report.data_boundaries)


# ==================== 不产生 Evidence 外数字 ====================


def test_report_numbers_all_derived_from_evidence_and_calc():
    """Report 中所有数值必须能在 Evidence/Calculation 中找到。"""
    state = _build_state()
    report = _report(state)

    def _serialize(obj):
        import json

        return json.dumps(obj, ensure_ascii=False, default=str)

    evidence_text = _serialize([ev.model_dump() for ev in state["evidences"]])
    calc_text = _serialize([c.model_dump() for c in state["calculations"]])
    report_text = _serialize(report.model_dump())

    # 关键数值（delta / change_rate / current / comparison）必须存在于证据或计算中
    for overview in report.metric_overview:
        for value in (overview.current_value, overview.comparison_value, overview.delta, overview.change_rate):
            assert str(value) in evidence_text or str(value) in calc_text
    for factor in report.drivers + report.offsets:
        assert str(factor.delta) in evidence_text or str(factor.delta) in calc_text
        if factor.contribution_rate is not None:
            assert str(factor.contribution_rate) in evidence_text or str(factor.contribution_rate) in calc_text


def test_report_status_matches_state():
    assert _report().status == AnalysisStatus.completed
    assert _report(_build_state(status=AnalysisStatus.partial)).status == AnalysisStatus.partial


# ==================== LLM Report schema / 数值守卫 / 失败契约 ====================


def test_llm_report_prompt_recommendation_json_passes_schema():
    """attribution_report.prompt 约定的合法 recommendation JSON 能通过 schema。"""
    from app.attribution.report_generator import ReportGenerator

    text = (
        '{"question_definition": "q", "core_conclusion": "c", '
        '"factor_summaries": {}, '
        '"recommendations": [{"factor_title": "华东的销售区域贡献", "text": "建议关注华东区域。"}]}'
    )
    parsed = ReportGenerator._parse_llm_report(text)
    assert parsed is not None
    assert parsed.recommendations[0].factor_title == "华东的销售区域贡献"
    assert parsed.recommendations[0].text == "建议关注华东区域。"


def test_llm_report_rejects_extra_fields():
    """_LLMReport / _LLMRecommendation extra=forbid：额外字段 → 解析失败。"""
    from app.attribution.report_generator import ReportGenerator

    assert ReportGenerator._parse_llm_report(
        '{"question_definition": "q", "core_conclusion": "c", "factor_summaries": {}, '
        '"recommendations": [], "hidden": "x"}'
    ) is None
    assert ReportGenerator._parse_llm_report(
        '{"question_definition": "q", "core_conclusion": "c", "factor_summaries": {}, '
        '"recommendations": [{"factor_title": "华东的销售区域贡献", "text": "t", "confidence": 0.9}]}'
    ) is None


def test_llm_new_number_triggers_deterministic_fallback():
    """LLM 引入 Evidence 外新数字 → 判非法 → deterministic fallback（不修正）。"""
    llm = _FakeLLM(output=(
        '{"question_definition": "q", '
        '"core_conclusion": "数据显示销售额下降999.99。", '  # 999.99 不在证据中
        '"factor_summaries": {}, "recommendations": []}'
    ))
    state = _build_state()
    report = _report(state, llm=llm)
    assert "999.99" not in report.core_conclusion  # 模板兜底，未使用 LLM 新数字
    assert report.metric_overview  # 确定性数值未丢
    assert report.drivers  # Evidence 未丢


def test_llm_percentage_form_change_rate_still_rejected():
    """把 0.2662 写成 26.62%（新量级）→ 判非法 → 模板。"""
    llm = _FakeLLM(output=(
        '{"question_definition": "q", '
        '"core_conclusion": "变化率约26.62%。", '  # 0.2662 被写成 26.62
        '"factor_summaries": {}, "recommendations": []}'
    ))
    report = _report(llm=llm)
    assert "26.62" not in report.core_conclusion
