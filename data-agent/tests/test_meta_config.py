"""Stage 1：制造业销售经营口径切换 配置验证测试。

验证 conf/meta_config.yaml 已从电商口径（GMV/AOV）切换为制造业销售经营口径：
- 核心指标为 销售额/销售数量/销售订单数/平均单件销售额；
- 不再使用 GMV/AOV 作为主业务口径；
- 产品字段中文描述统一为 产品/产品类别；
- member_level 使用 客户等级；
- 物理表名与字段名保持不变。
"""

from app.conf.meta_config import meta_config


def test_metrics_have_four_core_metrics():
    names = {m.name for m in meta_config.metrics}
    assert {"销售额", "销售数量", "销售订单数", "平均单件销售额"} <= names


def test_no_gmv_aov_metric():
    names = {m.name for m in meta_config.metrics}
    assert "GMV" not in names
    assert "AOV" not in names
    joined = " ".join(m.name + m.description for m in meta_config.metrics)
    assert "GMV" not in joined
    assert "AOV" not in joined


def test_sales_amount_metric_definition():
    metric = next(m for m in meta_config.metrics if m.name == "销售额")
    assert metric.relevant_columns == ["fact_order.order_amount"]
    assert "SUM(order_amount)" in metric.description
    for alias in ["销售额", "销售金额", "订单金额", "收入"]:
        assert alias in metric.alias


def test_sales_quantity_metric_definition():
    metric = next(m for m in meta_config.metrics if m.name == "销售数量")
    assert metric.relevant_columns == ["fact_order.order_quantity"]
    assert "SUM(order_quantity)" in metric.description


def test_order_count_metric_definition():
    metric = next(m for m in meta_config.metrics if m.name == "销售订单数")
    assert metric.relevant_columns == ["fact_order.order_id"]
    assert "COUNT(DISTINCT order_id)" in metric.description


def test_avg_unit_sales_amount_metric_definition():
    metric = next(m for m in meta_config.metrics if m.name == "平均单件销售额")
    assert set(metric.relevant_columns) == {"fact_order.order_amount", "fact_order.order_quantity"}
    assert "SUM(order_amount)" in metric.description
    assert "SUM(order_quantity)" in metric.description
    # 必须明确：仅表示销售额与销售数量的比值
    assert "仅表示销售额与销售数量的比值" in metric.description


def test_physical_table_names_unchanged():
    names = {t.name for t in meta_config.tables}
    assert names == {"dim_region", "dim_customer", "dim_product", "dim_date", "fact_order"}


def test_fact_order_physical_columns_unchanged():
    fact = next(t for t in meta_config.tables if t.name == "fact_order")
    col_names = {c.name for c in fact.columns}
    assert {"order_id", "customer_id", "product_id", "date_id", "region_id",
            "order_quantity", "order_amount"} <= col_names


def test_product_field_uses_manufacturing_wording():
    product = next(t for t in meta_config.tables if t.name == "dim_product")
    by_name = {c.name: c for c in product.columns}
    assert "产品" in by_name["product_name"].description
    assert "产品类别" in by_name["category"].description
    assert "产品类别" in by_name["category"].alias


def test_member_level_uses_customer_level_wording():
    customer = next(t for t in meta_config.tables if t.name == "dim_customer")
    member = next(c for c in customer.columns if c.name == "member_level")
    assert "客户等级" in member.description
    assert "客户等级" in member.alias


def test_order_amount_description_uses_sales_amount():
    fact = next(t for t in meta_config.tables if t.name == "fact_order")
    amount = next(c for c in fact.columns if c.name == "order_amount")
    assert "销售额" in amount.description
