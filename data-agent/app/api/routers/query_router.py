from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.api.dependencies import get_analysis_service
from app.api.schemas.query_schema import QuerySchema
from app.services.analysis_service import AnalysisService

# 统一分析入口路由（Stage 3）
# 职责边界：只接收 QuerySchema -> Depends AnalysisService -> StreamingResponse。
# router 不做 intent 判断、不操作 QueryService、不拼 SSE、不生成 analysis_id。
query_router = APIRouter()


@query_router.post("/api/query")
async def search(
    query_schema: QuerySchema,
    service: AnalysisService = Depends(get_analysis_service),
):
    return StreamingResponse(
        service.stream(query_schema),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
