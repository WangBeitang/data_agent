"""Evidence Builder（Stage 4 归因确定性核心）。

依据冻结文档（数据对象设计 §12 / SPEC §5.9）：

```text
Action + Observation + Calculation → Evidence[]
```

规则：

1. 查询事实必须来自 success Observation；
2. 涉及 delta/change_rate/contribution_rate/unit_price 的 Evidence
   必须引用对应 Calculation；
3. breakdown 贡献可以每个 member 生成一条 Evidence；
4. Evidence 不复制 SQL；SQL 追溯链保持
   Evidence → observation_id → Observation.query_result.sql；
5. 不新增 confidence；
6. statement 不得出现 Observation / Calculation 中不存在的新数值；
7. driver / offset / neutral 必须来自 Calculation 的确定性方向结果。

本模块不调用 LLM、不访问数据库。
"""

from app.attribution.action_router import DIMENSION_DISPLAY_NAMES, METRIC_DISPLAY_NAMES
from app.models.analysis import (
    Action,
    ContributionCalculation,
    Evidence,
    FactorDirection,
    Observation,
    ObservationStatus,
    PeriodChangeCalculation,
    UnitPriceCalculation,
)

_DIRECTION_TEXT = {
    FactorDirection.driver: "驱动因素",
    FactorDirection.offset: "抵消因素",
    FactorDirection.neutral: "中性因素",
}


def _fmt(value: float) -> str:
    return str(value)


class EvidenceBuilder:
    """从成功 Observation 与对应 Calculation 构建可追溯 Evidence。"""

    # ==================== 校验 ====================

    @staticmethod
    def _require_success(observation: Observation) -> None:
        if observation.status != ObservationStatus.success:
            raise ValueError(f"Evidence 只接受 success Observation，got status={observation.status.value}")

    @staticmethod
    def _require_linked(observation: Observation, calculation) -> None:
        if observation.observation_id not in calculation.source_observation_ids:
            raise ValueError(
                f"Evidence 引用的 Calculation 必须覆盖该 Observation（{observation.observation_id}）"
            )

    # ==================== 期间变化证据 ====================

    def build_period_change(
        self,
        evidence_id: str,
        action: Action,
        observation: Observation,
        calculation: PeriodChangeCalculation,
    ) -> Evidence:
        """总体期间变化证据（e.g. 场景一总体销售额下降）。"""
        self._require_success(observation)
        if not isinstance(calculation, PeriodChangeCalculation):
            raise ValueError("period_change Evidence 必须引用 PeriodChangeCalculation")
        self._require_linked(observation, calculation)

        metric_name = METRIC_DISPLAY_NAMES.get(calculation.metric, calculation.metric.value)
        current_label = action.current_period.label if action.current_period else "本期"
        comparison_label = action.comparison_period.label if action.comparison_period else "对比期"

        rate_part = (
            f"，变化率约{_fmt(calculation.change_rate)}"
            if calculation.change_rate is not None
            else "，对比期值为0无法计算变化率"
        )
        statement = (
            f"{current_label}{metric_name}为{_fmt(calculation.current_value)}，"
            f"较{comparison_label}的{_fmt(calculation.comparison_value)}"
            f"变化{_fmt(calculation.delta)}{rate_part}。"
        )

        return Evidence(
            evidence_id=evidence_id,
            action_id=action.action_id,
            observation_ids=[observation.observation_id],
            calculation_ids=[calculation.calculation_id],
            title=f"{current_label}{metric_name}期间变化",
            statement=statement,
            metric=calculation.metric,
            dimension=None,
            member=None,
            direction=None,
        )

    # ==================== 维度贡献证据（每 member 一条） ====================

    def build_contribution_members(
        self,
        evidence_id_prefix: str,
        action: Action,
        observation: Observation,
        calculation: ContributionCalculation,
    ) -> list[Evidence]:
        """维度贡献证据：每个维度成员生成一条 Evidence。

        direction 必须来自 Calculation 的确定性方向结果（item.direction）。
        """
        self._require_success(observation)
        if not isinstance(calculation, ContributionCalculation):
            raise ValueError("contribution Evidence 必须引用 ContributionCalculation")
        self._require_linked(observation, calculation)

        metric_name = METRIC_DISPLAY_NAMES.get(calculation.metric, calculation.metric.value)
        dimension_name = DIMENSION_DISPLAY_NAMES.get(calculation.dimension, calculation.dimension.value)

        evidences: list[Evidence] = []
        for index, item in enumerate(calculation.items, start=1):
            rate_part = (
                f"，贡献率约{_fmt(item.contribution_rate)}"
                if item.contribution_rate is not None
                else "，总体变化为0无法计算贡献率"
            )
            statement = (
                f"{item.member}的{dimension_name}{metric_name}"
                f"由{_fmt(item.comparison_value)}变为{_fmt(item.current_value)}，"
                f"变化{_fmt(item.delta)}{rate_part}，为{_DIRECTION_TEXT[item.direction]}。"
            )
            evidences.append(
                Evidence(
                    evidence_id=f"{evidence_id_prefix}_{index}",
                    action_id=action.action_id,
                    observation_ids=[observation.observation_id],
                    calculation_ids=[calculation.calculation_id],
                    title=f"{item.member}的{dimension_name}贡献",
                    statement=statement,
                    metric=calculation.metric,
                    dimension=calculation.dimension,
                    member=item.member,
                    direction=item.direction,
                )
            )
        return evidences

    # ==================== 原始拆解事实证据（无 Calculation，Stage 5 最小扩展） ====================

    def build_raw_breakdown(
        self,
        evidence_id: str,
        action: Action,
        observation: Observation,
    ) -> Evidence:
        """尚无总体变化 Calculation 时的原始拆解事实 Evidence。

        - 只在成功 breakdown Observation 存在但无法生成 Contribution 时使用
          （总体变化尚不存在，不猜 total_delta）；
        - calculation_ids=[]（不引用任何计算）；
        - statement 只使用 Observation.normalized_rows 中已存在的数字，
          不得产生 Observation 中不存在的数字。
        """
        self._require_success(observation)
        if observation.dimension is None:
            raise ValueError("raw breakdown Evidence 需要维度拆解 Observation")

        dimension_name = DIMENSION_DISPLAY_NAMES.get(observation.dimension, observation.dimension.value)
        parts = []
        for row in observation.normalized_rows:
            member = row.dimension_value
            values = []
            for metric, mv in row.metric_values.items():
                metric_name = METRIC_DISPLAY_NAMES.get(metric, metric.value)
                values.append(
                    f"{metric_name}由{mv.comparison_value}变为{mv.current_value}"
                )
            parts.append(f"{member}（{'，'.join(values)}）")
        statement = f"各{dimension_name}本期与对比期指标：{'；'.join(parts)}。"

        return Evidence(
            evidence_id=evidence_id,
            action_id=action.action_id,
            observation_ids=[observation.observation_id],
            calculation_ids=[],
            title=f"{dimension_name}拆解事实",
            statement=statement,
            metric=action.metrics[0],
            dimension=observation.dimension,
            member=None,
            direction=None,
        )


# ==================== 平均单件销售额证据 ====================

    def build_unit_price(
        self,
        evidence_id: str,
        action: Action,
        observation: Observation,
        calculation: UnitPriceCalculation,
    ) -> Evidence:
        """量额背离场景的平均单件销售额证据（只引用 UnitPriceCalculation 数值）。"""
        self._require_success(observation)
        if not isinstance(calculation, UnitPriceCalculation):
            raise ValueError("unit_price Evidence 必须引用 UnitPriceCalculation")
        self._require_linked(observation, calculation)

        current_label = action.current_period.label if action.current_period else "本期"
        comparison_label = action.comparison_period.label if action.comparison_period else "对比期"

        if calculation.current_unit_price is None or calculation.comparison_unit_price is None:
            statement = (
                f"{current_label}或{comparison_label}销售数量为0，"
                "无法计算平均单件销售额。"
            )
        else:
            change_part = ""
            if calculation.delta is not None:
                change_part = f"，变化{_fmt(calculation.delta)}"
            if calculation.change_rate is not None:
                change_part += f"，变化率约{_fmt(calculation.change_rate)}"
            statement = (
                f"平均单件销售额由{comparison_label}的{_fmt(calculation.comparison_unit_price)}"
                f"变为{current_label}的{_fmt(calculation.current_unit_price)}{change_part}。"
            )

        return Evidence(
            evidence_id=evidence_id,
            action_id=action.action_id,
            observation_ids=[observation.observation_id],
            calculation_ids=[calculation.calculation_id],
            title="平均单件销售额变化",
            statement=statement,
            metric=calculation.metric,
            dimension=None,
            member=None,
            direction=None,
        )
