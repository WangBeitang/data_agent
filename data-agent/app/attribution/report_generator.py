"""Report Generator（Stage 5）。

依据冻结文档（SPEC §3.10 / §10.4、数据对象设计 §14、Stage 5 指令 §十二）：

```text
确定性结构装配 + LLM 有限语言组织
```

Python 确定性生成（禁止 LLM 参与）：

- metric_overview（数值来自 PeriodChangeCalculation）
- drivers / offsets（只来自 driver/offset Evidence，关联 ContributionCalculation
  成员值，按 abs(delta) DESC 排序，最多各 3 个）
- evidence_ids（全部 Evidence，展示 Top3 不删除完整 Evidence）
- 所有数值与 Evidence 引用
- data_boundaries 基础文案
- status

LLM 只允许生成：

- question_definition
- core_conclusion
- FactorItem.summary 的语言组织
- 与已有 Evidence 对应的 recommendations

硬性禁止：

- 新增 Evidence 中不存在的数字；
- 编造业务事实；
- 把「数据归因」写成已验证因果；
- 引申库存、生产、成本、利润等无数据支持原因。

LLM 报告失败：不丢弃已有 Evidence → 确定性模板报告；
只有确定性报告也无法生成时才进入 REPORT_GENERATION_FAILED（由调用方处理）。
"""

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.agent import llm as llm_module
from app.attribution.action_router import DIMENSION_DISPLAY_NAMES, METRIC_DISPLAY_NAMES
from app.attribution.state import AttributionState
from app.core.log import logger
from app.models.analysis import (
    ActionType,
    AnalysisStatus,
    AttributionReport,
    CalculationType,
    ContributionCalculation,
    FactorDirection,
    FactorItem,
    MetricKey,
    MetricOverviewItem,
    PeriodChangeCalculation,
    RecommendationItem,
)
from app.prompt.prompt_loader import load_prompt

# 固定 data_boundaries 基础文案（数据对象设计 §14.5 / Stage 5 指令 §十二）
_BOUNDARY_BASIC_1 = "当前归因仅基于现有销售订单数据，属于数据归因，不代表实验验证的因果关系。"
_BOUNDARY_BASIC_2 = "当前数据不支持库存、成本、利润、生产、设备、质量、产能等原因验证。"
_BOUNDARY_FAILED = "未获得任何有效查询证据，无法形成有效归因结论。"
_BOUNDARY_PARTIAL = "已触发查询上限或连续未取得有效数据，拆解覆盖不完整，结论存在不确定性。"

# 默认最多展示 driver / offset 数量（完整 Evidence 仍保留）
_DEFAULT_TOP_N = 3


@dataclass
class _Assembled:
    """确定性装配中间结果（不进 State、不对外）。"""

    metric_overview: list[MetricOverviewItem]
    drivers: list[FactorItem]
    offsets: list[FactorItem]
    evidence_ids: list[str]
    boundaries: list[str]
    # LLM 输入的 factor 摘要（title -> 确定性数据文本）
    factor_digests: dict[str, str] = field(default_factory=dict)


class _LLMRecommendation(BaseModel):
    factor_title: str
    text: str


class _LLMReport(BaseModel):
    question_definition: str
    core_conclusion: str
    factor_summaries: dict[str, str] = Field(default_factory=dict)
    recommendations: list[_LLMRecommendation] = Field(default_factory=list)


class ReportGenerator:
    """确定性结构装配 + LLM 有限语言组织；LLM 失败 → 确定性模板。"""

    def __init__(self, llm=None):
        self._llm = llm if llm is not None else llm_module.llm
        self._prompt = load_prompt("attribution_report")

    # ==================== 公共入口 ====================

    def generate(self, state: AttributionState) -> AttributionReport:
        """生成正式报告；LLM 失败时回退确定性模板报告。

        确定性装配失败（理论上仅在状态极端异常时）则抛出异常，由调用方
        映射 REPORT_GENERATION_FAILED；正常流程不得丢弃已有 Evidence。
        """
        assembled = self._assemble(state)
        # 确定性报告兜底：先把确定性内容装配好，再尝试 LLM 语言组织
        report = self._try_llm_report(state, assembled)
        if report is not None:
            return report
        logger.warning("Report LLM 失败或输出非法，使用确定性模板报告")
        return self._template_report(state, assembled)

    # ==================== 确定性装配（禁止 LLM） ====================

    def _assemble(self, state: AttributionState) -> _Assembled:
        metric_overview = self._metric_overview(state)
        drivers, offsets, factor_digests = self._factors(state)
        boundaries = self._boundaries(state)

        return _Assembled(
            metric_overview=metric_overview,
            drivers=drivers,
            offsets=offsets,
            evidence_ids=[e.evidence_id for e in state["evidences"]],
            boundaries=boundaries,
            factor_digests=factor_digests,
        )

    @staticmethod
    def _metric_overview(state: AttributionState) -> list[MetricOverviewItem]:
        """指标概览：数值全部来自 PeriodChangeCalculation（不来自 LLM）。"""
        overview: list[MetricOverviewItem] = []
        calc_by_metric: dict[MetricKey, PeriodChangeCalculation] = {}
        for calc in state["calculations"]:
            if isinstance(calc, PeriodChangeCalculation):
                calc_by_metric[calc.metric] = calc

        for metric in state["target"].metrics:
            calc = calc_by_metric.get(metric)
            if calc is None:
                # 无期间变化计算（如派生指标或未完成比较）→ 不生成条目
                continue
            evidence_ids = [
                ev.evidence_id
                for ev in state["evidences"]
                if calc.calculation_id in ev.calculation_ids
            ]
            overview.append(
                MetricOverviewItem(
                    metric=metric,
                    current_period_label=state["target"].current_period.label,
                    current_value=calc.current_value,
                    comparison_period_label=state["target"].comparison_period.label,
                    comparison_value=calc.comparison_value,
                    delta=calc.delta,
                    change_rate=calc.change_rate,
                    evidence_ids=evidence_ids,
                )
            )
        return overview

    @staticmethod
    def _factors(state: AttributionState) -> tuple[list[FactorItem], list[FactorItem], dict[str, str]]:
        """驱动/抵消因素：只来自 driver/offset Evidence，按 abs(delta) DESC。

        每个 Evidence 关联其 ContributionCalculation 的成员行取值；
        计算不出 delta 的因素排在最后。
        """
        calc_by_id = {calc.calculation_id: calc for calc in state["calculations"]}

        def _build(evidence) -> FactorItem | None:
            if evidence.dimension is None or evidence.member is None:
                return None
            item = None
            for calc_id in evidence.calculation_ids:
                calc = calc_by_id.get(calc_id)
                if isinstance(calc, ContributionCalculation) and calc.dimension == evidence.dimension:
                    for contribution_item in calc.items:
                        if contribution_item.member == evidence.member:
                            item = contribution_item
                            break
                if item is not None:
                    break
            return FactorItem(
                title=evidence.title,
                metric=evidence.metric,
                dimension=evidence.dimension,
                member=evidence.member,
                delta=item.delta if item is not None else None,
                contribution_rate=item.contribution_rate if item is not None else None,
                summary="",  # 由 LLM / 模板填充
                evidence_ids=[evidence.evidence_id],
            )

        drivers: list[FactorItem] = []
        offsets: list[FactorItem] = []
        for evidence in state["evidences"]:
            if evidence.direction == FactorDirection.driver:
                factor = _build(evidence)
                if factor is not None:
                    drivers.append(factor)
            elif evidence.direction == FactorDirection.offset:
                factor = _build(evidence)
                if factor is not None:
                    offsets.append(factor)

        drivers.sort(key=_abs_delta_key)
        offsets.sort(key=_abs_delta_key)

        factor_digests: dict[str, str] = {}
        for factor in drivers[: _DEFAULT_TOP_N] + offsets[: _DEFAULT_TOP_N]:
            factor_digests[factor.title] = _factor_digest(factor)

        return drivers[:_DEFAULT_TOP_N], offsets[:_DEFAULT_TOP_N], factor_digests

    @staticmethod
    def _boundaries(state: AttributionState) -> list[str]:
        """data_boundaries：固定基础文案 + 实际数据缺口。"""
        boundaries = [_BOUNDARY_BASIC_1, _BOUNDARY_BASIC_2]

        if state["status"] == AnalysisStatus.failed:
            boundaries.append(_BOUNDARY_FAILED)
            return boundaries

        gaps = ReportGenerator._query_gaps(state)
        boundaries.extend(gaps)

        if state["status"] == AnalysisStatus.partial:
            boundaries.append(_BOUNDARY_PARTIAL)
        return boundaries

    @staticmethod
    def _query_gaps(state: AttributionState) -> list[str]:
        """empty/failed Observation 对应的实际数据缺口描述。"""
        dims: set = set()
        for obs in state["observations"]:
            if obs.status.value in ("empty", "failed") and obs.dimension is not None:
                dims.add(obs.dimension)
        if not dims:
            return []
        names = "、".join(DIMENSION_DISPLAY_NAMES[d] for d in sorted(dims, key=lambda d: d.value))
        return [f"以下维度查询未取得有效数据，无法进一步确认相关因素：{names}。"]

    # ==================== LLM 语言组织（有限） ====================

    def _try_llm_report(self, state: AttributionState, assembled: _Assembled) -> AttributionReport | None:
        """LLM 生成 question_definition / core_conclusion / summary / recommendations。

        任何异常或校验失败 → 返回 None（调用方走确定性模板）。
        """
        if not assembled.evidence_ids:
            # 无有效 Evidence：不调用 LLM（避免编造），直接走模板
            return None
        prompt = self._build_llm_prompt(state, assembled)
        try:
            raw = self._llm.invoke(prompt)
            text = raw.content if hasattr(raw, "content") else str(raw)
            llm_report = self._parse_llm_report(text)
        except Exception as e:
            logger.warning(f"Report LLM 调用/解析失败 error={e!r}")
            return None
        if llm_report is None:
            return None
        return self._apply_llm(state, assembled, llm_report)

    @staticmethod
    def _parse_llm_report(text: str) -> _LLMReport | None:
        text = (text or "").strip()
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if 0 <= start < end:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return None
        try:
            return _LLMReport(**data)
        except Exception:
            return None

    @staticmethod
    def _apply_llm(
        state: AttributionState,
        assembled: _Assembled,
        llm_report: _LLMReport,
    ) -> AttributionReport:
        """把 LLM 语言结果合入确定性结构。"""
        factor_by_title = {f.title: f for f in assembled.drivers + assembled.offsets}
        for factor in assembled.drivers + assembled.offsets:
            summary = llm_report.factor_summaries.get(factor.title)
            factor.summary = summary if summary and summary.strip() else _template_factor_summary(factor)

        recommendations: list[RecommendationItem] = []
        for rec in llm_report.recommendations:
            factor = factor_by_title.get(rec.factor_title)
            if factor is None:
                # LLM 引用了不存在的因素：丢弃，禁止凭空建议
                continue
            recommendations.append(
                RecommendationItem(text=rec.text.strip(), evidence_ids=list(factor.evidence_ids))
            )

        return AttributionReport(
            analysis_id=state["analysis_id"],
            status=state["status"],
            question_definition=llm_report.question_definition.strip()
            or _template_question(state),
            core_conclusion=llm_report.core_conclusion.strip()
            or _template_core_conclusion(state, assembled),
            metric_overview=assembled.metric_overview,
            drivers=assembled.drivers,
            offsets=assembled.offsets,
            evidence_ids=assembled.evidence_ids,
            data_boundaries=assembled.boundaries,
            recommendations=recommendations,
        )

    # ==================== 确定性模板报告 ====================

    @staticmethod
    def _template_report(state: AttributionState, assembled: _Assembled) -> AttributionReport:
        """LLM 失败时的确定性模板报告（不丢弃已有 Evidence）。"""
        drivers = assembled.drivers
        offsets = assembled.offsets
        for factor in drivers + offsets:
            factor.summary = _template_factor_summary(factor)

        recommendations: list[RecommendationItem] = []
        for factor in drivers[:1]:
            recommendations.append(
                RecommendationItem(
                    text=f"建议进一步关注「{factor.member}」的{DIMENSION_DISPLAY_NAMES[factor.dimension]}变化对{METRIC_DISPLAY_NAMES[factor.metric]}的影响。",
                    evidence_ids=list(factor.evidence_ids),
                )
            )

        return AttributionReport(
            analysis_id=state["analysis_id"],
            status=state["status"],
            question_definition=_template_question(state),
            core_conclusion=_template_core_conclusion(state, assembled),
            metric_overview=assembled.metric_overview,
            drivers=drivers,
            offsets=offsets,
            evidence_ids=assembled.evidence_ids,
            data_boundaries=assembled.boundaries,
            recommendations=recommendations,
        )

    # ==================== Prompt 构造 ====================

    def _build_llm_prompt(self, state: AttributionState, assembled: _Assembled) -> str:
        prompt = self._prompt
        prompt = prompt.replace("{question}", state["question"])
        prompt = prompt.replace("{target}", _target_digest(state))
        prompt = prompt.replace(
            "{evidences}",
            "\n".join(f"- {ev.statement}" for ev in state["evidences"]),
        )
        prompt = prompt.replace("{drivers}", _factors_digest(assembled.drivers))
        prompt = prompt.replace("{offsets}", _factors_digest(assembled.offsets))
        prompt = prompt.replace("{boundaries}", "\n".join(f"- {b}" for b in assembled.boundaries))
        return prompt


# ==================== 内部工具 ====================


def _abs_delta_key(factor: FactorItem):
    """排序键：delta 为 None 排最后，否则按 abs(delta) 降序。"""
    if factor.delta is None:
        return (1, 0)
    return (0, -abs(factor.delta))


def _factor_digest(factor: FactorItem) -> str:
    rate = (
        f"，贡献率约{factor.contribution_rate}"
        if factor.contribution_rate is not None
        else ""
    )
    return (
        f"{factor.member}（{DIMENSION_DISPLAY_NAMES[factor.dimension]}）："
        f"{METRIC_DISPLAY_NAMES[factor.metric]}变化{factor.delta}{rate}"
    )


def _factors_digest(factors: list[FactorItem]) -> str:
    if not factors:
        return "（无）"
    return "\n".join(f"- {_factor_digest(f)}" for f in factors)


def _target_digest(state: AttributionState) -> str:
    target = state["target"]
    return (
        f"指标：{'、'.join(METRIC_DISPLAY_NAMES[m] for m in target.metrics)}；"
        f"本期：{target.current_period.label}；对比期：{target.comparison_period.label}"
    )


def _template_question(state: AttributionState) -> str:
    return f"问题：{state['question']}"


def _template_core_conclusion(state: AttributionState, assembled: _Assembled) -> str:
    """确定性核心结论：优先引用期间变化 Evidence，无则说明数据不足。"""
    target = state["target"]
    for item in assembled.metric_overview:
        if item.change_rate is not None:
            return (
                f"本期{target.current_period.label}{METRIC_DISPLAY_NAMES[item.metric]}为{item.current_value}，"
                f"较{target.comparison_period.label}的{item.comparison_value}变化{item.delta}"
                f"（变化率约{item.change_rate}）。"
            )
        return (
            f"本期{target.current_period.label}{METRIC_DISPLAY_NAMES[item.metric]}为{item.current_value}，"
            f"较{target.comparison_period.label}的{item.comparison_value}变化{item.delta}。"
        )
    if assembled.drivers:
        top = assembled.drivers[0]
        return (
            f"数据显示「{top.member}」的{DIMENSION_DISPLAY_NAMES[top.dimension]}变化"
            f"是{METRIC_DISPLAY_NAMES[top.metric]}变化的主要驱动因素之一。"
        )
    if state["status"] == AnalysisStatus.failed:
        return "本次归因分析未获得足够的有效数据，无法形成有效归因结论。"
    return "本次归因分析取得部分事实证据，但证据不足以形成完整归因结论。"


def _template_factor_summary(factor: FactorItem) -> str:
    rate = (
        f"，贡献率约{factor.contribution_rate}"
        if factor.contribution_rate is not None
        else ""
    )
    if factor.delta is not None:
        return f"{factor.member}的{DIMENSION_DISPLAY_NAMES[factor.dimension]}{METRIC_DISPLAY_NAMES[factor.metric]}变化{factor.delta}{rate}。"
    return f"{factor.member}的{DIMENSION_DISPLAY_NAMES[factor.dimension]}拆解事实已记录。"
