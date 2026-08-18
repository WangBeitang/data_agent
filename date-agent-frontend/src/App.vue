<template>
  <div class="chat-page">
    <!-- 消息区 -->
    <div ref="messagesEl" class="messages">
      <div v-for="(msg, index) in messages" :key="index" :class="['message-row', msg.role]">
        <div v-if="msg.role === 'assistant'" class="avatar">🤖</div>

        <div class="bubble">
          <!-- 用户文本 -->
          <div v-if="msg.role === 'user'">{{ msg.content }}</div>

          <!-- 分析中 -->
          <div v-else-if="msg.loading && !msg.error" class="analysis-pending">
            <div v-if="msg.stages.length === 0">正在启动分析...</div>
            <AnalysisTimeline :stages="msg.stages" :has-error="!!msg.error" />
          </div>

          <!-- 完成的分析 -->
          <div v-else>
            <div v-if="msg.mode" class="route-badge">
              <span class="badge">{{ modeText(msg.mode.resolved) }}</span>
              <span v-if="msg.mode.source === 'forced'" class="badge-sub">强制模式</span>
            </div>
            <AnalysisTimeline v-if="msg.stages.length > 0" :stages="msg.stages" :has-error="!!msg.error" />
            <ResultTable v-if="msg.table" :columns="msg.table.columns" :rows="msg.table.rows" />
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
          @keyup.enter="sendQuestion"
          placeholder="请输入你的问题，例如：统计2025年各月销售额"
          :disabled="loading"
        />
        <button @click="sendQuestion" :disabled="loading">
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

const question = ref("");
const loading = ref(false);
const messages = ref([]);
const messagesEl = ref(null);

function scrollToBottom() {
  const el = messagesEl.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
}

function modeText(mode) {
  return mode === "attribution" ? "经营归因" : "普通问数";
}

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
    case "query_result":
      // 列只来自 table.columns / table.rows
      msg.table = { columns: data.table?.columns || [], rows: data.table?.rows || [] };
      break;
    case "error":
      msg.error = { code: data.code, message: data.message };
      break;
    case "done":
      // done 是结束 loading 的正式依据（连接关闭不算正常结束）
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

async function sendQuestion() {
  const q = question.value.trim();
  if (!q || loading.value) return;

  question.value = "";
  loading.value = true;

  messages.value.push({ role: "user", content: q });

  const msg = {
    role: "assistant",
    loading: true,
    stages: [],
    table: null,
    error: null,
    mode: null,
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
  overflow: hidden;
  background: #fff;
}

/* 消息区 */
.messages {
  height: 100%;
  overflow-y: auto;
  padding: 20px 20% 160px;
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

.avatar {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: #f3f4f6;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 10px;
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
