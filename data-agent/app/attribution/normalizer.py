"""Normalizer（Stage 4 归因确定性核心）。

依据冻结文档（数据对象设计 §10 / SPEC §3.6 / §5.7）：

```text
Action + QueryExecutionResult + result_contract → Observation / normalized_rows
```

硬约束：

- 不调用 LLM、不访问数据库、不计算变化率/贡献率；
- 不猜列、不做模糊 alias 匹配，只接受 result_contract 指定列；
- period_key 只允许 comparison / current；
- 维度成员只在一个期间出现时，缺失期间值按 0 处理（"该成员该期间无销售"）；
- QueryExecutionResult.status=empty → Observation.status=empty，normalized_rows=[]；
- Provider failed → Observation.status=failed，原样保留 QueryExecutionResult；
- contract 不匹配（缺 period_key / dimension_value / metric alias，
  或 period value 非法）→ Observation.status=failed，error 使用
  RESULT_NORMALIZATION_FAILED 安全语义；禁止猜测列名兜底。
"""

from app.models.analysis import (
    Action,
    ComparisonRow,
    DimensionKey,
    MetricKey,
    MetricPeriodValue,
    Observation,
    ObservationStatus,
    QueryExecutionResult,
)

# 冻结的期间键白名单
_ALLOWED_PERIODS = frozenset({"comparison", "current"})

# RESULT_NORMALIZATION_FAILED 安全语义前缀（API 接口设计 §13 错误码）
_RESULT_NORMALIZATION_FAILED = "查询结果归一化失败（RESULT_NORMALIZATION_FAILED）"

# Provider 失败时的安全兜底信息（详细异常只写服务端日志）
_PROVIDER_FAILED_MESSAGE = "查询执行失败，无法获得有效数据。"

# 非法值哨兵
_INVALID = object()


def validate_contract_result(
    result_columns: list[str],
    result_rows: list[dict],
    result_contract: dict | None,
) -> str | None:
    """硬执行契约：检查 SQL 输出列与值是否满足 result_contract（Stage 7）。

    返回失败原因（str）或 None（满足）。本函数与 Normalizer 的严格校验
    保持同一语义，作为 QueryService / DataAgent Graph 边界的硬契约检查，
    不调用 LLM、不猜列。

    校验范围（与 Normalizer._validate_contract / _normalize_rows 一致）：

    - 输出列必须满足 period_alias；
    - breakdown 必须满足 dimension_alias；
    - 所有 metric_aliases 必须存在；
    - period_alias 的实际值只能来自 period_values（且 ∈ {comparison, current}）；
    - breakdown dimension value 必须有效（非空字符串）；
    - 指标值必须保持可归一化数值语义（int/float，禁止 bool/str/None）；
    - 维度成员在同一期间不得出现重复行；
    - 列名缺失 / 值非法即失败，不猜测兜底。

    注意：result_rows 为空但列名符合契约 → 返回 None（空结果是合法 empty，
    不触发修复）；只有 SQL 语法正确但输出偏离契约才触发内部修复。
    """
    if result_contract is None:
        return None

    if not isinstance(result_contract, dict):
        return "result_contract 必须是 dict"
    period_alias = result_contract.get("period_alias")
    if not period_alias:
        return "契约缺少 period_alias"
    metric_aliases = result_contract.get("metric_aliases")
    if not isinstance(metric_aliases, dict) or not metric_aliases:
        return "契约缺少 metric_aliases"
    period_values = result_contract.get("period_values")
    if not isinstance(period_values, list) or not period_values:
        return "契约缺少 period_values"
    dimension_alias = result_contract.get("dimension_alias")
    need_dimension = dimension_alias is not None

    columns = list(result_columns or [])
    if period_alias not in columns:
        return f"结果缺少契约列 {period_alias!r}"
    if need_dimension and dimension_alias not in columns:
        return f"结果缺少契约列 {dimension_alias!r}"
    for alias in metric_aliases.values():
        if alias not in columns:
            return f"结果缺少契约列 {alias!r}"

    allowed_periods = set(period_values) | _ALLOWED_PERIODS
    seen: dict = {}
    for row in result_rows or []:
        period = row.get(period_alias)
        if period not in allowed_periods:
            return f"period 值 {period!r} 不在允许范围 (comparison/current)"
        if need_dimension:
            member = row.get(dimension_alias)
            if not isinstance(member, str) or not member.strip():
                return f"维度成员值非法：{member!r}"
        else:
            member = None
        if member in seen and period in seen[member]:
            return f"维度成员 {member!r} 在期间 {period!r} 出现重复行"
        seen.setdefault(member, set()).add(period)
        for alias in metric_aliases.values():
            value = row.get(alias)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"指标列 {alias!r} 的值不是数字：{value!r}"
    return None


class Normalizer:
    """查询结果归一化：QueryExecutionResult -> Observation。"""

    @classmethod
    def normalize(
        cls,
        observation_id: str,
        action: Action,
        sub_query: str,
        query_result: QueryExecutionResult,
        result_contract: dict,
    ) -> Observation:
        """按 Action 类型与 result_contract 将原始结果归一化为 Observation。"""
        dimension = action.dimension

        # 1. Provider failed：保留原始 QueryExecutionResult，状态 failed
        if query_result.status == ObservationStatus.failed:
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                sub_query=sub_query,
                query_result=query_result,
                dimension=dimension,
                normalized_rows=[],
                status=ObservationStatus.failed,
                error=query_result.error or _PROVIDER_FAILED_MESSAGE,
            )

        # 2. 成功但无数据行：empty，normalized_rows=[]
        if query_result.status == ObservationStatus.empty:
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                sub_query=sub_query,
                query_result=query_result,
                dimension=dimension,
                normalized_rows=[],
                status=ObservationStatus.empty,
                error=None,
            )

        # 3. Provider success：严格按 contract 归一化
        error = cls._validate_contract(result_contract, need_dimension=dimension is not None)
        if error is not None:
            return cls._failed(observation_id, action, sub_query, query_result, dimension, error)

        error, rows = cls._normalize_rows(query_result, result_contract)
        if error is not None:
            return cls._failed(observation_id, action, sub_query, query_result, dimension, error)

        return Observation(
            observation_id=observation_id,
            action_id=action.action_id,
            sub_query=sub_query,
            query_result=query_result,
            dimension=dimension,
            normalized_rows=rows,
            status=ObservationStatus.success,
            error=None,
        )

    # ==================== 内部实现 ====================

    @staticmethod
    def _failed(
        observation_id: str,
        action: Action,
        sub_query: str,
        query_result: QueryExecutionResult,
        dimension: DimensionKey | None,
        reason: str,
    ) -> Observation:
        return Observation(
            observation_id=observation_id,
            action_id=action.action_id,
            sub_query=sub_query,
            query_result=query_result,
            dimension=dimension,
            normalized_rows=[],
            status=ObservationStatus.failed,
            error=f"{_RESULT_NORMALIZATION_FAILED}：{reason}",
        )

    @staticmethod
    def _validate_contract(result_contract: dict, *, need_dimension: bool) -> str | None:
        if not isinstance(result_contract, dict):
            return "result_contract 必须是 dict"
        if not result_contract.get("period_alias"):
            return "契约缺少 period_alias"
        metric_aliases = result_contract.get("metric_aliases")
        if not isinstance(metric_aliases, dict) or not metric_aliases:
            return "契约缺少 metric_aliases"
        period_values = result_contract.get("period_values")
        if not isinstance(period_values, list) or not period_values:
            return "契约缺少 period_values"
        if need_dimension and not result_contract.get("dimension_alias"):
            return "维度拆解契约缺少 dimension_alias"
        return None

    @classmethod
    def _normalize_rows(
        cls,
        query_result: QueryExecutionResult,
        result_contract: dict,
    ) -> tuple[str | None, list[ComparisonRow]]:
        period_alias = result_contract["period_alias"]
        metric_aliases: dict = result_contract["metric_aliases"]
        period_values = set(result_contract["period_values"])
        dimension_alias = result_contract.get("dimension_alias")
        need_dimension = dimension_alias is not None

        # member -> (已出现期间集合, {metric_key: {period: value}})
        members: dict[str | None, tuple[set, dict]] = {}

        for row in query_result.table.rows:
            if period_alias not in row:
                return f"结果缺少契约列 {period_alias!r}", []
            period = row[period_alias]
            if period not in _ALLOWED_PERIODS or period not in period_values:
                return f"period 值 {period!r} 不在允许范围 (comparison/current)", []

            if need_dimension:
                if dimension_alias not in row:
                    return f"结果缺少契约列 {dimension_alias!r}", []
                member = row[dimension_alias]
                if not isinstance(member, str) or not member.strip():
                    return f"维度成员值非法：{member!r}", []
            else:
                member = None

            seen_periods, metric_map = members.setdefault(member, (set(), {}))
            if period in seen_periods:
                return f"维度成员 {member!r} 在期间 {period!r} 出现重复行", []
            seen_periods.add(period)

            for metric_key, alias in metric_aliases.items():
                if alias not in row:
                    return f"结果缺少契约列 {alias!r}", []
                value = cls._to_float(row[alias])
                if value is _INVALID:
                    return f"指标列 {alias!r} 的值不是数字：{row[alias]!r}", []
                metric_map.setdefault(metric_key, {})[period] = value

        if not members:
            return "结果没有任何可归一化行", []

        rows: list[ComparisonRow] = []
        # 确定性排序：总体行（None）放最后，其余按成员名排序
        for member in sorted(members, key=lambda k: (k is None, k or "")):
            _, metric_map = members[member]
            metric_values: dict[MetricKey, MetricPeriodValue] = {}
            for metric_key in metric_aliases:
                period_values_map = metric_map.get(metric_key, {})
                # 缺失期间 = 该成员该期间无销售 → 0（不是未知值）
                metric_values[MetricKey(metric_key)] = MetricPeriodValue(
                    current_value=period_values_map.get("current", 0.0),
                    comparison_value=period_values_map.get("comparison", 0.0),
                )
            rows.append(ComparisonRow(dimension_value=member, metric_values=metric_values))

        return None, rows

    @staticmethod
    def _to_float(value):
        """契约列值转 float；只接受 int/float，不做 str/bool/None 猜测。"""
        if value is None:
            return None
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return float(value)
        return _INVALID
