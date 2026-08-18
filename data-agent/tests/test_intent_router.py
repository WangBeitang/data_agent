"""Stage 3：Intent Router 测试。

覆盖（Stage 3 指令 §二十）：
- resolved_mode 不能 auto；
- forced query / forced attribution（问题文本不能覆盖）；
- attribution 强关键词；
- query 强关键词；
- attribution / query 同时命中时 attribution 优先；
- 模糊问题走 LLM；
- LLM 第一次非法、第二次成功；
- LLM 连续两次非法 → query fallback。

LLM 全部 mock，不调用真实服务。
"""

from app.attribution.intent_router import IntentRouter
from app.models.analysis import AnalysisMode, RequestMode, RouteSource


class _FakeLLM:
    """按调用顺序返回响应的 LLM mock（内容可带 content 属性模拟 AIMessage）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, prompt):
        raw = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(raw, str):
            return raw
        return type("AIMessage", (), {"content": raw})()


def _router(llm=None):
    return IntentRouter(llm=llm, max_llm_attempts=2)


# ==================== forced ====================

def test_forced_query_cannot_be_overridden_by_text():
    result = _router().route("为什么2月销售额下降？", RequestMode.query)
    assert result.resolved_mode == AnalysisMode.query
    assert result.source == RouteSource.forced
    assert result.rule is None
    assert result.requested_mode == RequestMode.query


def test_forced_attribution_cannot_be_overridden_by_text():
    result = _router().route("统计2025年各月销售额", RequestMode.attribution)
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.forced
    assert result.rule is None
    assert result.requested_mode == RequestMode.attribution


# ==================== 强规则 ====================

def test_attribution_keyword_why():
    result = _router().route("为什么2月销售额下降？", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.rule


def test_attribution_keyword_reason():
    result = _router().route("2月销售额下降的原因是什么", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.rule


def test_attribution_keyword_guiyin():
    result = _router().route("对销售下滑做归因分析", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution


def test_attribution_keyword_growth_but():
    result = _router().route("销量增长但销售额下降", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution


def test_query_keyword_tongji():
    result = _router().route("统计2025年各月销售额", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.query
    assert result.source == RouteSource.rule
    assert result.rule == "统计类关键词"


def test_query_keyword_rank():
    result = _router().route("查询各销售区域销售额排名", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.query


def test_query_keyword_how_many():
    result = _router().route("2月销售了多少台", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.query


def test_attribution_priority_over_query():
    """同时命中归因与问数特征时，attribution 优先。"""
    result = _router().route("统计为什么2月销售额下降的原因", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.rule


def test_why_query_with_sales_not_routed_to_query():
    """「为什么2月销售额下降？」不能因为含「销售额」而路由 query。"""
    result = _router().route("为什么2月销售额下降？", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution


# ==================== LLM ====================

def test_ambiguous_query_goes_to_llm():
    fake = _FakeLLM(['{"mode": "query"}'])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert fake.calls == 1
    assert result.resolved_mode == AnalysisMode.query
    assert result.source == RouteSource.llm


def test_llm_attribution_output():
    fake = _FakeLLM(['{"mode": "attribution"}'])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.llm


def test_llm_first_invalid_second_success():
    fake = _FakeLLM(["不是JSON", '{"mode": "attribution"}'])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert fake.calls == 2
    assert result.resolved_mode == AnalysisMode.attribution
    assert result.source == RouteSource.llm


def test_llm_first_exception_second_success():
    class _RaisingThenOk:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("llm service down")
            return '{"mode": "query"}'

    fake = _RaisingThenOk()
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert fake.calls == 2
    assert result.resolved_mode == AnalysisMode.query
    assert result.source == RouteSource.llm


def test_llm_twice_invalid_falls_back_to_query():
    fake = _FakeLLM(["bad", "bad"])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert fake.calls == 2
    assert result.resolved_mode == AnalysisMode.query
    assert result.source == RouteSource.llm  # 降级发生在 LLM 路径


def test_llm_invalid_mode_value_falls_back():
    fake = _FakeLLM(['{"mode": "report"}', '{"mode": "report"}'])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert fake.calls == 2
    assert result.resolved_mode == AnalysisMode.query


def test_llm_tolerant_to_code_block():
    fake = _FakeLLM(["```json\n{\"mode\": \"query\"}\n```"])
    result = _router(fake).route("帮我看看这个月的销售情况", RequestMode.auto)
    assert result.resolved_mode == AnalysisMode.query


# ==================== 不变量 ====================

def test_resolved_mode_never_auto():
    fake = _FakeLLM(['{"mode": "query"}'])
    router = _router(fake)
    cases = [
        ("统计2025年各月销售额", RequestMode.auto),
        ("为什么2月销售额下降？", RequestMode.auto),
        ("帮我看看这个月的销售情况", RequestMode.auto),
        ("任意问题", RequestMode.query),
        ("任意问题", RequestMode.attribution),
    ]
    for query, mode in cases:
        assert router.route(query, mode).resolved_mode in (
            AnalysisMode.query,
            AnalysisMode.attribution,
        )
