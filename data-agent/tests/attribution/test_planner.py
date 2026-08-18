"""Stage 5：Planner 测试。

覆盖（Stage 5 指令 §十五）：
- 合法单 Action；
- SQL 字段被拒绝（extra=forbid）；
- 非法第一次重试（重试成功）；
- 第二次非法进入 fallback（plan 返回 None → fallback_action 候选）；
- fallback 跳过已执行 Action；
- fallback 不死循环（tried_keys 耗尽返回 None）；
- calculate_contribution 被拒绝（由系统自动处理）；
- Planner 输入视图不含 SQL/凭证（由 Graph 构造，Planner 只读摘要）。
"""

from datetime import date

from app.attribution.planner import Planner
from app.models.analysis import (
    Action,
    ActionType,
    AttributionTarget,
    DimensionKey,
    MetricKey,
    Period,
)

JAN = Period(label="2025年1月", start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
FEB = Period(label="2025年2月", start_date=date(2025, 2, 1), end_date=date(2025, 2, 28))

TARGET = AttributionTarget(
    metrics=[MetricKey.sales_amount],
    current_period=FEB,
    comparison_period=JAN,
)


def _view(actions=None, question="为什么 2 月销售额下降？") -> dict:
    return {
        "question": question,
        "target": TARGET,
        "actions": actions or [],
        "observations": [],
        "calculations": [],
        "evidences": [],
        "query_action_count": 0,
        "max_query_actions": 6,
    }


class _FakeLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if self._outputs:
            return type("R", (), {"content": self._outputs.pop(0)})
        raise RuntimeError("no more outputs")


# ==================== 合法单 Action ====================


def test_plan_returns_single_legal_action():
    llm = _FakeLLM([
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "filters": [], "reason": "按区域拆解"}'
    ])
    action = Planner(llm=llm).plan(_view())
    assert action is not None
    assert action.type == ActionType.breakdown_region
    assert action.dimension == DimensionKey.region
    assert action.metrics == [MetricKey.sales_amount]
    assert action.current_period == FEB
    assert action.comparison_period == JAN
    assert action.action_id == "a1"
    assert llm.calls == 1


def test_plan_compare_period_defaults_to_target_metrics():
    llm = _FakeLLM([
        '{"type": "compare_period", "metrics": [], "reason": "总体比较"}'
    ])
    action = Planner(llm=llm).plan(_view())
    assert action is not None
    assert action.type == ActionType.compare_period
    assert action.metrics == [MetricKey.sales_amount]


def test_plan_analyze_unit_price_forces_metrics():
    llm = _FakeLLM([
        '{"type": "analyze_unit_price", "reason": "量额背离分析"}'
    ])
    action = Planner(llm=llm).plan(_view())
    assert action is not None
    assert action.type == ActionType.analyze_unit_price
    assert set(action.metrics) == {MetricKey.sales_amount, MetricKey.sales_quantity}


def test_plan_finish_action():
    llm = _FakeLLM(['{"type": "finish_analysis", "reason": "完成分析"}'])
    action = Planner(llm=llm).plan(_view())
    assert action is not None
    assert action.type == ActionType.finish_analysis


# ==================== SQL 字段被拒绝 ====================


def test_sql_field_in_action_rejected():
    """LLM 输出带 sql 字段 → extra=forbid 校验失败 → 重试 → 仍非法 → None。"""
    llm = _FakeLLM([
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "sql": "SELECT 1", "reason": "x"}',
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "sql": "SELECT 2", "reason": "x"}',
    ])
    planner = Planner(llm=llm)
    action = planner.plan(_view())
    assert action is None  # 第二次仍非法 → fallback 由调用方接管
    assert llm.calls == 2
    # fallback 给出确定性候选（compare_period）
    fb = planner.fallback_action(_view(), set())
    assert fb is not None
    assert fb.type == ActionType.compare_period


def test_dimension_mismatch_rejected():
    """breakdown_region 但 dimension=category → 结构校验失败 → 重试 → None。"""
    llm = _FakeLLM([
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "category", "reason": "x"}',
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "category", "reason": "x"}',
    ])
    assert Planner(llm=llm).plan(_view()) is None


# ==================== 非法第一次重试 ====================


def test_first_invalid_then_retry_success():
    """第一次非法（缺 reason），反馈后第二次合法。"""
    llm = _FakeLLM([
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region"}',
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "reason": "按区域拆解"}',
    ])
    action = Planner(llm=llm).plan(_view())
    assert action is not None
    assert action.type == ActionType.breakdown_region
    assert llm.calls == 2


def test_calculate_contribution_rejected():
    """calculate_contribution 由系统自动处理，Planner 输出即非法。"""
    llm = _FakeLLM([
        '{"type": "calculate_contribution", "metrics": ["sales_amount"], "source_observation_ids": ["o1"], "reason": "x"}',
        '{"type": "calculate_contribution", "metrics": ["sales_amount"], "source_observation_ids": ["o1"], "reason": "x"}',
    ])
    assert Planner(llm=llm).plan(_view()) is None


# ==================== fallback ====================


def test_fallback_skips_already_executed_actions():
    """fallback 跳过已执行动作：compare_period 已执行 → 直接返回 breakdown_region。"""
    executed = Action(
        action_id="fb_compare_period",
        type=ActionType.compare_period,
        metrics=[MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
        reason="总体比较",
    )
    planner = Planner(llm=_FakeLLM([]))
    candidate = planner.fallback_action(_view(actions=[executed]), set())
    assert candidate is not None
    assert candidate.type == ActionType.breakdown_region
    assert candidate.dimension == DimensionKey.region


def test_fallback_skips_tried_keys_no_dead_loop():
    """fallback 推进：tried_keys 累积后逐候选耗尽，最终返回 None（不死循环）。"""
    planner = Planner(llm=_FakeLLM([]))
    view = _view()
    tried: set = set()
    collected = []
    for _ in range(10):  # 上限保护，证明不会超过候选数
        candidate = planner.fallback_action(view, tried)
        if candidate is None:
            break
        collected.append(candidate.type)
        tried.add(_dedup_key(candidate))
    # 路线：compare + region + category + product + finish（单指标无 unit_price）
    assert [c.value for c in collected] == [
        ActionType.compare_period.value,
        ActionType.breakdown_region.value,
        ActionType.breakdown_category.value,
        ActionType.breakdown_product.value,
        ActionType.finish_analysis.value,
    ]
    # 再调用必须返回 None（路线耗尽）
    assert planner.fallback_action(view, tried) is None


def test_fallback_quantity_amount_divergence_inserts_unit_price():
    """量额背离 target → fallback 路线在 compare_period 后插入 analyze_unit_price。"""
    target = AttributionTarget(
        metrics=[MetricKey.sales_quantity, MetricKey.sales_amount],
        current_period=FEB,
        comparison_period=JAN,
    )
    planner = Planner(llm=_FakeLLM([]))
    view = {
        "question": "为什么 3 月数量增长但金额有限？",
        "target": target,
        "actions": [],
        "observations": [],
        "calculations": [],
        "evidences": [],
        "query_action_count": 0,
        "max_query_actions": 6,
    }
    tried: set = set()
    types = []
    for _ in range(10):
        candidate = planner.fallback_action(view, tried)
        if candidate is None:
            break
        types.append(candidate.type)
        tried.add(_dedup_key(candidate))
    assert types == [
        ActionType.compare_period,
        ActionType.analyze_unit_price,
        ActionType.breakdown_region,
        ActionType.breakdown_category,
        ActionType.breakdown_product,
        ActionType.finish_analysis,
    ]


def test_fallback_action_still_requires_router():
    """fallback 候选与 LLM 候选一样是 Action 对象，调用方仍会走 Action Router
    校验（此处验证候选可被去重键识别，不会绕过校验）。"""
    planner = Planner(llm=_FakeLLM([]))
    candidate = planner.fallback_action(_view(), set())
    assert isinstance(candidate, Action)
    assert candidate.reason  # reason 必填，Router 可校验


# ==================== retry budget 共享（schema + Router） ====================


def test_plan_llm_invoke_budget_shared_between_schema_and_router():
    """schema 与 Router 非法共享同一个 retry budget：LLM 总调用 <= 2。"""

    class _SeqLLM:
        def __init__(self, outputs):
            self._outputs = list(outputs)
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            return type("R", (), {"content": self._outputs.pop(0) if self._outputs else "{}"})

    llm = _SeqLLM([
        # 第一次：schema 非法（breakdown_region 但 dimension=category）
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "category", "reason": "x"}',
        # 第二次：结构合法但 Router 状态非法（validator 拒绝）
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "reason": "x"}',
    ])
    planner = Planner(llm=llm)

    def validator(action):
        from app.attribution.action_router import ActionValidation

        return ActionValidation.reject("ACTION_DUPLICATE", "相同 Action 已执行过，禁止重复查询")

    action = planner.plan(_view(), validator=validator)
    assert action is None  # 两次尝试都用完 → 调用方走确定性 fallback
    assert llm.calls == 2  # 总调用不超过 2，不存在 2+2 嵌套


def test_plan_router_reject_then_success_within_budget():
    """Router 拒绝一次后重试成功：总调用 2 次且返回合法 Action。"""

    class _SeqLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {"content": '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "reason": "x"}'})
            return type("R", (), {"content": '{"type": "breakdown_category", "metrics": ["sales_amount"], "dimension": "category", "reason": "x"}'})

    llm = _SeqLLM()
    planner = Planner(llm=llm)

    def validator(action):
        from app.attribution.action_router import ActionValidation

        if action.type == ActionType.breakdown_region:
            return ActionValidation.reject("ACTION_DUPLICATE", "该维度已拆解过")
        return ActionValidation.accept()

    action = planner.plan(_view(), validator=validator)
    assert action is not None
    assert action.type == ActionType.breakdown_category
    assert llm.calls == 2


# ==================== 指标硬校验（target 外指标拒绝） ====================


def test_plan_rejects_metric_outside_target():
    """Planner 不得把 AttributionTarget 之外的业务指标加入 Action。"""
    llm = _FakeLLM([
        '{"type": "compare_period", "metrics": ["order_count"], "reason": "x"}',
        '{"type": "compare_period", "metrics": ["order_count"], "reason": "x"}',
    ])
    # _view 的 target.metrics=[sales_amount]：order_count 不在目标范围 → 拒绝
    assert Planner(llm=llm).plan(_view()) is None
    assert llm.calls == 2


def test_plan_unit_price_metrics_exempt_from_target_check():
    """analyze_unit_price 指标由系统固定，不受 target 指标范围限制。"""
    llm = _FakeLLM(['{"type": "analyze_unit_price", "reason": "x"}'])
    action = Planner(llm=llm).plan(_view())  # target 只有 sales_amount
    assert action is not None
    assert action.type == ActionType.analyze_unit_price
    assert set(action.metrics) == {MetricKey.sales_amount, MetricKey.sales_quantity}


# ==================== 剩余次数不足的确定性 finish 短路 ====================


def test_plan_finish_shortcut_when_remaining_one_and_conditions_met():
    """remaining=1 且 finish 条件满足 → 确定性 finish，不调用 LLM。"""
    from app.attribution.graph import AttributionGraph
    from tests.attribution.test_report_generator import _build_state

    state = _build_state()  # compare + region + category 全部成功
    state["query_action_count"] = 5  # remaining = 1
    view = AttributionGraph._build_view(state)
    llm = _FakeLLM([])
    action = Planner(llm=llm).plan(view)
    assert action is not None
    assert action.type == ActionType.finish_analysis
    assert llm.calls == 0  # 未调用 LLM


def test_plan_no_shortcut_when_conditions_not_met():
    """remaining=1 但 finish 条件不满足（无证据）→ 走 LLM 决策。"""
    from app.attribution.graph import AttributionGraph
    from tests.attribution.test_report_generator import _build_state

    state = _build_state()
    state["query_action_count"] = 5
    state["evidences"] = []  # 破坏 finish 条件
    view = AttributionGraph._build_view(state)
    llm = _FakeLLM([
        '{"type": "breakdown_region", "metrics": ["sales_amount"], "dimension": "region", "reason": "补拆解"}'
    ])
    action = Planner(llm=llm).plan(view)
    assert action is not None
    assert action.type == ActionType.breakdown_region
    assert llm.calls == 1


def test_plan_no_shortcut_when_remaining_two():
    """remaining=2 → 不短路，交 LLM 决策。"""
    from app.attribution.graph import AttributionGraph
    from tests.attribution.test_report_generator import _build_state

    state = _build_state()
    state["query_action_count"] = 4  # remaining = 2
    view = AttributionGraph._build_view(state)
    llm = _FakeLLM([
        '{"type": "breakdown_product", "metrics": ["sales_amount"], "dimension": "product", "reason": "补产品拆解"}'
    ])
    action = Planner(llm=llm).plan(view)
    assert action is not None
    assert action.type == ActionType.breakdown_product
    assert llm.calls == 1


def _dedup_key(action: Action):
    from app.attribution.action_router import action_dedup_key

    return action_dedup_key(action)
