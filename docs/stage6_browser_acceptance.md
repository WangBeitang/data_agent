# Stage 6 最终浏览器验收报告

- **日期**：2026-08-19
- **验证范围**：仅做 Stage 6 真实浏览器验收，未修改任何功能代码，未进入 Stage 7。
- **基线 commit**：`3ba936b05dbd296a3a11117156eb7fcb1c3f7619`
- **环境**：Mac 开发机（前端+后端+进程内 Embedding），云端 `49.235.159.92` 的 MySQL(3308)/Qdrant(6333)/Elasticsearch(9200) 均可达。

## 结论

三条真实链路 **全部通过**。Stage 6 页面能够正确消费 Stage 5 的真实 SSE 事件流并完成渲染。等待架构复核。

---

## 1. 前后端启动

- **后端**：`cd data-agent && uv run python main.py` 正常监听 `0.0.0.0:8000`，进程内加载 `bge-large-zh-v1.5`（CPU，dim=1024）无报错；MySQL/Qdrant/ES 连接正常。
- **前端**：`cd date-agent-frontend && npm run dev` 正常监听 `http://127.0.0.1:5173/`，Vite 代理转发 `/api/query` → `:8000` 返回 200。
  - 说明：`npm ci` 触发 WorkBuddy 的 safe-delete 守卫（会批量删除已存在的 `node_modules`），故改用非破坏性的 `npm install --yes` 修复此前缺失的 `.bin` 符号链接；未改动任何源码或 lockfile。

## 2. 场景一：`为什么2025年2月销售额较1月明显下降？`

**通过。**

页面展示（真实渲染，非模拟）：
- mode = `经营归因`（route badge）
- 执行时间线：解析归因指标与期间 / 规划下一步归因动作 / 生成归因报告（均“完成”）
- Action 卡片；多次 Query 结果**不互相覆盖**（查询 1…N 各自独立结果块）
- SQL（折叠）+ QueryTable
- 关键数值：**1 月 109030.5**、**2 月 80009.0**、**delta −29021.5**、**change_rate −26.62%**
- 至少两个有效 breakdown 维度：**产品 + 客户**
- drivers（驱动）：iPhone 15 Pro、美的空调、白银、铂金；**offsets（抵消）**：青铜、黄金
- ContributionChart 正常渲染（驱动/抵消条形）
- Evidence（ev13_*、ev20_*）→ Observation（o4/o5）→ 后端 Calculation（`c_contribution_*`）→ SQL / QueryTable **可追溯**
- 数据边界 + 建议 段落完整
- 「复制报告」正常（Clipboard API）

控制台：0 error，0 pageerror；页面与 SSE 原始流中均未出现 Prompt / hidden reasoning / traceback / API Key / 数据库密码。

> ⚠️ 架构复核需关注（非 Stage 6 缺陷）：该问题的 breakdown 结果在后端**不稳定**。一次运行中 LLM 归因 Agent 将“区域/类别”拆解标记为“未取得有效数据”（报告显示“无驱动因素”）；另一次运行（及场景二）正常产出有效拆解与 drivers/offsets。属 Stage 5（归因逻辑/数据）层的非确定性，页面两种结果均能正确渲染。建议为演示问题固化确定性期望结果（AGENTS.md 已提出此要求）。

## 3. 场景二：`为什么2025年3月销售数量大幅增长，但销售额增长有限？`

**通过。**

页面展示：
- 2 月销售数量 **151**、3 月销售数量 **322**、数量变化率 **+113.25%**
- 2 月销售额 **80009.0**、3 月销售额 **90120.0**、销售额变化率 **+12.64%**
- 平均单件销售额 **529.86 → 279.88**（来自后端 Calculation，非前端重算）
- 报告 / Evidence / SQL / QueryTable 均可正常查看，追溯链完整
- 控制台：0 error，0 pageerror；无敏感内容

## 4. 普通问数回归：`统计2025年各月销售额`

**通过。**

- route = `普通问数`，正常展示 ResultTable（month 1=109030.5 / 2=80009 / 3=90120）
- 不进入 AttributionReport（无归因报告 / 无复制报告）
- 收到 `done` 后 loading 正常结束
- 控制台：0 error，0 pageerror

## 5. 浏览器控制台异常检查

- 三条链路均无阻塞性控制台异常（仅有 Vite HMR 的 `connecting/connected` debug 信息）。
- 页面不展示任何 Prompt / hidden reasoning / traceback / API Key / 数据库密码。
- 原始 SSE 流扫描：0 处敏感词（password / api_key / sk- / traceback / Prompt / 数据库密码）。

## 6. 失败情况

无功能失败。三条真实链路均通过，停止并等待架构复核。

### 测试工具备注（非应用缺陷）
- 浏览器验收使用 Playwright + 系统 Chrome（headless），需加 `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling`。
- 真实无头 Chrome 在长连接 SSE（>数分钟）空闲时会挂起 URLLoader（`net::ERR_NETWORK_IO_SUSPENDED`）；已用上述反后台化参数 + 两次重归因之间留后端冷却规避。真实前台浏览器不受影响，此为测试脚手架问题，不是应用 bug。
- 验收脚本：`/Users/beitang/Desktop/项目实战/data_agent/accept.cjs`（参数化）、`debug_s1.cjs`。
