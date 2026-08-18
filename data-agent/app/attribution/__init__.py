"""经营归因模块包（Stage 3 + Stage 4 + Stage 5）。

当前已实现：

- Stage 3：意图路由（IntentRouter）；
- Stage 4：确定性核心——Action Router / Normalizer / Calculator /
  Evidence Builder / 停止条件纯函数 / AttributionState / Context；
- Stage 5：Target Parser / Planner（含 retry + fallback）/ Attribution
  Graph / Report Generator（确定性装配 + LLM 有限语言组织）。

不新增 Repository / 数据库 Session / LLM Client Manager（复用
app.agent.llm.llm 与请求级 QueryService）。
"""
