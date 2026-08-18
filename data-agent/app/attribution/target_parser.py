"""Target Parser（Stage 5）。

依据冻结文档（数据对象设计 §7 / SPEC §5.4 / Stage 5 指令 §五）：

```text
原始归因问题 → AttributionTarget
```

只解析：

- metrics
- current_period
- comparison_period

规则：

1. 优先利用问题中明确日期和指标（确定性规则解析）；
2. 规则无法可靠确定时调用现有 `app.agent.llm.llm`（不新增 LLM Client Manager）；
3. LLM 输出必须经过 Pydantic 结构化校验；
4. 无法可靠确定指标、本期或对比期 → parse() 返回 None，调用方映射
   `TARGET_PARSE_FAILED`；
5. 不生成原因、不生成 SQL、不生成事实数字；
6. 不擅自加入成本、利润、库存、生产等不存在指标；
7. 第一版不实现服务端追问。

量额背离问题：

```text
为什么 2025 年 3 月销售数量大幅增长，但销售额增长有限？
→ metrics = [sales_quantity, sales_amount]
  current_period = 2025-03
  comparison_period = 2025-02
```

avg_unit_sales_amount 属于派生指标，不进入 target，由 Calculator 派生。

本模块禁止：生成 SQL、生成原因、访问数据库。
"""

import calendar
import json
import re
from datetime import date

from pydantic import BaseModel, Field

from app.agent import llm as llm_module
from app.core.log import logger
from app.models.analysis import AttributionTarget, MetricKey, Period
from app.prompt.prompt_loader import load_prompt

# 不支持业务词：问题出现这些词时无法可靠解析为支持指标
_UNSUPPORTED_TERMS = (
    "成本",
    "利润",
    "毛利",
    "净利",
    "库存",
    "存货",
    "生产",
    "产能",
    "设备",
    "质量",
    "良率",
    "交期",
)

# 指标中文名 → MetricKey（长词优先，防止"销售数量"被"数量"提前匹配）
_METRIC_RULES: tuple[tuple[str, MetricKey], ...] = (
    ("销售数量", MetricKey.sales_quantity),
    ("销售件数", MetricKey.sales_quantity),
    ("销售件量", MetricKey.sales_quantity),
    ("销售额", MetricKey.sales_amount),
    ("销售金额", MetricKey.sales_amount),
    ("销售收入", MetricKey.sales_amount),
    ("订单数量", MetricKey.order_count),
    ("订单量", MetricKey.order_count),
    ("订单数", MetricKey.order_count),
)

# 量额背离：同时关注数量与金额 → 固定顺序 [sales_quantity, sales_amount]
_QUANTITY_TERMS = ("数量", "件数", "件量", "销量")
_AMOUNT_TERMS = ("销售额", "销售金额", "销售收入", "金额")

# 显式对比期表达（无年份，只给月份）：未写比较年份时沿用本期年份，
# 比较月大于本期月时跨年前推（如"2025年1月较12月"→ 2024-12）
_COMPARISON_PATTERNS = (
    r"(?:较|比|对比|与|相较|相对|环比)\s*(\d{1,2})\s*月",
    r"(\d{1,2})\s*月\s*(?:较|比|对比|与|相较|相对|环比)",
)

# 显式完整比较期间（带年份，优先于无年份规则）："较 2024 年 6 月"
_EXPLICIT_COMPARISON_PATTERN = re.compile(
    r"(?:较|比|对比|与|相较|相对|环比)\s*(\d{4})\s*年\s*(\d{1,2})\s*月"
)

# 主期间（带年份的月份）模式
_YEAR_MONTH_PATTERN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")


class _LLMTarget(BaseModel):
    """LLM 输出结构化校验（Target Parser 专用，不对外）。"""

    metrics: list[MetricKey] = Field(min_length=1)
    current_period: str
    comparison_period: str


class TargetParser:
    """解析归因目标；无法可靠确定时返回 None（TARGET_PARSE_FAILED）。"""

    def __init__(self, llm=None, max_llm_attempts: int = 2):
        self._llm = llm if llm is not None else llm_module.llm
        self._max_llm_attempts = max_llm_attempts
        self._prompt = load_prompt("attribution_target")

    # ==================== 公共入口 ====================

    def parse(self, query: str) -> AttributionTarget | None:
        """返回 AttributionTarget；无法可靠确定指标/本期/对比期 → None。

        命中不支持经营词（成本/利润/库存/生产/产能/设备/质量等）时硬拒绝，
        直接返回 None，不得继续调用 LLM。
        """
        if any(term in query for term in _UNSUPPORTED_TERMS):
            logger.warning("问题包含不支持的经营维度/指标，拒绝解析（不调用 LLM）")
            return None

        target = self._parse_by_rule(query)
        if target is not None:
            return target
        logger.warning("Target Parser 规则解析失败，尝试 LLM 解析 query=%r", query)
        return self._parse_by_llm(query)

    # ==================== 确定性规则解析 ====================

    def _parse_by_rule(self, query: str) -> AttributionTarget | None:
        if any(term in query for term in _UNSUPPORTED_TERMS):
            logger.warning("问题包含不支持的经营维度/指标，拒绝解析")
            return None

        metrics = self._parse_metrics(query)
        if not metrics:
            return None

        periods = self._parse_periods(query)
        if periods is None:
            return None
        current_period, comparison_period = periods
        return AttributionTarget(
            metrics=metrics,
            current_period=current_period,
            comparison_period=comparison_period,
        )

    @staticmethod
    def _parse_metrics(query: str) -> list[MetricKey]:
        """规则解析指标；量额背离时固定 [sales_quantity, sales_amount]。"""
        if any(term in query for term in _QUANTITY_TERMS) and any(
            term in query for term in _AMOUNT_TERMS
        ):
            return [MetricKey.sales_quantity, MetricKey.sales_amount]

        metrics: list[MetricKey] = []
        for keyword, metric in _METRIC_RULES:
            if keyword in query and metric not in metrics:
                metrics.append(metric)
        return metrics

    def _parse_periods(self, query: str) -> tuple[Period, Period] | None:
        """解析本期与对比期。

        规则（优先级从高到低）：
        - 带年份的月份作为本期（问题中第一个 "XXXX年X月"）；
        - 显式完整比较期间（"较 2024 年 6 月"）优先使用显式年份；
        - 未给出比较年份时："较X月 / 比X月" 沿用本期年份，
          比较月大于本期月则视为上一年（跨年前推）；
        - 无显式对比期 → 本期前一个月。
        """
        year_month_matches = list(_YEAR_MONTH_PATTERN.finditer(query))
        if not year_month_matches:
            return None
        explicit = _EXPLICIT_COMPARISON_PATTERN.search(query)
        # 本期默认取第一个 "XXXX年X月"；若该匹配落在显式比较表达内部
        # （如"较 2024 年 6 月"先于本期出现），则本期取下一个带年份月份
        current_match = year_month_matches[0]
        if (
            explicit is not None
            and explicit.start() < current_match.start() < explicit.end()
            and len(year_month_matches) > 1
        ):
            current_match = year_month_matches[1]
        year = int(current_match.group(1))
        month = int(current_match.group(2))
        if not 1 <= month <= 12:
            return None
        current_period = _make_period(year, month)

        # 1. 显式完整比较期间（带年份）：必须优先使用显式年份
        if explicit is not None:
            comparison_year = int(explicit.group(1))
            comparison_month = int(explicit.group(2))
            if not 1 <= comparison_month <= 12:
                return None
            comparison_period = _make_period(comparison_year, comparison_month)
            return current_period, comparison_period

        # 2. 无年份比较月份：沿用本期年份（跨年回退）
        comparison_month: int | None = None
        for pattern in _COMPARISON_PATTERNS:
            m = re.search(pattern, query)
            if m:
                comparison_month = int(m.group(1))
                break

        if comparison_month is None or comparison_month == month:
            # 无显式对比期或对比期等于本期：默认前一个月
            prev_year, prev_month = _prev_month(year, month)
            comparison_period = _make_period(prev_year, prev_month)
        else:
            if not 1 <= comparison_month <= 12:
                return None
            comparison_year = year
            if comparison_month > month:
                comparison_year = year - 1
            comparison_period = _make_period(comparison_year, comparison_month)

        return current_period, comparison_period

    # ==================== LLM 解析 ====================

    def _parse_by_llm(self, query: str) -> AttributionTarget | None:
        # 使用 replace 而非 str.format：prompt 内含 JSON 示例花括号
        prompt = self._prompt.replace("{query}", query)
        for attempt in range(1, self._max_llm_attempts + 1):
            try:
                raw = self._llm.invoke(prompt)
                text = raw.content if hasattr(raw, "content") else str(raw)
                target = self._parse_llm_text(text)
                if target is not None:
                    return target
            except Exception as e:  # LLM 调用异常也视为一次失败
                logger.warning(f"Target Parser LLM 调用异常 attempt={attempt} error={e!r}")
            logger.warning(f"Target Parser LLM 输出非法 attempt={attempt}，重试或失败")
        logger.warning("Target Parser LLM 连续失败，返回 TARGET_PARSE_FAILED")
        return None

    def _parse_llm_text(self, text: str) -> AttributionTarget | None:
        """LLM 文本 → AttributionTarget；任何结构/枚举/期间非法 → None。"""
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
        if "error" in data:
            return None
        try:
            llm_target = _LLMTarget(**data)
        except Exception:
            return None
        if MetricKey.avg_unit_sales_amount in llm_target.metrics:
            # 派生指标不能作为 target 指标（由 Calculator 派生）
            return None
        current = _parse_period_string(llm_target.current_period)
        comparison = _parse_period_string(llm_target.comparison_period)
        if current is None or comparison is None:
            return None
        return AttributionTarget(
            metrics=llm_target.metrics,
            current_period=_make_period(*current),
            comparison_period=_make_period(*comparison),
        )


# ==================== 内部工具 ====================


def _make_period(year: int, month: int) -> Period:
    end = date(year, month, calendar.monthrange(year, month)[1])
    return Period(
        label=f"{year}年{month}月",
        start_date=date(year, month, 1),
        end_date=end,
    )


def _prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _parse_period_string(value: str) -> tuple[int, int] | None:
    """校验 "YYYY-MM" 格式并返回 (year, month)。"""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", value.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month
