"""意图路由（Stage 3）。

将请求模式解析为最终分析模式，输出 RouteResult。

优先级（冻结 SPEC §5.3 / Stage 3 指令 §7.1）：

```text
forced mode
> attribution 强规则
> query 强规则
> LLM
```

- forced：mode=query / mode=attribution 直接生效，问题文本不能覆盖；
- auto：先按强规则关键词判断，attribution 强特征优先于 query 强特征；
- 规则无法明确判断时调用 LLM 做二分类；LLM 非法输出重试一次，
  第二次仍失败降级 resolved_mode=query 并写 warning 日志；
- 本类不暴露任何 LLM 隐藏推理。
"""

import json

from app.agent import llm as llm_module
from app.core.log import logger
from app.models.analysis import AnalysisMode, RequestMode, RouteResult, RouteSource
from app.prompt.prompt_loader import load_prompt

# 归因强特征关键词（attribution 优先）
_ATTRIBUTION_KEYWORDS = (
    "为什么",
    "原因",
    "归因",
    "哪些因素导致",
    "下降原因",
    "增长原因",
    "增长但",
    "下降但",
)

# 普通问数强特征关键词
_QUERY_KEYWORDS = (
    "统计",
    "查询",
    "多少",
    "排名",
    "分别是多少",
)

# route 事件中可展示的规则文本（API 接口设计 §7 示例风格）
_RULE_TEXT = {
    "attribution": "原因类关键词",
    "query": "统计类关键词",
}


class IntentRouter:
    """意图路由：RequestMode -> RouteResult。"""

    def __init__(self, llm=None, max_llm_attempts: int = 2):
        self._llm = llm if llm is not None else llm_module.llm
        self._max_llm_attempts = max_llm_attempts
        self._prompt = load_prompt("attribution_intent")

    def route(self, query: str, requested_mode: RequestMode) -> RouteResult:
        """按优先级解析最终模式，永不返回 resolved_mode=auto。"""
        if requested_mode == RequestMode.query:
            return RouteResult(
                requested_mode=requested_mode,
                resolved_mode=AnalysisMode.query,
                source=RouteSource.forced,
                rule=None,
            )
        if requested_mode == RequestMode.attribution:
            return RouteResult(
                requested_mode=requested_mode,
                resolved_mode=AnalysisMode.attribution,
                source=RouteSource.forced,
                rule=None,
            )

        # auto：归因强特征优先
        if any(kw in query for kw in _ATTRIBUTION_KEYWORDS):
            return RouteResult(
                requested_mode=RequestMode.auto,
                resolved_mode=AnalysisMode.attribution,
                source=RouteSource.rule,
                rule=_RULE_TEXT["attribution"],
            )
        if any(kw in query for kw in _QUERY_KEYWORDS):
            return RouteResult(
                requested_mode=RequestMode.auto,
                resolved_mode=AnalysisMode.query,
                source=RouteSource.rule,
                rule=_RULE_TEXT["query"],
            )

        # 规则无法明确判断：LLM 二分类
        return self._route_by_llm(query)

    def _route_by_llm(self, query: str) -> RouteResult:
        # 使用 replace 而非 str.format：prompt 内含 JSON 示例花括号，避免被解析为格式字段
        prompt = self._prompt.replace("{query}", query)
        for attempt in range(1, self._max_llm_attempts + 1):
            try:
                raw = self._llm.invoke(prompt)
                text = raw.content if hasattr(raw, "content") else str(raw)
                mode = self._parse_mode(text)
                if mode is not None:
                    return RouteResult(
                        requested_mode=RequestMode.auto,
                        resolved_mode=mode,
                        source=RouteSource.llm,
                        rule=None,
                    )
            except Exception as e:  # LLM 调用异常也视为一次失败
                logger.warning(f"意图路由 LLM 调用异常 attempt={attempt} error={e!r}")
            logger.warning(f"意图路由 LLM 输出非法 attempt={attempt}，重试或降级")
        # 第二次仍失败：降级普通问数，不让路由 LLM 格式错误拖垮问数链路
        logger.warning("意图路由 LLM 连续失败，降级 resolved_mode=query")
        return RouteResult(
            requested_mode=RequestMode.auto,
            resolved_mode=AnalysisMode.query,
            source=RouteSource.llm,
            rule=None,
        )

    @staticmethod
    def _parse_mode(text: str) -> AnalysisMode | None:
        """从 LLM 输出解析 mode，容忍代码块/前后杂文本。"""
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
        mode = data.get("mode")
        if mode == AnalysisMode.query.value:
            return AnalysisMode.query
        if mode == AnalysisMode.attribution.value:
            return AnalysisMode.attribution
        return None
