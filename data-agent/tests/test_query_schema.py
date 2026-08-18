"""Stage 3：QuerySchema 校验测试。

覆盖（Stage 3 指令 §二十）：
- mode 默认 auto；
- query trim；
- 空 query 拒绝；
- > 1000 拒绝（1000 恰好通过）；
- 非法 mode 拒绝。
"""

import pytest
from pydantic import ValidationError

from app.api.schemas.query_schema import QuerySchema
from app.models.analysis import RequestMode


def test_mode_defaults_to_auto():
    schema = QuerySchema(query="统计2025年各月销售额")
    assert schema.mode == RequestMode.auto


def test_query_is_trimmed():
    schema = QuerySchema(query="  统计2025年各月销售额  ")
    assert schema.query == "统计2025年各月销售额"


def test_query_trimmed_len_1_ok():
    schema = QuerySchema(query="  统  ")
    assert schema.query == "统"


def test_blank_query_rejected():
    with pytest.raises(ValidationError):
        QuerySchema(query="   ")


def test_empty_query_rejected():
    with pytest.raises(ValidationError):
        QuerySchema(query="")


def test_query_over_1000_rejected():
    with pytest.raises(ValidationError):
        QuerySchema(query="a" * 1001)


def test_query_exactly_1000_ok():
    schema = QuerySchema(query="a" * 1000)
    assert len(schema.query) == 1000


def test_invalid_mode_rejected():
    with pytest.raises(ValidationError):
        QuerySchema(query="统计销售额", mode="report")


def test_explicit_query_mode_ok():
    schema = QuerySchema(query="统计销售额", mode="query")
    assert schema.mode == RequestMode.query


def test_explicit_attribution_mode_ok():
    schema = QuerySchema(query="为什么下降", mode="attribution")
    assert schema.mode == RequestMode.attribution
