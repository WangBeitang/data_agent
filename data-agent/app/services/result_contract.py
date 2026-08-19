"""结果结构硬契约校验（Stage 7 收口）。

中立、低层、纯函数模块：不调用 LLM、不访问数据库、不依赖 Attribution Graph。
供 `QueryService` 与 `Normalizer` 共同复用，作为 DataAgent Graph 产出 SQL
输出边界的硬契约检查，保证与冻结 Normalizer（`app/attribution/normalizer.py`
`_normalize_rows`）语义严格一致，避免两套校验再次漂移。

校验规则（与 Normalizer._normalize_rows 一一对应）：

- 输出列必须满足 period_alias；
- breakdown 必须满足 dimension_alias；
- 所有 metric_aliases 必须存在；
- period_alias 的实际值必须 **同时** 属于冻结白名单 {comparison, current}
  且属于 result_contract.period_values（交集，禁止 union 放宽）；
- breakdown dimension value 必须有效（非空字符串）；
- 指标值必须保持可归一化数值语义：int / float 合法；None 合法
  （对应 MetricPeriodValue: float | null）；bool / 字符串等非法；
  不猜测、不自动转换字符串数字；
- 维度成员在同一期间不得出现重复行；
- 列名缺失 / 值非法即失败，不猜测兜底。

注意：result_rows 为空但列名符合契约 → 返回 None（空结果是合法 empty，
不触发修复）；只有 SQL 语法正确但输出偏离契约才触发内部修复。
"""

# 冻结的期间键白名单（与 Normalizer 共享单一来源，禁止 union 放宽）
PERIOD_WHITELIST = frozenset({"comparison", "current"})


def validate_contract_result(
    result_columns: list[str],
    result_rows: list[dict],
    result_contract: dict | None,
) -> str | None:
    """硬执行契约：检查 SQL 输出列与值是否满足 result_contract。

    返回失败原因（str）或 None（满足）。本函数与 Normalizer 的严格校验
    保持同一语义，作为 QueryService / DataAgent Graph 边界的硬契约检查，
    不调用 LLM、不猜列。
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

    period_values_set = set(period_values)
    seen: dict = {}
    for row in result_rows or []:
        period = row.get(period_alias)
        # 交集语义：必须同时属于冻结白名单与契约 period_values（禁止 union 放宽）
        if period not in PERIOD_WHITELIST or period not in period_values_set:
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
            # None 合法：对应 MetricPeriodValue: float | null
            if value is None:
                continue
            # 仅 int / float 合法；bool / 字符串等非法；不猜列、不自动转换
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"指标列 {alias!r} 的值不是数字：{value!r}"
    return None
