"""Planner（Stage 5）。

依据冻结文档（SPEC §5.5 / §8 / §9、Stage 5 指令 §六）：

```text
AttributionState → 一个 Action
```

Planner 每次只决策「一个」下一步 Action。

输入只允许包含（由调用方构造受限视图）：

- question
- target
- 已执行 Action 摘要
- Observation 状态与 normalized_rows 摘要
- Calculations
- Evidences
- 剩余查询次数

不得向 Planner 提供：

- 数据库凭证
- Graph 内部召回状态
- 隐藏推理
- Prompt 内部信息
- SQL

非法 Action 处理：

1. 第一次非法（Pydantic/结构校验失败）→ 将简洁错误反馈给 Planner → 重试一次；
2. 第二次仍非法 → 返回 None，由调用方使用确定性 fallback 路线：

```text
compare_period
→ breakdown_region
→ breakdown_category
→ breakdown_product
→ finish_analysis
```

fallback_action 提供路线候选：跳过已执行动作；是否放行由调用方
（Action Router）最终校验；路线耗尽返回 None 时由调用方受控结束。

本模块禁止：生成 SQL、心算变化率/贡献率/平均单件销售额、
编造 Observation/Evidence、访问数据库。
"""

import json

from pydantic import BaseModel, ConfigDict, Field

from app.agent import llm as llm_module
from app.attribution.action_router import (
    DIMENSION_DISPLAY_NAMES,
    METRIC_DISPLAY_NAMES,
    action_dedup_key,
    validate_filters,
)
from app.core.log import logger
from app.models.analysis import (
    Action,
    ActionType,
    DimensionKey,
    FilterCondition,
    MetricKey,
)
from app.prompt.prompt_loader import load_prompt

# 冻结 fallback 路线：compare_period → breakdown_region → breakdown_category
# → breakdown_product → finish_analysis（量额背离时在 compare_period 后插入
# analyze_unit_price，保证冻结验收场景可复算平均单件销售额）
FALLBACK_ROUTE = (
    ActionType.compare_period,
    ActionType.breakdown_region,
    ActionType.breakdown_category,
    ActionType.breakdown_product,
    ActionType.finish_analysis,
)

# analyze_unit_price 固定指标（Calculator 派生平均单件销售额所需）
_UNIT_PRICE_METRICS = [MetricKey.sales_amount, MetricKey.sales_quantity]


class _LLMAction(BaseModel):
    """Planner LLM 输出结构化校验模型（不对外暴露）。

    extra="forbid"：拒绝 SQL / 隐藏推理等一切多余字段。
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    metrics: list[MetricKey] = Field(default_factory=list)
    dimension: DimensionKey | None = None
    filters: list[FilterCondition] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=200)


class Planner:
    """归因规划器：LLM 单 Action 决策 + 确定性 fallback。"""

    def __init__(self, llm=None, max_attempts: int = 2):
        self._llm = llm if llm is not None else llm_module.llm
        self._max_attempts = max_attempts
        self._prompt = load_prompt("attribution_planner")

    # ==================== LLM 决策 ====================

    def plan(self, state_view: dict, feedback: str | None = None) -> Action | None:
        """LLM 生成单个 Action；非法时反馈重试一次；仍非法返回 None。

        - state_view：受限输入视图（由调用方构造，不得包含 SQL/凭证）；
        - feedback：上一次校验失败的简洁错误说明（可为 None）；
        - 返回 None 表示 LLM 连续非法，调用方应进入确定性 fallback。

        确定性短路：剩余查询次数不足（≤1）且正常 finish 条件已满足时，
        直接返回 finish_analysis（尊重剩余查询次数，防止 LLM 用光次数）。
        """
        finish = self._maybe_finish(state_view)
        if finish is not None:
            return finish

        for attempt in range(1, self._max_attempts + 1):
            prompt = self._build_prompt(state_view, feedback)
            try:
                raw = self._llm.invoke(prompt)
                text = raw.content if hasattr(raw, "content") else str(raw)
                action, error = self._parse_action(text, state_view)
            except Exception as e:  # LLM 调用异常也视为一次失败
                logger.warning(f"Planner LLM 调用异常 attempt={attempt} error={e!r}")
                action, error = None, "LLM 调用异常，请重新输出合法的 Action JSON"
            if action is not None:
                return action
            logger.warning(f"Planner 输出非法 attempt={attempt}：{error}")
            feedback = error
        logger.warning("Planner 连续非法，进入确定性 fallback")
        return None

    @staticmethod
    def _maybe_finish(state_view: dict) -> Action | None:
        """剩余次数不足且 finish 条件已满足 → 确定性 finish（防止用光次数）。"""
        remaining = state_view["max_query_actions"] - state_view["query_action_count"]
        if remaining > 1:
            return None
        # 复用 Stage 4 冻结 finish 判定纯函数（不复制规则）
        from app.attribution.action_router import (
            finish_conditions_met,
            is_force_stopped,
        )

        forced = is_force_stopped(
            state_view["query_action_count"],
            state_view["consecutive_empty_or_failed"],
            state_view["max_query_actions"],
        )
        if finish_conditions_met(
            state_view["actions"],
            state_view["observations"],
            state_view["evidences"],
            forced_stopped=forced,
        ):
            return Action(
                action_id=f"a{len(state_view['actions']) + 1}",
                type=ActionType.finish_analysis,
                reason="剩余查询次数不足，已完成必要的总体比较与多维度拆解证据",
            )
        return None

    def _parse_action(self, text: str, state_view: dict) -> tuple[Action | None, str]:
        """LLM 文本 → Action；返回 (action, 简洁错误)。"""
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
            return None, "输出不是合法 JSON 对象，请只输出 Action JSON"

        try:
            llm_action = _LLMAction(**data)
        except Exception as e:
            return None, f"Action 字段校验失败：{_first_error(e)}"

        if llm_action.type == ActionType.calculate_contribution:
            return None, "calculate_contribution 由系统自动处理，请选择查询动作或 finish_analysis"

        error = self._validate_structure(llm_action)
        if error is not None:
            return None, error

        action = self._to_action(llm_action, state_view)
        try:
            action.model_dump()  # 触发模型层条件校验（白名单第一层）
        except Exception as e:
            return None, f"Action 类型条件校验失败：{_first_error(e)}"
        return action, ""

    @staticmethod
    def _validate_structure(llm_action: _LLMAction) -> str | None:
        """type 与 dimension/metrics 的结构一致性校验。

        compare_period / breakdown_* 允许 metrics 为空（_to_action 默认
        沿用 target.metrics）；analyze_unit_price 指标由系统固定。
        """
        t = llm_action.type
        dim = llm_action.dimension
        if t == ActionType.compare_period:
            if dim is not None:
                return "compare_period 不允许设置 dimension，请设为 null"
        elif t in (
            ActionType.breakdown_region,
            ActionType.breakdown_category,
            ActionType.breakdown_product,
            ActionType.breakdown_customer,
        ):
            expected = {
                ActionType.breakdown_region: DimensionKey.region,
                ActionType.breakdown_category: DimensionKey.category,
                ActionType.breakdown_product: DimensionKey.product,
            }.get(t)
            if expected is not None and dim != expected:
                return f"{t.value} 的 dimension 必须为 {expected.value}"
            if t == ActionType.breakdown_customer and dim not in (
                DimensionKey.customer,
                DimensionKey.customer_level,
            ):
                return "breakdown_customer 的 dimension 只能是 customer 或 customer_level"
        elif t == ActionType.analyze_unit_price:
            if dim is not None:
                return "analyze_unit_price 不允许设置 dimension，请设为 null"
            # 指标由系统固定为 sales_amount + sales_quantity，无需 LLM 指定
        elif t == ActionType.finish_analysis:
            if dim is not None or llm_action.metrics:
                return "finish_analysis 不携带指标与维度"
        filter_error = validate_filters(llm_action.filters)
        if filter_error is not None:
            return filter_error
        return None

    @staticmethod
    def _to_action(llm_action: _LLMAction, state_view: dict) -> Action:
        """_LLMAction → 正式 Action（期间沿用 target，action_id 由后端生成）。"""
        target = state_view["target"]
        action_id = f"a{len(state_view['actions']) + 1}"

        if llm_action.type == ActionType.finish_analysis:
            return Action(action_id=action_id, type=ActionType.finish_analysis, reason=llm_action.reason)

        if llm_action.type == ActionType.analyze_unit_price:
            metrics = _UNIT_PRICE_METRICS
        elif llm_action.type == ActionType.compare_period:
            metrics = llm_action.metrics or target.metrics
        else:
            metrics = llm_action.metrics or target.metrics

        return Action(
            action_id=action_id,
            type=llm_action.type,
            metrics=metrics,
            current_period=target.current_period,
            comparison_period=target.comparison_period,
            dimension=llm_action.dimension,
            filters=llm_action.filters,
            source_observation_ids=llm_action.source_observation_ids,
            reason=llm_action.reason,
        )

    # ==================== 确定性 fallback ====================

    def fallback_action(self, state_view: dict, tried_keys: set) -> Action | None:
        """冻结 fallback 路线下一个未执行且未尝试的动作；耗尽返回 None。

        - 跳过已执行动作（seen actions 逻辑键）；
        - 跳过已尝试动作（tried_keys，防止死循环）；
        - 是否放行仍由 Action Router 最终校验，本方法不绕过。
        """
        target = state_view["target"]
        seen_keys = {action_dedup_key(action) for action in state_view["actions"]}
        for candidate in self._build_fallback_candidates(target):
            key = action_dedup_key(candidate)
            if key in seen_keys or key in tried_keys:
                continue
            return candidate
        return None

    def _build_fallback_candidates(self, target) -> list[Action]:
        """构造 fallback 路线候选（量额背离时插入 analyze_unit_price）。"""
        candidates: list[Action] = [
            Action(
                action_id="fb_compare_period",
                type=ActionType.compare_period,
                metrics=target.metrics,
                current_period=target.current_period,
                comparison_period=target.comparison_period,
                reason="比较本期与对比期的总体指标变化",
            )
        ]
        if _is_quantity_amount_divergence(target.metrics):
            candidates.append(
                Action(
                    action_id="fb_unit_price",
                    type=ActionType.analyze_unit_price,
                    metrics=_UNIT_PRICE_METRICS,
                    current_period=target.current_period,
                    comparison_period=target.comparison_period,
                    reason="分析量额背离下的平均单件销售额变化",
                )
            )
        for action_type, dimension in (
            (ActionType.breakdown_region, DimensionKey.region),
            (ActionType.breakdown_category, DimensionKey.category),
            (ActionType.breakdown_product, DimensionKey.product),
        ):
            candidates.append(
                Action(
                    action_id=f"fb_{action_type.value}",
                    type=action_type,
                    metrics=target.metrics,
                    current_period=target.current_period,
                    comparison_period=target.comparison_period,
                    dimension=dimension,
                    reason=f"按{DIMENSION_DISPLAY_NAMES[dimension]}拆解指标变化",
                )
            )
        candidates.append(
            Action(
                action_id="fb_finish",
                type=ActionType.finish_analysis,
                reason="已完成规定路线的归因分析",
            )
        )
        return candidates

    # ==================== Prompt 构造 ====================

    def _build_prompt(self, state_view: dict, feedback: str | None) -> str:
        prompt = self._prompt
        prompt = prompt.replace("{question}", state_view["question"])
        prompt = prompt.replace("{target}", json.dumps(_target_summary(state_view["target"]), ensure_ascii=False))
        prompt = prompt.replace("{actions_summary}", _actions_summary(state_view["actions"]))
        prompt = prompt.replace("{observations_summary}", _observations_summary(state_view["observations"]))
        prompt = prompt.replace("{calculations_summary}", _calculations_summary(state_view["calculations"]))
        prompt = prompt.replace("{evidences_summary}", _evidences_summary(state_view["evidences"]))
        remaining = state_view["max_query_actions"] - state_view["query_action_count"]
        prompt = prompt.replace("{remaining_queries}", str(max(remaining, 0)))
        if feedback:
            prompt = prompt.replace(
                "{feedback_section}",
                f"\n【上次校验反馈】（必须遵守，重新输出一个合法 Action）\n{feedback}\n",
            )
        else:
            prompt = prompt.replace("{feedback_section}", "")
        return prompt


# ==================== 受限视图序列化（不包含 SQL / 凭证 / 隐藏推理） ====================


def _target_summary(target) -> dict:
    return {
        "metrics": [metric.value for metric in target.metrics],
        "current_period": _period_summary(target.current_period),
        "comparison_period": _period_summary(target.comparison_period),
    }


def _period_summary(period) -> dict | None:
    if period is None:
        return None
    return {
        "label": period.label,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
    }


def _actions_summary(actions) -> str:
    if not actions:
        return "（无）"
    lines = []
    for action in actions:
        parts = [action.type.value]
        if action.metrics:
            parts.append("指标=" + ",".join(METRIC_DISPLAY_NAMES[m] for m in action.metrics))
        if action.dimension is not None:
            parts.append("维度=" + DIMENSION_DISPLAY_NAMES[action.dimension])
        lines.append(f"- {action.action_id}：{'，'.join(parts)}（已执行）")
    return "\n".join(lines)


def _observations_summary(observations) -> str:
    """Observation 摘要（接收 Graph 构造的受限 _ObservationView，不含 SQL）。"""
    if not observations:
        return "（无）"
    lines = []
    for obs in observations:
        status = obs.status.value if hasattr(obs.status, "value") else obs.status
        if status != "success":
            lines.append(f"- {obs.observation_id}（action={obs.action_id}）：状态={status}")
            continue
        dim_text = "总体" if obs.dimension is None else DIMENSION_DISPLAY_NAMES[obs.dimension]
        row_texts = []
        for row in obs.normalized_rows[:5]:
            member = "总体" if row.dimension_value is None else row.dimension_value
            values = []
            for metric, mv in row.metric_values.items():
                values.append(
                    f"{METRIC_DISPLAY_NAMES.get(metric, metric.value)}={mv.comparison_value}→{mv.current_value}"
                )
            row_texts.append(f"{member}（{'，'.join(values)}）")
        lines.append(f"- {obs.observation_id}（action={obs.action_id}，{dim_text}）：{'；'.join(row_texts)}")
    return "\n".join(lines)


def _calculations_summary(calculations) -> str:
    if not calculations:
        return "（无）"
    lines = []
    for calc in calculations:
        base = f"- {calc.type.value} metric={METRIC_DISPLAY_NAMES.get(calc.metric, calc.metric.value)}"
        if calc.type.value == "period_change":
            lines.append(f"{base} delta={calc.delta} change_rate={calc.change_rate}")
        elif calc.type.value == "contribution":
            lines.append(f"{base} dimension={DIMENSION_DISPLAY_NAMES[calc.dimension]} total_delta={calc.total_delta}")
        else:  # unit_price
            lines.append(
                f"{base} 平均单件销售额 {calc.comparison_unit_price}→{calc.current_unit_price}"
            )
    return "\n".join(lines)


def _evidences_summary(evidences) -> str:
    if not evidences:
        return "（无）"
    return "\n".join(f"- {ev.title}：{ev.statement}" for ev in evidences)


def _is_quantity_amount_divergence(metrics) -> bool:
    return {MetricKey.sales_quantity, MetricKey.sales_amount}.issubset(set(metrics))


def _first_error(exc: Exception) -> str:
    """从 Pydantic ValidationError 中提取第一条错误信息（简洁反馈）。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            errs = errors()
            if errs:
                loc = ".".join(str(x) for x in errs[0].get("loc", ()))
                msg = errs[0].get("msg", "")
                return f"{loc}: {msg}" if loc else msg
        except Exception:
            pass
    return str(exc).splitlines()[0] if str(exc) else "未知校验错误"
