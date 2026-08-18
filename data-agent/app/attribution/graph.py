"""Attribution Graph（Stage 5）。

依据冻结文档（SPEC §5.7 / §8 / §9、Stage 5 指令 §七/§八/§九/§十）：

节点原则保持：

```text
plan_next
validate_and_route_action
execute_query_action
normalize_observation
calculate
build_evidence
generate_report
```

主循环：

```text
START
→ plan_next
→ validate_and_route_action

query action
→ execute_query_action
→ normalize_observation
→ calculate
→ build_evidence
→ plan_next

calculate_contribution
→ calculate
→ build_evidence
→ plan_next

finish_analysis
→ generate_report
→ END
```

Graph 只负责编排，严禁在本模块复制 Normalizer 算法 / Calculator 公式 /
Action Router 规则 / Evidence Builder 逻辑（统一委托 Stage 4 冻结核心）。

查询 Action 执行规则（Stage 5 指令 §八）：

- 每个查询 Action 只调用一次 QueryService；
- 只有真正开始查询时 query_action_count + 1；
- duplicate / invalid Action 不增加；
- success Observation 重置 consecutive_empty_or_failed=0；empty/failed +1；
- 连续达到 2 或 query_action_count 达到 6 后禁止继续查询；
- 完整传递 consecutive_empty_or_failed / max_query_actions / actions /
  observations / calculations / evidences 给 ActionRouter。

自动 Calculation（Stage 5 指令 §九）：

- compare_period 成功后自动 PeriodChangeCalculation；
- breakdown_* 成功后若有对应目标指标总体变化 Calculation，自动
  ContributionCalculation；无则保留原始拆解事实（calculation_ids=[]）；
- analyze_unit_price 成功后自动 UnitPriceCalculation。

完成条件（Stage 5 指令 §十）：继续严格保持 Stage 4 冻结条件
（compare_period 总体比较证据链 + ≥2 维度成功 breakdown + ≥1 driver
Evidence + 未强制停止 + Planner 主动 finish）。
"""

from app.attribution.action_router import (
    ActionRouter,
    MAX_QUERY_ACTIONS,
    action_dedup_key,
    has_valid_evidence,
    is_force_stopped,
    is_query_action,
    next_consecutive,
)
from app.attribution.calculator import contribution, period_change, unit_price
from app.attribution.evidence_builder import EvidenceBuilder
from app.attribution.normalizer import Normalizer
from app.attribution.planner import Planner
from app.attribution.report_generator import ReportGenerator
from app.attribution.state import AttributionState
from app.core.log import logger
from app.models.analysis import (
    Action,
    ActionType,
    AnalysisStatus,
    Calculation,
    CalculationType,
    ContributionCalculation,
    Evidence,
    MetricKey,
    Observation,
    ObservationStatus,
    PeriodChangeCalculation,
    UnitPriceCalculation,
)

# 派生指标：由 Calculator 派生，不作为期间变化/贡献的独立计算来源
_DERIVED_METRIC = MetricKey.avg_unit_sales_amount


class AttributionGraph:
    """归因编排层：驱动 Planner → Router → QueryService → Normalizer
    → Calculator → EvidenceBuilder → ReportGenerator 主循环。

    run() 为异步生成器：yield 内部业务事件并原地更新 state；
    调用方（AnalysisService）负责 SSE 适配与安全兜底。
    """

    def __init__(
        self,
        query_service,
        router: ActionRouter | None = None,
        planner: Planner | None = None,
        report_generator: ReportGenerator | None = None,
        max_query_actions: int = MAX_QUERY_ACTIONS,
    ):
        self._query_service = query_service
        self._router = router if router is not None else ActionRouter()
        self._planner = planner if planner is not None else Planner()
        self._report_generator = report_generator if report_generator is not None else ReportGenerator()
        self._evidence_builder = EvidenceBuilder()
        self._max_query_actions = max_query_actions
        # 防御性循环上限：6 次查询 + 本地动作与重试余量，理论不可达
        self._max_loop_guard = max_query_actions * 2 + 8
        self._fallback_tried: set = set()

    # ==================== 主循环 ====================

    async def run(self, state: AttributionState):
        """驱动归因主循环；yield 内部业务事件；原地更新 state。"""
        self._fallback_tried = set()
        loop_guard = 0
        while True:
            loop_guard += 1
            if loop_guard > self._max_loop_guard:
                logger.error(f"归因循环超过防护上限，受控结束 analysis_id={state['analysis_id']}")
                for event in self._finish_with_report(state, reason="归因执行循环异常，已强制结束"):
                    yield event
                return

            # 强制停止检查（连续 empty/failed 或查询次数上限）
            if is_force_stopped(
                state["query_action_count"],
                state["consecutive_empty_or_failed"],
                state["max_query_actions"],
            ):
                reason = "已触发强制停止：查询次数达到上限或连续查询无有效数据"
                for event in self._finish_with_report(state, reason=reason):
                    yield event
                return

            # ---- plan_next 节点 ----
            action = self._plan_next(state)
            if action is None:
                for event in self._finish_with_report(
                    state,
                    reason="可执行动作路线已耗尽，无法完成全部归因拆解",
                ):
                    yield event
                return

            # ---- validate_and_route_action 节点（兜底校验 + 执行规格） ----
            validation_ok, spec = self._validate_and_route(state, action)
            if not validation_ok:
                # _plan_next 已放行，此处仅防御；不再循环，受控结束
                for event in self._finish_with_report(
                    state,
                    reason="动作最终校验未通过，归因受控结束",
                ):
                    yield event
                return

            # Action 合法：记录并广播 action_start
            state["actions"].append(action)
            # 正式 SSE 语义：查询 Action 的 query_action_count 必须包含当前
            # 已开始 Action（第一个=1、第六个=6）；本地 Action 不增加计数。
            # state 自增位置保持不变（真正开始查询时才 +1）。
            started_count = state["query_action_count"] + (
                1 if is_query_action(action.type) else 0
            )
            yield {
                "type": "action_start",
                "action": action,
                "query_action_count": started_count,
                "max_query_actions": state["max_query_actions"],
            }

            if is_query_action(action.type):
                # ---- execute_query_action + normalize_observation 节点 ----
                state["query_action_count"] += 1
                query_result = await self._query_service.execute(
                    spec.sub_query, spec.result_contract
                )
                observation = Normalizer.normalize(
                    observation_id=f"o{len(state['observations']) + 1}",
                    action=action,
                    sub_query=spec.sub_query,
                    query_result=query_result,
                    result_contract=spec.result_contract,
                )
                state["observations"].append(observation)
                state["consecutive_empty_or_failed"] = next_consecutive(
                    observation.status, state["consecutive_empty_or_failed"]
                )
                yield {
                    "type": "query_result",
                    "observation": observation,
                    "sub_query": spec.sub_query,
                }

                # ---- calculate + build_evidence 节点 ----
                new_calculations, new_evidences = self._calculate_and_build_evidence(
                    state, action, observation
                )
                if new_calculations or new_evidences:
                    yield {
                        "type": "calculation",
                        "action_id": action.action_id,
                        "calculations": new_calculations,
                        "evidences": new_evidences,
                    }
            elif action.type == ActionType.calculate_contribution:
                # ---- 本地贡献计算（不发起查询、不增加次数） ----
                new_calculations, new_evidences = self._calculate_and_build_evidence(
                    state, action, None
                )
                if new_calculations or new_evidences:
                    yield {
                        "type": "calculation",
                        "action_id": action.action_id,
                        "calculations": new_calculations,
                        "evidences": new_evidences,
                    }
            elif action.type == ActionType.finish_analysis:
                # ---- generate_report 节点（正常完成） ----
                state["status"] = AnalysisStatus.completed
                try:
                    report = self._report_generator.generate(state)
                except Exception as e:
                    # deterministic report 也失败 → REPORT_GENERATION_FAILED
                    logger.error(f"归因报告确定性生成失败：{e!r}")
                    state["status"] = AnalysisStatus.failed
                    state["failure_reason"] = "归因报告生成失败"
                    state["report"] = None
                    yield {"type": "report_failed"}
                    return
                state["report"] = report
                if report is not None:
                    yield {
                        "type": "report",
                        "report": report,
                        "evidences": state["evidences"],
                    }
                return

    # ==================== plan_next 节点 ====================

    def _plan_next(self, state: AttributionState) -> Action | None:
        """Planner 决策 + 校验反馈重试；仍失败进入确定性 fallback。

        retry 契约：schema/Pydantic 非法与 Router 状态非法共享同一个
        retry budget——一次 plan_next 决策周期最多调用 Planner LLM
        max_attempts（默认 2）次，不存在 2+2 嵌套。

        返回 None 表示动作路线耗尽（调用方受控结束）。
        """
        view = self._build_view(state)

        # 1. LLM 决策（validator 回调在 plan 内部共享 retry budget）
        action = self._planner.plan(
            view,
            validator=lambda candidate: self._validate(state, candidate),
        )
        if action is not None:
            return action

        # 2. 确定性 fallback（跳过已执行/已尝试；仍经过 Action Router）
        while True:
            candidate = self._planner.fallback_action(view, self._fallback_tried)
            if candidate is None:
                return None
            validation = self._validate(state, candidate)
            if validation.ok:
                return candidate
            self._fallback_tried.add(action_dedup_key(candidate))

    # ==================== validate_and_route_action 节点 ====================

    def _validate(self, state: AttributionState, action: Action):
        """完整传递 Stage 4 冻结状态参数给 ActionRouter（指令 §八）。"""
        return self._router.validate(
            action,
            seen_actions=state["actions"],
            query_action_count=state["query_action_count"],
            observations=state["observations"],
            calculations=state["calculations"],
            evidences=state["evidences"],
            consecutive_empty_or_failed=state["consecutive_empty_or_failed"],
            max_query_actions=state["max_query_actions"],
        )

    def _validate_and_route(self, state: AttributionState, action: Action):
        """正式校验并生成查询 Action 执行规格。

        返回 (validation_ok, spec)：
        - 校验失败 → (False, None)；
        - 查询 Action → (True, ActionExecutionSpec)；
        - 本地 Action（calculate_contribution / finish_analysis）→ (True, None)。
        """
        validation = self._validate(state, action)
        if not validation.ok:
            logger.warning(f"归因动作最终校验未通过：{validation.error_code} {validation.error_message}")
            return False, None
        if is_query_action(action.type):
            return True, self._router.execution_spec(action)
        return True, None

    # ==================== calculate + build_evidence 节点 ====================

    def _calculate_and_build_evidence(
        self,
        state: AttributionState,
        action: Action,
        observation: Observation | None,
    ) -> tuple[list[Calculation], list[Evidence]]:
        """calculate 节点 + build_evidence 节点（编排职责，算法委托冻结核心）。"""
        new_calculations = self._calculate(state, action, observation)
        new_evidences = self._build_evidence(state, action, new_calculations)
        state["calculations"].extend(new_calculations)
        state["evidences"].extend(new_evidences)
        return new_calculations, new_evidences

    @staticmethod
    def _find_period_change(
        calculations: list[Calculation], metric: MetricKey
    ) -> PeriodChangeCalculation | None:
        for calc in calculations:
            if isinstance(calc, PeriodChangeCalculation) and calc.metric == metric:
                return calc
        return None

    def _calculate(
        self,
        state: AttributionState,
        action: Action,
        observation: Observation | None,
    ) -> list[Calculation]:
        """自动计算（指令 §九）：不依赖 Planner 手工计算。"""
        new_calculations: list[Calculation] = []

        if action.type == ActionType.compare_period:
            if observation is None or observation.status != ObservationStatus.success:
                return new_calculations
            for metric in action.metrics:
                if metric == _DERIVED_METRIC:
                    continue
                try:
                    calc = period_change(
                        observation,
                        metric,
                        calculation_id=f"c_{CalculationType.period_change.value}_{metric.value}",
                    )
                except Exception as e:
                    logger.warning(f"期间变化计算失败 metric={metric.value} error={e!r}")
                    continue
                new_calculations.append(calc)

        elif action.type in (
            ActionType.breakdown_region,
            ActionType.breakdown_category,
            ActionType.breakdown_product,
            ActionType.breakdown_customer,
        ):
            if observation is None or observation.status != ObservationStatus.success:
                return new_calculations
            for metric in action.metrics:
                if metric == _DERIVED_METRIC:
                    continue
                total_calc = self._find_period_change(state["calculations"], metric)
                if total_calc is None:
                    # 总体变化尚不存在：不猜 total_delta，不生成伪 Contribution，
                    # 保留成功拆解事实（由 build_evidence 生成 raw breakdown）
                    continue
                try:
                    calc = contribution(
                        observation,
                        metric,
                        total_calc.delta,
                        calculation_id=(
                            f"c_{CalculationType.contribution.value}_{metric.value}_"
                            f"{action.dimension.value}_{observation.observation_id}"
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        f"贡献计算失败 metric={metric.value} dimension={action.dimension} error={e!r}"
                    )
                    continue
                new_calculations.append(calc)

        elif action.type == ActionType.analyze_unit_price:
            if observation is None or observation.status != ObservationStatus.success:
                return new_calculations
            try:
                calc = unit_price(
                    observation,
                    calculation_id=f"c_{CalculationType.unit_price.value}_sales",
                )
            except Exception as e:
                logger.warning(f"平均单件销售额计算失败 error={e!r}")
                return new_calculations
            new_calculations.append(calc)

        elif action.type == ActionType.calculate_contribution:
            # 本地贡献计算：只用于已有成功 breakdown Observation 尚未生成贡献的场景
            obs_by_id = {o.observation_id: o for o in state["observations"]}
            for oid in action.source_observation_ids:
                obs = obs_by_id.get(oid)
                if (
                    obs is None
                    or obs.status != ObservationStatus.success
                    or obs.dimension is None
                ):
                    continue
                metric = action.metrics[0]
                if metric == _DERIVED_METRIC:
                    continue
                total_calc = self._find_period_change(state["calculations"], metric)
                if total_calc is None:
                    continue
                try:
                    calc = contribution(
                        obs,
                        metric,
                        total_calc.delta,
                        calculation_id=(
                            f"c_{CalculationType.contribution.value}_{metric.value}_"
                            f"{obs.dimension.value}_{obs.observation_id}"
                        ),
                    )
                except Exception as e:
                    logger.warning(f"贡献计算失败（本地 Action） error={e!r}")
                    continue
                new_calculations.append(calc)

        return new_calculations

    def _build_evidence(
        self,
        state: AttributionState,
        action: Action,
        new_calculations: list[Calculation],
    ) -> list[Evidence]:
        """从本次计算构建 Evidence；无计算的成功拆解保留原始事实。"""
        obs_by_id = {o.observation_id: o for o in state["observations"]}
        new_evidences: list[Evidence] = []
        offset = len(state["evidences"])

        for calc in new_calculations:
            obs = obs_by_id.get(calc.source_observation_ids[0])
            if obs is None:
                continue
            if isinstance(calc, PeriodChangeCalculation):
                offset += 1
                new_evidences.append(
                    self._evidence_builder.build_period_change(
                        f"ev{offset}", action, obs, calc
                    )
                )
            elif isinstance(calc, ContributionCalculation):
                offset += 1
                new_evidences.extend(
                    self._evidence_builder.build_contribution_members(
                        f"ev{offset}", action, obs, calc
                    )
                )
            elif isinstance(calc, UnitPriceCalculation):
                offset += 1
                new_evidences.append(
                    self._evidence_builder.build_unit_price(
                        f"ev{offset}", action, obs, calc
                    )
                )

        # 无 Contribution 的成功拆解：保留原始拆解事实（calculation_ids=[]）
        if (
            action.type
            in (
                ActionType.breakdown_region,
                ActionType.breakdown_category,
                ActionType.breakdown_product,
                ActionType.breakdown_customer,
            )
            and not new_calculations
        ):
            observation = None
            for obs in reversed(state["observations"]):
                if obs.action_id == action.action_id:
                    observation = obs
                    break
            if observation is not None and observation.status == ObservationStatus.success:
                offset += 1
                new_evidences.append(
                    self._evidence_builder.build_raw_breakdown(
                        f"ev{offset}", action, observation
                    )
                )

        return new_evidences

    # ==================== generate_report 节点（强制停止 / 路线耗尽） ====================

    def _finish_with_report(self, state: AttributionState, *, reason: str) -> list[dict]:
        """受控结束：根据已有 Evidence 决定 partial / failed 并尽量生成报告。

        deterministic report 本身也抛异常时 → 状态置 failed 并返回
        report_failed 内部事件（调用方映射 REPORT_GENERATION_FAILED）。
        """
        has_evidence = has_valid_evidence(state["evidences"], state["observations"])
        state["status"] = AnalysisStatus.partial if has_evidence else AnalysisStatus.failed
        state["failure_reason"] = reason
        try:
            report = self._report_generator.generate(state)
        except Exception as e:
            logger.error(f"归因报告确定性生成失败：{e!r}")
            state["status"] = AnalysisStatus.failed
            state["failure_reason"] = "归因报告生成失败"
            state["report"] = None
            return [{"type": "report_failed"}]
        state["report"] = report
        if report is not None:
            return [
                {
                    "type": "report",
                    "report": report,
                    "evidences": state["evidences"],
                }
            ]
        return []

    # ==================== Planner 受限视图 ====================

    @staticmethod
    def _build_view(state: AttributionState) -> dict:
        """Planner 受限输入：不含 SQL、数据库凭证、Graph 内部召回状态。

        observations 使用只读轻量对象（属性访问与冻结 finish 纯函数兼容），
        只暴露 observation_id / action_id / status / dimension / normalized_rows。
        """
        return {
            "question": state["question"],
            "target": state["target"],
            "actions": state["actions"],
            "observations": [
                _ObservationView(
                    observation_id=obs.observation_id,
                    action_id=obs.action_id,
                    status=obs.status,
                    dimension=obs.dimension,
                    normalized_rows=obs.normalized_rows,
                )
                for obs in state["observations"]
            ],
            "calculations": state["calculations"],
            "evidences": state["evidences"],
            "query_action_count": state["query_action_count"],
            "consecutive_empty_or_failed": state["consecutive_empty_or_failed"],
            "max_query_actions": state["max_query_actions"],
        }


class _ObservationView:
    """Planner 视图中的只读 Observation 摘要（不含 query_result/SQL）。"""

    __slots__ = ("observation_id", "action_id", "status", "dimension", "normalized_rows")

    def __init__(self, observation_id, action_id, status, dimension, normalized_rows):
        self.observation_id = observation_id
        self.action_id = action_id
        self.status = status
        self.dimension = dimension
        self.normalized_rows = normalized_rows
