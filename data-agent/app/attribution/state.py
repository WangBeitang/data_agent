"""归因状态与上下文（Stage 4 + Stage 5）。

依据冻结文档（数据对象设计 §13 / SPEC §3.3 / §5.2）：

- AttributionContext 第一版只包含 query_service（请求级 QueryService，
  持有请求级数据库 Session）；不得加入 Repository、Session、LLM Client；
- AttributionState 使用 TypedDict，字段按数据对象设计冻结；
  max_query_actions 第一版固定为 6（初始化约束）；
- Stage 5 将 report 从临时占位改为正式 AttributionReport | None；
- AttributionState 不复制 DataAgentState 的关键词/召回等内部字段。
"""

from typing import TypedDict

from app.models.analysis import (
    Action,
    AnalysisStatus,
    AttributionReport,
    AttributionTarget,
    Calculation,
    Evidence,
    Observation,
    RequestMode,
    RouteResult,
)
from app.services.query_service import QueryService


class AttributionContext(TypedDict):
    """归因执行上下文（请求级，不可全局共享）。

    仅包含 query_service；不新增 Repository / 数据库 Session / LLM Client。
    """

    query_service: QueryService


class AttributionState(TypedDict):
    """一次归因请求的 LangGraph 临时状态（不持久化，页面刷新可丢失）。"""

    analysis_id: str
    question: str
    requested_mode: RequestMode
    route: RouteResult
    target: AttributionTarget
    actions: list[Action]
    observations: list[Observation]
    calculations: list[Calculation]
    evidences: list[Evidence]
    query_action_count: int
    consecutive_empty_or_failed: int
    max_query_actions: int
    status: AnalysisStatus
    # Stage 5 正式冻结对象；completed/partial 时由 Report Generator 填充
    report: AttributionReport | None
    failure_reason: str | None


def initial_state(
    analysis_id: str,
    question: str,
    requested_mode: RequestMode,
    route: RouteResult,
    target: AttributionTarget,
) -> AttributionState:
    """构造初始化 AttributionState（冻结 SPEC §7.1）。

    max_query_actions 属于初始化约束，第一版固定为 6。
    """
    return AttributionState(
        analysis_id=analysis_id,
        question=question,
        requested_mode=requested_mode,
        route=route,
        target=target,
        actions=[],
        observations=[],
        calculations=[],
        evidences=[],
        query_action_count=0,
        consecutive_empty_or_failed=0,
        max_query_actions=6,
        status=AnalysisStatus.running,
        report=None,
        failure_reason=None,
    )
