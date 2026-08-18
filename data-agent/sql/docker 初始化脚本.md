可以，按下面顺序操作。密码不要发到对话里。

## 1. 从 Mac 上传初始化 SQL

先创建服务器目录：

```bash
ssh ubuntu@49.235.159.92 'mkdir -p ~/data-agent/mysql-init'
```

上传正式 SQL：

```bash
scp \
"/Users/beitang/Desktop/项目实战/data_agent/data-agent/sql/meta.sql" \
"/Users/beitang/Desktop/项目实战/data_agent/data-agent/sql/dw.sql" \
ubuntu@49.235.159.92:~/data-agent/mysql-init/
```

预期：服务器 `~/data-agent/mysql-init/` 下出现两个文件。

## 2. 在服务器生成应用密码

登录服务器后执行：

```bash
openssl rand -base64 24
```

保存输出，后面用作 `data_agent_app` 的密码。不要把密码发给我。

## 3. 进入 MySQL

```bash
docker exec -it customer-service-mysql mysql -uroot -p
```

输入现有 MySQL root 密码。

如果不知道 root 密码，从原 MySQL 部署使用的 `.env` 获取，不要重置。

## 4. 先检查同名数据库

在 MySQL 终端执行：

```sql
SELECT TABLE_SCHEMA, TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ('meta2', 'dw2')
ORDER BY TABLE_SCHEMA, TABLE_NAME;
```

停止条件：

- 如果返回了现有业务表，而且不确定是否可以覆盖，先停止，不执行后续 `SOURCE`。
- 如果没有返回记录，可以继续。

## 5. 创建数据库和专用账号

把下面的 `替换成刚生成的密码` 换成实际密码：

```sql
CREATE DATABASE IF NOT EXISTS meta2
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_general_ci;

CREATE DATABASE IF NOT EXISTS dw2
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_general_ci;

CREATE USER IF NOT EXISTS 'data_agent_app'@'%'
IDENTIFIED BY '替换成刚生成的密码';

ALTER USER 'data_agent_app'@'%'
IDENTIFIED BY '替换成刚生成的密码';

GRANT ALL PRIVILEGES ON meta2.* TO 'data_agent_app'@'%';
GRANT ALL PRIVILEGES ON dw2.* TO 'data_agent_app'@'%';

FLUSH PRIVILEGES;

SHOW GRANTS FOR 'data_agent_app'@'%';
```

预期最后看到对 `meta2.*` 和 `dw2.*` 的授权。

然后退出：

```sql
EXIT;
```

## 6. 把 SQL 复制进容器

在服务器 Shell 执行：

```bash
docker cp \
~/data-agent/mysql-init/meta.sql \
customer-service-mysql:/tmp/data_agent_meta.sql
```

```bash
docker cp \
~/data-agent/mysql-init/dw.sql \
customer-service-mysql:/tmp/data_agent_dw.sql
```

## 7. 执行初始化

重新进入 MySQL：

```bash
docker exec -it customer-service-mysql mysql -uroot -p
```

在 MySQL 终端执行：

```sql
SOURCE /tmp/data_agent_meta.sql;
SOURCE /tmp/data_agent_dw.sql;
```

如果出现 SQL 错误，先停止，把错误原文发给我，不要反复重跑。

## 8. 验证初始化结果

继续执行：

```sql
SHOW TABLES FROM meta2;
SHOW TABLES FROM dw2;

SELECT COUNT(*) AS meta_count FROM meta2.table_info;
SELECT COUNT(*) AS order_count FROM dw2.fact_order;
```

预期：

- `meta2` 有 4 张表；
- `meta_count` 为 0，后续元数据构建才会写入；
- `dw2` 有 5 张表；
- `order_count` 大于 0。

退出：

```sql
EXIT;
```

## 9. 验证应用账号

```bash
docker exec -it customer-service-mysql \
mysql -udata_agent_app -p \
-e "SELECT COUNT(*) AS order_count FROM dw2.fact_order; SHOW TABLES FROM meta2;"
```

输入刚才生成的应用密码。能查询成功，就说明账号、权限、库表均正常。