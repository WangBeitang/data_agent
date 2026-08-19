<template>
  <div class="chat-page">
    <header class="page-header">
      <div class="page-title">制造业销售经营归因分析工作台</div>
      <div class="page-sub">普通问数 / 经营归因 · 数据归因，结论可追溯到 SQL 与确定性计算</div>
      <div class="examples">
        <button
          v-for="ex in examples"
          :key="ex"
          class="example-chip"
          :disabled="loading"
          @click="sendQuestion(ex)"
        >
          {{ ex }}
        </button>
      </div>
    </header>

    <!-- 消息区 -->
    <div ref="messagesEl" class="messages">
      <div
        v-for="(msg, index) in messages"
        :key="index"
        :class="['message-row', msg.role, { attribution: isAttribution(msg) }]"
      >
        <div v-if="msg.role === 'assistant'" class="avatar">🤖</div>

        <div class="bubble">
          <!-- 用户文本 -->
          <div v-if="msg.role === 'user'">{{ msg.content }}</div>

          <!-- 分析中 -->
          <div v-else-if="msg.loading && !msg.error" class="analysis-pending">
            <div v-if="msg.stages.length === 0 && msg.actions.length === 0">正在启动分析...</div>
            <AnalysisTimeline
              :stages="msg.stages"
              :actions="msg.actions"
              :query-results="msg.queryResults"
              :mode="msg.mode?.resolved"
              :has-error="!!msg.error"
            />
          </div>

          <!-- 完成的分析 -->
          <div v-else>
            <div v-if="msg.mode" class="route-badge">
              <span class="badge">{{ modeText(msg.mode.resolved) }}</span>
              <span v-if="msg.mode.source === 'forced'" class="badge-sub">强制模式</span>
            </div>

            <AnalysisTimeline
              v-if="msg.stages.length > 0 || isAttribution(msg)"
              :stages="msg.stages"
              :actions="msg.actions"
              :query-results="msg.queryResults"
              :mode="msg.mode?.resolved"
              :has-error="!!msg.error"
            />

            <!-- 普通问数结果表 -->
            <ResultTable
              v-if="msg.table && !isAttribution(msg)"
              :columns="msg.table.columns"
              :rows="msg.table.rows"
            />

            <!-- 归因报告（含贡献图） -->
            <AttributionReport
              v-if="isAttribution(msg) && msg.report"
              :report="msg.report"
              :evidences="msg.evidences"
              :query-results="msg.queryResults"
              :done="msg.done"
            />

            <!-- 归因失败（无报告）：安全失败提示，不伪造报告 -->
            <div
              v-if="isAttribution(msg) && msg.done && msg.done.status === 'failed' && !msg.report"
              class="fail-box"
            >
              <div class="fail-title">归因未完成</div>
              <div class="fail-msg">
                {{ msg.done.message || (msg.error && msg.error.message) || "本次分析未能形成可用报告。" }}
              </div>
            </div>

            <div v-if="msg.error" class="error-text">
              <div v-if="msg.error.code" class="error-code">[{{ msg.error.code }}]</div>
              {{ msg.error.message }}
            </div>
          </div>
        </div>

        <div v-if="msg.role === 'user'" class="avatar">🧑</div>
      </div>
      <div class="messages-bottom-spacer"></div>
    </div>

    <!-- 悬浮输入框 -->
    <div class="input-wrapper">
      <div class="input-box">
        <input
          v-model="question"
          @keyup.enter="sendQuestion()"
          placeholder="请输入你的问题，例如：统计2025年各月销售额"
          :disabled="loading"
        />
        <button @click="sendQuestion()" :disabled="loading">
          {{ loading ? "执行中..." : "发送" }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { nextTick, ref } from "vue";
import { fetchAnalysisStream } from "./composables/useAnalysisStream.js";
import AnalysisTimeline from "./components/AnalysisTimeline.vue";
import ResultTable from "./components/ResultTable.vue";
import AttributionReport from "./components/AttributionReport.vue";

const question = ref("");
const loading = ref(false);
const messages = ref([]);
const messagesEl = ref(null);

// 至少三个示例问题：普通问数 + 两个冻结归因场景
const examples = [
  "统计2025年各月销售额",
  "为什么2025年2月销售额较1月明显下降？",
  "为什么2025年3月销售数量大幅增长，但销售额增长有限？",
];

function scrollToBottom() {
  const el = messagesEl.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
}

function modeText(mode) {
  return mode === "attribution" ? "经营归因" : "普通问数";
}

function isAttribution(msg) {
  return msg.mode?.resolved === "attribution";
}

function mergeEvidences(target, incoming) {
  const seen = new Set(target.map((e) => e.evidence_id));
  for (const e of incoming) {
    if (!seen.has(e.evidence_id)) {
      target.push(e);
      seen.add(e.evidence_id);
    }
  }
}

// 每条 assistant message 维护完整 attribution 状态（普通 query 复用 table 字段）
function handleEvent(msg, data) {
  switch (data.type) {
    case "route":
      msg.mode = {
        requested: data.requested_mode,
        resolved: data.resolved_mode,
        source: data.source,
      };
      break;
    case "stage":
      upsertStage(msg.stages, data);
      break;
    case "action_start":
      msg.actions.push({
        action: data.action,
        query_action_count: data.query_action_count,
        max_query_actions: data.max_query_actions,
      });
      break;
    case "query_result":
      if (data.mode === "attribution") {
        // 归因：追加，不覆盖前一次查询
        msg.queryResults.push({
          action_id: data.action_id,
          observation_id: data.observation_id,
          sub_query: data.sub_query,
          sql: data.sql,
          table: data.table,
          dimension: data.dimension,
          normalized_rows: data.normalized_rows || [],
          status: data.status,
          error: data.error,
        });
      } else {
        // 普通问数：保持 Stage 3 行为
        msg.table = { columns: data.table?.columns || [], rows: data.table?.rows || [] };
      }
      break;
    case "calculation":
      if (Array.isArray(data.calculations)) {
        for (const c of data.calculations) msg.calculations.push(c);
      }
      if (Array.isArray(data.evidences)) {
        // Evidence 以 evidence_id 去重合并
        mergeEvidences(msg.evidences, data.evidences);
      }
      break;
    case "report":
      msg.report = data.report;
      // report 事件携带的完整 evidences 作为最终 Evidence 集合
      if (Array.isArray(data.evidences)) {
        msg.evidences = data.evidences.slice();
      }
      break;
    case "error":
      msg.error = { code: data.code, message: data.message };
      break;
    case "done":
      msg.done = {
        status: data.status,
        query_count: data.query_count,
        has_report: data.has_report,
        message: data.message,
      };
      break;
    default:
      break;
  }
}

function upsertStage(stages, data) {
  const existing = stages.find((s) => s.stage_code === data.stage_code);
  if (existing) {
    existing.status = data.status;
    if (data.status === "success" || data.status === "failed") {
      existing.stage = data.stage;
    }
  } else {
    stages.push({ stage_code: data.stage_code, stage: data.stage, status: data.status });
  }
}

async function sendQuestion(override) {
  const q = (override ?? question.value).trim();
  if (!q || loading.value) return;

  if (override === undefined) question.value = "";
  loading.value = true;

  messages.value.push({ role: "user", content: q });

  const msg = {
    role: "assistant",
    loading: true,
    mode: null,
    stages: [],
    actions: [],
    queryResults: [],
    calculations: [],
    evidences: [],
    report: null,
    table: null,
    done: null,
    error: null,
  };
  messages.value.push(msg);

  await nextTick();
  scrollToBottom();

  try {
    await fetchAnalysisStream({
      query: q,
      mode: "auto",
      onEvent: (data) => handleEvent(msg, data),
    });
  } catch (e) {
    if (!msg.error) {
      msg.error = { code: "NETWORK_ERROR", message: e?.message || "请求失败" };
    }
  } finally {
    msg.loading = false;
    loading.value = false;
    await nextTick();
    scrollToBottom();
  }
}
</script>

<style scoped>
/* 覆盖 Vite 默认居中 */
:global(html),
:global(body) {
  height: 100%;
  margin: 0;
}
:global(body) {
  display: block !important;
  place-items: unset !important;
}
:global(#app) {
  height: 100%;
  max-width: none !important;
  margin: 0 !important;
  padding: 0 !important;
}

/* 页面 */
.chat-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
}

/* 顶部 */
.page-header {
  padding: 14px 24px 10px;
  border-bottom: 1px solid #eee;
  background: #fff;
  flex-shrink: 0;
}
.page-title {
  font-size: 16px;
  font-weight: 700;
  color: #1f2d3d;
}
.page-sub {
  margin-top: 2px;
  font-size: 12px;
  color: #999;
}
.examples {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.example-chip {
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid #d6e4ff;
  background: #f5f9ff;
  color: #2f6fed;
  font-size: 12px;
  cursor: pointer;
}
.example-chip:hover {
  background: #e6f0ff;
}
.example-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 消息区 */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 18px 6% 160px;
}

.message-row {
  display: flex;
  margin-bottom: 14px;
}
.message-row.assistant {
  justify-content: flex-start;
}
.message-row.user {
  justify-content: flex-end;
}
/* 归因消息占满宽度，承载更宽的报告面板 */
.message-row.attribution {
  width: 100%;
}

.avatar {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: #f3f4f6;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 10px;
  flex-shrink: 0;
}

.bubble {
  max-width: min(820px, 72%);
  padding: 12px 14px;
  border-radius: 12px;
  background: #f5f5f5;
}
.message-row.user .bubble {
  background: #e6f4ff;
}
.message-row.attribution .bubble {
  max-width: 100%;
  width: 100%;
  background: #fafbfc;
  border: 1px solid #eef0f3;
}

/* 路由徽标 */
.route-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  background: #eef4ff;
  color: #2f6fed;
  font-size: 12px;
  font-weight: 600;
}
.badge-sub {
  color: #999;
  font-size: 12px;
}

/* 失败提示 */
.fail-box {
  margin-top: 10px;
  padding: 12px 14px;
  border-radius: 10px;
  background: #fff1f0;
  border: 1px solid #ffccc7;
}
.fail-title {
  font-weight: 700;
  color: #cf1322;
  margin-bottom: 4px;
}
.fail-msg {
  font-size: 13px;
  color: #7a2e2e;
}

/* 错误 */
.error-text {
  color: #e74c3c;
  font-weight: 600;
  margin-top: 6px;
}
.error-code {
  font-size: 12px;
  font-weight: 400;
  opacity: 0.7;
}

/* 悬浮输入框 */
.input-wrapper {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 24px;
  display: flex;
  justify-content: center;
  padding: 0 16px;
  pointer-events: none;
}

.input-box {
  pointer-events: auto;
  width: 100%;
  max-width: 720px;
  display: flex;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.12);
}

.input-box input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font-size: 15px;
}

.input-box button {
  padding: 8px 18px;
  border-radius: 999px;
  border: none;
  background: linear-gradient(135deg, #409eff, #66b1ff);
  color: #fff;
  cursor: pointer;
}
.input-box button:disabled {
  opacity: 0.5;
}

.messages-bottom-spacer {
  height: 200px;
}
</style>
