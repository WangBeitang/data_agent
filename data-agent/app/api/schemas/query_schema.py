"""POST /api/query 请求体 Schema（API 接口设计 §4.1 / §4.2）。

约束：

- query：trim 后长度 1～1000，缺失 / trim 后为空 / 超长均返回 HTTP 422；
- mode：auto | query | attribution，默认 auto；
- query 在 schema 层完成 trim，后续业务统一使用清洗后的 query。

无效请求由 FastAPI/Pydantic 在 SSE 建立前返回 HTTP 422，不进入业务链路。
"""

from pydantic import BaseModel, field_validator

from app.models.analysis import RequestMode


class QuerySchema(BaseModel):
    query: str
    mode: RequestMode = RequestMode.auto

    @field_validator("query")
    @classmethod
    def _trim_and_check_query(cls, value: str) -> str:
        value = value.strip()
        if not (1 <= len(value) <= 1000):
            raise ValueError("query 经 trim 后长度必须在 1~1000 字符之间")
        return value
