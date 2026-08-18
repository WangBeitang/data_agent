"""统一分析服务（Stage 3）。

HTTP 层唯一业务服务入口。职责（冻结 SPEC §5.12 / Stage 3 指令 §十）：

```text
analysis_id
→ route
→ query / attribution 分流
→ SSE 序列化
→ stage 状态适配
→ query_result 适配
→ error
→ done
```

本类不得：
- 生成 SQL；
- 操作 Repository；
- 做归因计算；
- 写 Planner 逻辑；
- 直接访问数据库。

普通问数事件流（新 SSE 契约）：

```text
route(query)
→ stage*
→ query_result
→ done(completed)
```

Attribution（Stage 3 过渡行为）：route(attribution) 后以 NOT_IMPLEMENTED
受控失败结束，不伪造 query_result / report。
"""

import json
import uuid

from app.api.schemas.query_schema import QuerySchema
from app.attribution.intent_router import IntentRouter
from app.core.context import get_req_id
from app.core.log import logger
from app.models.analysis import (
    AnalysisMode,
    AnalysisStatus,
    QueryTable,
    RouteResult,
)
from app.services.query_service import QueryService

# 稳定错误码（API 接口设计 §13；NOT_IMPLEMENTED 仅属于 Stage 3 过渡码）
_CODE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
_CODE_INTERNAL_ERROR = "INTERNAL_ERROR"

# 安全错误信息：禁止包含 str(e) / traceback / 绝对路径 / 密码 / API Key / Prompt
_SAFE_ERROR_MESSAGES = {
    "QUERY_GENERATION_FAILED": "无法生成有效的查询 SQL，请调整问题后重试。",
    "QUERY_VALIDATION_FAILED": "查询 SQL 校验或修正失败，请调整问题后重试。",
    "QUERY_EXECUTION_FAILED": "查询 SQL 执行失败，请调整问题后重试。",
    "INTERNAL_ERROR": "分析执行过程中出现内部错误，请稍后重试。",
    "NOT_IMPLEMENTED": "经营归因分析尚未进入实施阶段，当前仅支持普通问数。",
}


class _StageTracker:
    """stage 状态机（Stage 3 指令 §13.1 / §13.2）。

    - 连续相同 stage_code 只保留一次 running；
    - 进入新 stage_code 前，前一个 stage → success；
    - 收到 query_result 前，当前 stage → success；
    - 执行异常时，当前 stage → failed。
    """

    def __init__(self, analysis_id: str):
        self._analysis_id = analysis_id
        self._current: str | None = None
        self._texts: dict[str, str] = {}

    @property
    def current(self) -> str | None:
        return self._current

    def on_stage(self, stage_code: str, stage: str) -> list[dict]:
        if stage_code == self._current:
            return []  # 连续去重
        events = []
        if self._current is not None:
            events.append(self._stage_event(self._current, self._texts[self._current], "success"))
        self._texts[stage_code] = stage
        self._current = stage_code
        events.append(self._stage_event(stage_code, stage, "running"))
        return events

    def close_success(self) -> list[dict]:
        events = []
        if self._current is not None:
            events.append(self._stage_event(self._current, self._texts[self._current], "success"))
            self._current = None
        return events

    def close_failed(self) -> list[dict]:
        events = []
        if self._current is not None:
            events.append(self._stage_event(self._current, self._texts[self._current], "failed"))
            self._current = None
        return events

    def _stage_event(self, stage_code: str, stage: str, status: str) -> dict:
        return {
            "type": "stage",
            "analysis_id": self._analysis_id,
            "stage_code": stage_code,
            "stage": stage,
            "status": status,
        }


class AnalysisService:
    def __init__(
        self,
        query_service: QueryService,
        intent_router: IntentRouter | None = None,
    ):
        self._query_service = query_service
        self._intent_router = intent_router if intent_router is not None else IntentRouter()

    # ==================== 公共入口 ====================

    async def stream(self, query_schema: QuerySchema):
        """SSE 字符串流（router 直接消费）。"""
        async for event in self.iter_events(query_schema):
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)} \n\n"

    async def iter_events(self, query_schema: QuerySchema):
        """结构化事件流（测试与序列化共用）。所有事件含 type + analysis_id。"""
        analysis_id = self._resolve_analysis_id()
        query = query_schema.query  # schema 层已完成 trim
        requested_mode = query_schema.mode

        route_result = self._intent_router.route(query, requested_mode)
        yield self._route_event(analysis_id, route_result)

        if route_result.resolved_mode == AnalysisMode.attribution:
            # Stage 3：只识别、不执行；受控失败，不伪造归因成功
            yield self._error_event(
                analysis_id,
                _CODE_NOT_IMPLEMENTED,
                phase=None,
                fatal=True,
            )
            yield self._done_event(
                analysis_id,
                mode=AnalysisMode.attribution,
                status=AnalysisStatus.failed,
                query_count=0,
            )
            return

        # query 模式
        async for event in self._iter_query_mode(analysis_id, query):
            yield event

    # ==================== query 模式 ====================

    async def _iter_query_mode(self, analysis_id: str, query: str):
        tracker = _StageTracker(analysis_id)
        result_status: str | None = None  # "success" | "empty"

        try:
            async for chunk in self._query_service.stream(query):
                if not isinstance(chunk, dict):
                    continue
                if "stage_code" in chunk and "stage" in chunk:
                    for ev in tracker.on_stage(chunk["stage_code"], chunk["stage"]):
                        yield ev
                elif "query_result" in chunk:
                    # 成功获得 query_result 前：当前 stage → success
                    for ev in tracker.close_success():
                        yield ev
                    event = self._query_result_event(analysis_id, query, chunk["query_result"])
                    result_status = event["status"]
                    yield event
                # legacy {"result": [...]} 事件被忽略，不进入正式 SSE
        except Exception as e:
            logger.error(f"普通问数流式执行异常 query={query!r}", exc_info=True)
            phase = tracker.current
            code = self._map_error_code(phase)
            for ev in tracker.close_failed():
                yield ev
            yield self._error_event(analysis_id, code, phase=phase, fatal=True)
            yield self._done_event(
                analysis_id,
                mode=AnalysisMode.query,
                status=AnalysisStatus.failed,
                query_count=1,
            )
            return

        if result_status is None:
            # 流正常结束但未收到 query_result：内部 Graph 契约被破坏
            logger.error(f"普通问数流结束但未收到 query_result query={query!r}")
            phase = tracker.current
            for ev in tracker.close_failed():
                yield ev
            yield self._error_event(analysis_id, _CODE_INTERNAL_ERROR, phase=phase, fatal=True)
            yield self._done_event(
                analysis_id,
                mode=AnalysisMode.query,
                status=AnalysisStatus.failed,
                query_count=1,
            )
            return

        # success / empty → completed
        yield self._done_event(
            analysis_id,
            mode=AnalysisMode.query,
            status=AnalysisStatus.completed,
            query_count=1,
        )

    # ==================== 事件构造 ====================

    @staticmethod
    def _route_event(analysis_id: str, route_result: RouteResult) -> dict:
        return {
            "type": "route",
            "analysis_id": analysis_id,
            "requested_mode": route_result.requested_mode.value,
            "resolved_mode": route_result.resolved_mode.value,
            "source": route_result.source.value,
            "rule": route_result.rule,
        }

    @staticmethod
    def _query_result_event(analysis_id: str, query: str, internal: dict) -> dict:
        """内部 query_result -> 正式 query_result（API 接口设计 §10.1）。"""
        sql = internal.get("sql")
        columns = list(internal.get("columns") or [])
        rows = list(internal.get("rows") or [])
        status = "success" if rows else "empty"
        return {
            "type": "query_result",
            "analysis_id": analysis_id,
            "mode": AnalysisMode.query.value,
            "action_id": None,
            "observation_id": None,
            "query": query,
            "sql": sql,
            "table": QueryTable(
                columns=columns,
                rows=rows,
                row_count=len(rows),
            ).model_dump(),
            "status": status,
            "error": None,
        }

    @staticmethod
    def _error_event(
        analysis_id: str,
        code: str,
        phase: str | None,
        fatal: bool,
        retryable: bool = False,
    ) -> dict:
        return {
            "type": "error",
            "analysis_id": analysis_id,
            "code": code,
            "message": _SAFE_ERROR_MESSAGES.get(code, _SAFE_ERROR_MESSAGES[_CODE_INTERNAL_ERROR]),
            "phase": phase,
            "action_id": None,
            "retryable": retryable,
            "fatal": fatal,
        }

    @staticmethod
    def _done_event(
        analysis_id: str,
        mode: AnalysisMode,
        status: AnalysisStatus,
        query_count: int,
        has_report: bool = False,
        message: str | None = None,
    ) -> dict:
        return {
            "type": "done",
            "analysis_id": analysis_id,
            "mode": mode.value,
            "status": status.value,
            "query_count": query_count,
            "has_report": has_report,
            "message": message,
        }

    # ==================== 辅助 ====================

    @staticmethod
    def _map_error_code(phase: str | None) -> str:
        """根据异常时可判断阶段映射稳定错误码；无法判断用 INTERNAL_ERROR。"""
        if phase == "sql_generation":
            return "QUERY_GENERATION_FAILED"
        if phase == "sql_validation":
            return "QUERY_VALIDATION_FAILED"
        if phase == "sql_execution":
            return "QUERY_EXECUTION_FAILED"
        return _CODE_INTERNAL_ERROR

    @staticmethod
    def _resolve_analysis_id() -> str:
        """analysis_id 复用当前 HTTP req_id；脱离 HTTP（空值）时回退 uuid4。"""
        req_id = get_req_id()
        if req_id:
            return req_id
        return str(uuid.uuid4())
