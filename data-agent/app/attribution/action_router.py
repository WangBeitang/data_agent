"""Action Router（Stage 4 归因确定性核心）。

依据冻结文档（数据对象设计 §9 / §16、SPEC §5.6 / §8）：

职责：
1. 查询 Action 判断（6 类查询 / 2 类本地）；
2. Action 去重（冻结逻辑键，action_id/reason 不参与）；
3. query_action_count 上限（最多 6 次查询 Action）；
4. Filter 白名单复核（模型层已封闭，此处提供一致性兜底判断）；
5. 生成受控 sub_query（确定性模板拼接，不调用 LLM、不生成 SQL）；
6. 生成内部 result_contract（Stage 2 已冻结的内部契约，不新增公开业务对象）；
7. 停止条件 / finish 条件的确定性判定（纯函数）。

停止/finish 判定直接实现为本模块的纯函数（不额外创建 stopping.py）。

本模块禁止：调用 LLM、生成 SQL、直接查询数据库。
"""

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.models.analysis import (
    Action,
    ActionType,
    AnalysisStatus,
    Calculation,
    ContributionCalculation,
    DimensionKey,
    Evidence,
    FactorDirection,
    FilterCondition,
    MetricKey,
    Observation,
    ObservationStatus,
    Period,
)

# 冻结的查询 Action 白名单：只有这 6 类增加查询次数
QUERY_ACTION_TYPES = frozenset(
    {
        ActionType.compare_period,
        ActionType.breakdown_region,
        ActionType.breakdown_category,
        ActionType.breakdown_product,
        ActionType.breakdown_customer,
        ActionType.analyze_unit_price,
    }
)

# 本地 Action：不发起查询、不计入 6 次上限
LOCAL_ACTION_TYPES = frozenset(
    {
        ActionType.calculate_contribution,
        ActionType.finish_analysis,
    }
)

# 第一版固定最大查询 Action 数
MAX_QUERY_ACTIONS = 6

# 连续 empty/failed 触发强制停止的阈值
MAX_CONSECUTIVE_EMPTY_OR_FAILED = 2

# 可展示的指标中文名（sub_query / Evidence statement 共用）
METRIC_DISPLAY_NAMES: dict[MetricKey, str] = {
    MetricKey.sales_amount: "销售额",
    MetricKey.sales_quantity: "销售数量",
    MetricKey.order_count: "销售订单数",
    MetricKey.avg_unit_sales_amount: "平均单件销售额",
}

# 可展示的维度中文名（sub_query filters 共用）
DIMENSION_DISPLAY_NAMES: dict[DimensionKey, str] = {
    DimensionKey.region: "销售区域",
    DimensionKey.category: "产品类别",
    DimensionKey.product: "产品",
    DimensionKey.customer: "客户",
    DimensionKey.customer_level: "客户等级",
}


# ==================== 1. 查询 Action 判断 ====================


def is_query_action(action_type: ActionType) -> bool:
    """是否为查询 Action（只有查询 Action 增加 query_action_count）。"""
    return action_type in QUERY_ACTION_TYPES


def is_local_action(action_type: ActionType) -> bool:
    """是否为本地 Action（calculate_contribution / finish_analysis）。"""
    return action_type in LOCAL_ACTION_TYPES


# ==================== 2. Action 去重 ====================

# 冻结逻辑键（数据对象设计 §9.3）：
# (type, metrics, current_period, comparison_period, dimension, normalized_filters)
# action_id / reason 不参与去重。


def _period_key(period: Period | None):
    if period is None:
        return None
    return (period.label, period.start_date.isoformat(), period.end_date.isoformat())


def _filter_key(condition: FilterCondition):
    # in values 顺序不影响逻辑键：values 排序后参与
    values = tuple(sorted(condition.values))
    return (condition.dimension.value, condition.operator.value, values)


def action_dedup_key(action: Action) -> tuple:
    """生成 Action 去重逻辑键。

    - metrics 按枚举值规范化（排序）；
    - filters 按 dimension/operator/values 规范化；
    - in values 顺序不影响逻辑键。
    """
    metrics = tuple(sorted(metric.value for metric in action.metrics))
    filters = tuple(sorted(_filter_key(f) for f in action.filters))
    return (
        action.type.value,
        metrics,
        _period_key(action.current_period),
        _period_key(action.comparison_period),
        action.dimension.value if action.dimension is not None else None,
        filters,
    )


def is_duplicate(action: Action, seen_actions: Sequence[Action]) -> bool:
    """查询 Action 是否与已执行 Action 重复（冻结逻辑键）。

    只对查询 Action 生效；本地 Action 由各自规则（贡献重复计算、
    finish 条件）校验，不参与本去重。
    """
    if not is_query_action(action.type):
        return False
    key = action_dedup_key(action)
    return any(action_dedup_key(seen) == key for seen in seen_actions)


# ==================== 3. 查询次数上限 ====================


def can_start_query(query_action_count: int, max_query_actions: int = MAX_QUERY_ACTIONS) -> bool:
    """是否允许继续发起查询 Action（严格小于上限）。"""
    return query_action_count < max_query_actions


# ==================== 4. 停止条件纯函数（SPEC §8） ====================


def next_consecutive(observation_status: ObservationStatus, current: int) -> int:
    """状态规则：success 重置为 0；empty/failed 加 1。"""
    if observation_status == ObservationStatus.success:
        return 0
    return current + 1


def is_force_stopped(
    query_action_count: int,
    consecutive_empty_or_failed: int,
    max_query_actions: int = MAX_QUERY_ACTIONS,
) -> bool:
    """强制停止：查询达到上限 OR 连续 empty/failed 达到阈值。"""
    if query_action_count >= max_query_actions:
        return True
    if consecutive_empty_or_failed >= MAX_CONSECUTIVE_EMPTY_OR_FAILED:
        return True
    return False


def forced_status(has_valid_evidence: bool) -> AnalysisStatus:
    """强制停止后的最终状态：有有效 Evidence → partial，否则 failed。

    达到查询上限不自动 completed。
    """
    return AnalysisStatus.partial if has_valid_evidence else AnalysisStatus.failed


def has_successful_overall_comparison(observations: Sequence[Observation]) -> bool:
    """是否存在成功总体比较 Observation（dimension=null 且 success）。"""
    return any(
        obs.status == ObservationStatus.success and obs.dimension is None
        for obs in observations
    )


def successful_breakdown_dimensions(observations: Sequence[Observation]) -> set[DimensionKey]:
    """成功拆解 Observation 覆盖的维度集合。"""
    return {
        obs.dimension
        for obs in observations
        if obs.status == ObservationStatus.success and obs.dimension is not None
    }


def has_driver_evidence(evidences: Sequence[Evidence]) -> bool:
    """是否存在 direction=driver 的 Evidence。"""
    return any(evidence.direction == FactorDirection.driver for evidence in evidences)


def has_valid_evidence(evidences: Sequence[Evidence], observations: Sequence[Observation]) -> bool:
    """是否存在至少一条引用成功 Observation 的 Evidence。"""
    success_ids = {obs.observation_id for obs in observations if obs.status == ObservationStatus.success}
    return any(set(evidence.observation_ids) & success_ids for evidence in evidences)


def can_finish(
    *,
    has_overall_comparison: bool,
    breakdown_dimensions: Iterable[DimensionKey],
    has_driver_evidence: bool,
    forced_stopped: bool,
) -> bool:
    """正常 finish 最低条件（数据对象设计 §13.2 / SPEC §8.2）：

    1. 有成功总体比较；
    2. 至少两个不同 dimension 的成功 breakdown；
    3. 至少一个 driver Evidence；
    4. 不是连续失败触发的强制结束。

    后续由 Stage 5 Planner 决定是否主动选择 finish_analysis。
    """
    if forced_stopped:
        return False
    if not has_overall_comparison:
        return False
    if len(set(breakdown_dimensions)) < 2:
        return False
    if not has_driver_evidence:
        return False
    return True


def finish_conditions_met(
    observations: Sequence[Observation],
    evidences: Sequence[Evidence],
    forced_stopped: bool,
) -> bool:
    """基于当前状态便捷判定正常 finish 条件是否满足。"""
    return can_finish(
        has_overall_comparison=has_successful_overall_comparison(observations),
        breakdown_dimensions=successful_breakdown_dimensions(observations),
        has_driver_evidence=has_driver_evidence(evidences),
        forced_stopped=forced_stopped,
    )


# ==================== 5. Filter 白名单复核 ====================


def validate_filters(filters: Sequence[FilterCondition]) -> str | None:
    """Filter 白名单一致性复核（模型层已封闭结构，此处兜底）。

    返回错误说明；合法返回 None。不允许 where_sql / sql / 任意表达式——
    模型字段本身不存在这些入口，这里复核运算符与维度枚举。
    """
    for condition in filters:
        if condition.operator not in (condition.operator.eq, condition.operator.in_):
            return "Filter 运算符不在白名单 (eq|in)"
        if condition.dimension not in DimensionKey:
            return "Filter 维度不在白名单"
        if not condition.values:
            return "Filter 必须至少 1 个值"
    return None


# ==================== 6. sub_query 确定性模板（SPEC §5.6） ====================


def _metric_names(metrics: Sequence[MetricKey]) -> str:
    return "、".join(METRIC_DISPLAY_NAMES[m] for m in metrics)


def _filter_text(condition: FilterCondition) -> str:
    dim_name = DIMENSION_DISPLAY_NAMES[condition.dimension]
    if condition.operator == condition.operator.eq:
        return f"{dim_name}为「{condition.values[0]}」"
    return f"{dim_name}属于「{'、'.join(condition.values)}」"


def build_sub_query(action: Action) -> str:
    """确定性模板拼接 sub_query；禁止把用户输入直接拼成 SQL。"""
    comparison_label = action.comparison_period.label if action.comparison_period else "对比期"
    current_label = action.current_period.label if action.current_period else "本期"
    t = action.type

    if t == ActionType.compare_period:
        return f"分别统计{comparison_label}和{current_label}的{_metric_names(action.metrics)}。"
    if t == ActionType.breakdown_region:
        return f"分别统计{comparison_label}和{current_label}各销售区域的{_metric_names(action.metrics)}。"
    if t == ActionType.breakdown_category:
        return f"分别统计{comparison_label}和{current_label}各产品类别的{_metric_names(action.metrics)}。"
    if t == ActionType.breakdown_product:
        text = f"分别统计{comparison_label}和{current_label}各产品的{_metric_names(action.metrics)}"
        if action.filters:
            text += "，并限定" + "、".join(_filter_text(f) for f in action.filters)
        return text + "。"
    if t == ActionType.breakdown_customer:
        return f"分别统计{comparison_label}和{current_label}各客户/客户等级的{_metric_names(action.metrics)}。"
    if t == ActionType.analyze_unit_price:
        return f"分别统计{comparison_label}和{current_label}的销售额和销售数量。"
    # 本地 Action（calculate_contribution / finish_analysis）不产生查询子问题
    raise ValueError(f"{t.value} 不是查询 Action，无法生成 sub_query")


# ==================== 7. result_contract（SPEC §3.6） ====================


def build_result_contract(action: Action) -> dict:
    """生成内部 result_contract（不新增公开业务对象）。

    总体比较：dimension_alias=null；维度拆解：dimension_alias="dimension_value"。
    多 metric 时按实际指标增加 aliases；analyze_unit_price 至少包含
    sales_amount 与 sales_quantity。
    """
    contract: dict = {
        "period_alias": "period_key",
        "metric_aliases": {metric.value: metric.value for metric in action.metrics},
        "period_values": ["comparison", "current"],
    }
    contract["dimension_alias"] = "dimension_value" if action.dimension is not None else None
    return contract


# ==================== 8. 执行规格与状态化校验 ====================


@dataclass(frozen=True)
class ActionExecutionSpec:
    """查询 Action 的执行规格：受控子问题 + 内部契约。"""

    action: Action
    sub_query: str
    result_contract: dict


@dataclass(frozen=True)
class ActionValidation:
    """Action 校验结果（状态相关校验，模型层之外）。"""

    ok: bool
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def reject(cls, code: str, message: str) -> "ActionValidation":
        return cls(ok=False, error_code=code, error_message=message)

    @classmethod
    def accept(cls) -> "ActionValidation":
        return cls(ok=True)


class ActionRouter:
    """Action 状态化校验 + 执行规格生成（不调用 LLM、不生成 SQL）。"""

    def validate(
        self,
        action: Action,
        *,
        seen_actions: Sequence[Action],
        query_action_count: int,
        observations: Sequence[Observation],
        calculations: Sequence[Calculation],
        evidences: Sequence[Evidence],
        consecutive_empty_or_failed: int = 0,
        max_query_actions: int = MAX_QUERY_ACTIONS,
    ) -> ActionValidation:
        """校验 Action 在当前状态下的合法性。

        模型层已保证 Action 结构与类型条件合法（白名单第一层），
        此处只做依赖状态判定：

        - 查询 Action：上限（query_action_count >= 6 禁止再产生查询执行规格）、
          连续 empty/failed 强制停止、去重；
        - finish_analysis：正常 finish 最低条件（premature finish 拒绝）；
        - calculate_contribution：只能引用成功 breakdown Observation，
          且不得对同一 Observation+metric 重复计算。
        """
        t = action.type

        if is_query_action(t):
            if query_action_count >= max_query_actions:
                return ActionValidation.reject(
                    "ACTION_LIMIT_REACHED",
                    f"查询 Action 已达上限（{max_query_actions}），禁止继续发起查询",
                )
            if consecutive_empty_or_failed >= MAX_CONSECUTIVE_EMPTY_OR_FAILED:
                return ActionValidation.reject(
                    "FORCED_STOP",
                    "连续两次查询为空或失败，禁止继续发起查询",
                )
            if is_duplicate(action, seen_actions):
                return ActionValidation.reject("ACTION_DUPLICATE", "相同 Action 已执行过，禁止重复查询")
            filter_error = validate_filters(action.filters)
            if filter_error is not None:
                return ActionValidation.reject("INVALID_FILTER", filter_error)
            return ActionValidation.accept()

        if t == ActionType.calculate_contribution:
            return self._validate_contribution(action, observations, calculations)

        if t == ActionType.finish_analysis:
            forced_stopped = is_force_stopped(
                query_action_count, consecutive_empty_or_failed, max_query_actions
            )
            # finish 判定基于当前状态；正常 finish 要求非强制停止
            if not finish_conditions_met(observations, evidences, forced_stopped=forced_stopped):
                return ActionValidation.reject(
                    "PREMATURE_FINISH",
                    "正常 finish 最低条件未满足（需要成功总体比较、至少两个维度拆解、存在 driver 证据，且未触发强制停止）",
                )
            return ActionValidation.accept()

        # ActionType 枚举封闭，理论不可达
        return ActionValidation.reject("PLANNER_INVALID_ACTION", "未知 Action 类型")

    @staticmethod
    def _validate_contribution(
        action: Action,
        observations: Sequence[Observation],
        calculations: Sequence[Calculation],
    ) -> ActionValidation:
        obs_by_id = {obs.observation_id: obs for obs in observations}
        for oid in action.source_observation_ids:
            obs = obs_by_id.get(oid)
            if obs is None:
                return ActionValidation.reject(
                    "INVALID_CONTRIBUTION_SOURCE", f"source Observation 不存在：{oid}"
                )
            if obs.status != ObservationStatus.success:
                return ActionValidation.reject(
                    "INVALID_CONTRIBUTION_SOURCE", f"calculate_contribution 只能引用成功 Observation：{oid}"
                )
            if obs.dimension is None:
                return ActionValidation.reject(
                    "INVALID_CONTRIBUTION_SOURCE", f"calculate_contribution 只能引用维度拆解 Observation：{oid}"
                )
        target_metric = action.metrics[0]
        source_ids = set(action.source_observation_ids)
        for calc in calculations:
            if (
                isinstance(calc, ContributionCalculation)
                and calc.metric == target_metric
                and set(calc.source_observation_ids) & source_ids
            ):
                return ActionValidation.reject(
                    "CONTRIBUTION_ALREADY_CALCULATED",
                    "该 breakdown Observation 已生成贡献计算，禁止重复计算",
                )
        return ActionValidation.accept()

    def execution_spec(self, action: Action) -> ActionExecutionSpec:
        """生成查询 Action 的执行规格（sub_query + result_contract）。

        仅允许查询 Action；本地 Action 不产生查询规格。
        """
        if not is_query_action(action.type):
            raise ValueError(f"{action.type.value} 不是查询 Action，无法生成执行规格")
        return ActionExecutionSpec(
            action=action,
            sub_query=build_sub_query(action),
            result_contract=build_result_contract(action),
        )
