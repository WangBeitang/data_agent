"""Stage 5：Target Parser 测试。

覆盖（Stage 5 指令 §十五）：
- 场景一 target（销售额下降）；
- 场景二 target（量额背离）；
- 缺指标；
- 缺期间；
- 非支持业务指标；
- TARGET_PARSE_FAILED（parse 返回 None）；
- LLM fallback 成功 / 非法输出；
- 跨年对比期（1 月较 12 月 → 上一年）。
"""

from datetime import date

from app.attribution.target_parser import TargetParser
from app.models.analysis import MetricKey

# ==================== 规则解析：冻结场景 ====================


def test_scenario_one_target():
    """为什么 2025 年 2 月销售额较 1 月明显下降？"""
    target = TargetParser(llm=None).parse("为什么 2025 年 2 月销售额较 1 月明显下降？")
    assert target is not None
    assert target.metrics == [MetricKey.sales_amount]
    assert target.current_period.label == "2025年2月"
    assert target.current_period.start_date == date(2025, 2, 1)
    assert target.current_period.end_date == date(2025, 2, 28)
    assert target.comparison_period.label == "2025年1月"
    assert target.comparison_period.start_date == date(2025, 1, 1)
    assert target.comparison_period.end_date == date(2025, 1, 31)


def test_scenario_two_target_quantity_amount_divergence():
    """量额背离：metrics=[sales_quantity, sales_amount]，期间 2025-03 / 2025-02。

    avg_unit_sales_amount 不进入 target（由 Calculator 派生）。
    """
    target = TargetParser(llm=None).parse(
        "为什么 2025 年 3 月销售数量大幅增长，但销售额增长有限？"
    )
    assert target is not None
    assert target.metrics == [MetricKey.sales_quantity, MetricKey.sales_amount]
    assert MetricKey.avg_unit_sales_amount not in target.metrics
    assert target.current_period.label == "2025年3月"
    assert target.comparison_period.label == "2025年2月"


def test_compare_month_greater_than_current_uses_previous_year():
    """2025年1月较12月：对比期应为 2024-12（跨年）。"""
    target = TargetParser(llm=None).parse("为什么 2025 年 1 月销售额较 12 月明显下降？")
    assert target is not None
    assert target.current_period.label == "2025年1月"
    assert target.comparison_period.label == "2024年12月"


def test_metrics_with_both_quantity_and_amount_keywords():
    target = TargetParser(llm=None).parse("为什么 2025 年 4 月销售数量和销售额同时下降？")
    assert target is not None
    assert target.metrics == [MetricKey.sales_quantity, MetricKey.sales_amount]


# ==================== 规则解析：失败场景 ====================


def test_missing_metric_returns_none_with_rule_and_fake_llm_failure():
    """缺指标：规则失败，LLM 也失败 → None（TARGET_PARSE_FAILED）。"""

    class _FakeLLM:
        def invoke(self, prompt):
            return type("R", (), {"content": "not json"})  # 非法输出

    parser = TargetParser(llm=_FakeLLM())
    assert parser.parse("为什么 2025 年 2 月出现明显下降？") is None


def test_missing_period_returns_none():
    """缺期间：无明确年份月份 → 规则失败 → LLM 失败 → None。"""

    class _FakeLLM:
        def invoke(self, prompt):
            return type("R", (), {"content": '{"metrics": ["sales_amount"]}'})

    parser = TargetParser(llm=_FakeLLM())
    assert parser.parse("为什么销售额下降？") is None


def test_unsupported_business_metric_rejected_without_llm_call():
    """非支持业务指标（成本/利润/库存/生产）→ TARGET_PARSE_FAILED。

    硬拒绝：unsupported term 一旦命中直接返回 None，不得调用 LLM
    （真实断言 call_count == 0）。
    """

    class _CountingLLM:
        def __init__(self):
            self.call_count = 0

        def invoke(self, prompt):
            self.call_count += 1
            raise AssertionError("不支持指标不应调用 LLM")

    llm = _CountingLLM()
    parser = TargetParser(llm=llm)
    assert parser.parse("为什么 2025 年 2 月利润下降？") is None
    assert parser.parse("为什么 2025 年 2 月库存增加？") is None
    assert parser.parse("为什么 2025 年 2 月生产成本上升？") is None
    assert llm.call_count == 0  # 硬拒绝，不调用 LLM


def test_explicit_comparison_year_preferred():
    """显式完整比较期间优先："为什么2025年6月销售额较2024年6月下降？"

    current=2025-06，comparison=2024-06；不得把明确的 2024-06 静默解析成
    2025-05（同年/跨年前推规则只在未给出比较年份时使用）。
    """
    target = TargetParser(llm=None).parse("为什么2025年6月销售额较2024年6月下降？")
    assert target is not None
    assert target.current_period.label == "2025年6月"
    assert target.comparison_period.label == "2024年6月"


def test_explicit_comparison_year_with_spaces():
    """带空格的显式比较年份同样支持。"""
    target = TargetParser(llm=None).parse("为什么 2025 年 6 月销售额较 2024 年 6 月下降？")
    assert target is not None
    assert target.current_period.label == "2025年6月"
    assert target.comparison_period.label == "2024年6月"


def test_comparison_without_year_falls_back_to_current_year():
    """未给出比较年份 → 沿用本期年份（2025-06 较 5 月 → 2025-05）。"""
    target = TargetParser(llm=None).parse("为什么2025年6月销售额较5月下降？")
    assert target is not None
    assert target.current_period.label == "2025年6月"
    assert target.comparison_period.label == "2025年5月"


# ==================== LLM fallback ====================


def test_llm_fallback_success():
    """规则无法解析时 LLM 给出合法 JSON → 校验通过。"""

    class _FakeLLM:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {"content": '{"metrics": ["order_count"], "current_period": "2025-05", "comparison_period": "2025-04"}'},
            )

    target = TargetParser(llm=_FakeLLM()).parse("为什么本季度订单数下降？")
    assert target is not None
    assert target.metrics == [MetricKey.order_count]
    assert target.current_period.label == "2025年5月"
    assert target.comparison_period.label == "2025年4月"


def test_llm_derived_metric_rejected():
    """LLM 输出 avg_unit_sales_amount → 派生指标拒绝 → None。"""

    class _FakeLLM:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {"content": '{"metrics": ["avg_unit_sales_amount"], "current_period": "2025-05", "comparison_period": "2025-04"}'},
            )

    parser = TargetParser(llm=_FakeLLM())
    assert parser.parse("为什么平均单件销售额变化？") is None


def test_llm_invalid_json_then_retry_success():
    """第一次非法 JSON，反馈重试后合法。"""

    class _FakeLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {"content": "garbage"})
            return type(
                "R",
                (),
                {"content": '{"metrics": ["sales_amount"], "current_period": "2025-06", "comparison_period": "2025-05"}'},
            )

    parser = TargetParser(llm=_FakeLLM())
    target = parser.parse("为什么销售额变化？")
    assert target is not None
    assert target.metrics == [MetricKey.sales_amount]


def test_llm_consecutive_failure_returns_none():
    """LLM 连续非法 → TARGET_PARSE_FAILED。"""

    class _FakeLLM:
        def invoke(self, prompt):
            return type("R", (), {"content": "still not json"})

    parser = TargetParser(llm=_FakeLLM(), max_llm_attempts=2)
    assert parser.parse("为什么销售额下降？") is None


def test_parse_none_is_target_parse_failed_semantics():
    """parse() 返回 None 即调用方映射 TARGET_PARSE_FAILED。"""
    assert TargetParser(llm=_FailingLLM()).parse("为什么销售额下降？") is None


class _FailingLLM:
    def invoke(self, prompt):
        raise RuntimeError("llm down")
