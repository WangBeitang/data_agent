# MySQL 初始化说明

正式初始化文件：

- `meta.sql`：创建 `meta2` 及 4 张元数据表；首次执行后表为空，后续由 `build_meta_knowledge` 写入。
- `dw.sql`：创建 `dw2`、当前电商演示表和演示数据。

两个脚本都会 `DROP TABLE IF EXISTS` 后重建目标表。执行前必须检查 `meta2`、`dw2` 是否已有需要保留的表和数据；不要在未知数据库上直接重跑。

推荐为应用创建独立账号 `data_agent_app`，并仅授权 `meta2.*`、`dw2.*`。密码由用户在服务器本地生成和保存，不写入本文件。

初始化完成后的预期状态：

- `meta2` 有 `table_info`、`column_info`、`metric_info`、`column_metric`；首次初始化后为空。
- `dw2` 有 `dim_region`、`dim_customer`、`dim_product`、`dim_date`、`fact_order`，并包含演示数据。
