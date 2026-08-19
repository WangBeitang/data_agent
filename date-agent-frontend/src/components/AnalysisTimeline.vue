<template>
  <div class="analysis-timeline">
    <!-- 阶段 -->
    <div v-for="(s, i) in stages" :key="'s' + i" class="timeline-item">
      <span class="timeline-dot" :class="s.status"></span>
      <span class="timeline-label">{{ s.stage || s.stage_code }}</span>
      <span class="timeline-status" :class="s.status">{{ statusText(s.status) }}</span>
    </div>

    <!-- 归因：Action + 查询执行结果 -->
    <template v-if="mode === 'attribution'">
      <div v-for="(a, i) in actions" :key="'a' + i" class="tl-action">
        <div class="tl-action-head">
          <span class="tl-action-type">{{ actionTypeText(a.action.type) }}</span>
          <span class="tl-action-count">
            查询 {{ a.query_action_count }}/{{ a.max_query_actions }}
          </span>
        </div>
        <div class="tl-action-reason">{{ a.action.reason }}</div>
        <div class="tl-action-meta">
          <span v-if="a.action.dimension" class="tl-tag">
            维度：{{ dimText(a.action.dimension) }}
          </span>
          <span v-if="a.action.metrics && a.action.metrics.length" class="tl-tag">
            指标：{{ metricsText(a.action.metrics) }}
          </span>
        </div>

        <div
          v-for="(q, qi) in queryResultsByAction(a.action.action_id)"
          :key="'q' + i + '_' + qi"
          class="tl-query"
        >
          <div class="tl-query-head">
            <span class="tl-query-sub">{{ q.sub_query }}</span>
            <span class="tl-query-status" :class="q.status">{{ queryStatusText(q.status) }}</span>
          </div>
          <details class="tl-sql">
            <summary>查看 SQL</summary>
            <pre>{{ q.sql || "（无可用 SQL）" }}</pre>
          </details>
          <ResultTable
            v-if="q.table && q.table.columns && q.table.columns.length"
            :columns="q.table.columns"
            :rows="q.table.rows"
          />
          <div v-else-if="q.status === 'empty'" class="tl-empty">查询成功，但没有匹配的数据</div>
          <div v-else-if="q.status === 'failed'" class="tl-empty tl-empty-fail">
            {{ q.error || "查询失败" }}
          </div>
        </div>
      </div>
    </template>

    <div v-if="hasError" class="timeline-item">
      <span class="timeline-dot failed"></span>
      <span class="timeline-label">分析失败</span>
    </div>
  </div>
</template>

<script setup>
import ResultTable from "./ResultTable.vue";

const props = defineProps({
  stages: { type: Array, default: () => [] },
  actions: { type: Array, default: () => [] },
  queryResults: { type: Array, default: () => [] },
  mode: { type: String, default: null },
  hasError: { type: Boolean, default: false },
});

const ACTION_TEXT = {
  compare_period: "总体比较",
  breakdown_region: "按销售区域拆解",
  breakdown_category: "按产品类别拆解",
  breakdown_product: "按产品拆解",
  breakdown_customer: "按客户/客户等级拆解",
  analyze_unit_price: "平均单件销售额分析",
  calculate_contribution: "贡献计算",
  finish_analysis: "结束分析",
};

const DIM_TEXT = {
  region: "销售区域",
  category: "产品类别",
  product: "产品",
  customer: "客户",
  customer_level: "客户等级",
};

const METRIC_TEXT = {
  sales_amount: "销售额",
  sales_quantity: "销售数量",
  order_count: "销售订单数",
  avg_unit_sales_amount: "平均单件销售额",
};

function statusText(status) {
  return (
    { running: "执行中", success: "完成", failed: "失败" }[status] || status
  );
}

function queryStatusText(status) {
  return { success: "成功", empty: "无数据", failed: "失败" }[status] || status;
}

function actionTypeText(type) {
  return ACTION_TEXT[type] || type;
}

function dimText(dim) {
  return DIM_TEXT[dim] || dim;
}

function metricsText(metrics) {
  return (metrics || []).map((m) => METRIC_TEXT[m] || m).join("、");
}

function queryResultsByAction(actionId) {
  return props.queryResults.filter((q) => q.action_id === actionId);
}
</script>

<style scoped>
.analysis-timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}
.timeline-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.timeline-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.timeline-dot.running {
  background: #f1c40f;
  animation: pulse 1s infinite;
}
.timeline-dot.success {
  background: #2ecc71;
}
.timeline-dot.failed {
  background: #e74c3c;
}
.timeline-label {
  color: #333;
}
.timeline-status {
  margin-left: auto;
  color: #999;
  font-size: 12px;
}
.timeline-status.running {
  color: #f1c40f;
}
.timeline-status.success {
  color: #2ecc71;
}
.timeline-status.failed {
  color: #e74c3c;
}

/* Action 卡片 */
.tl-action {
  margin: 8px 0 8px 18px;
  padding: 10px 12px;
  border-left: 3px solid #409eff;
  background: #f7fbff;
  border-radius: 0 8px 8px 0;
}
.tl-action-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tl-action-type {
  font-weight: 700;
  color: #2f6fed;
  font-size: 13px;
}
.tl-action-count {
  font-size: 12px;
  color: #999;
}
.tl-action-reason {
  margin-top: 4px;
  font-size: 13px;
  color: #444;
}
.tl-action-meta {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.tl-tag {
  font-size: 12px;
  color: #666;
  background: #eef2f7;
  border-radius: 6px;
  padding: 2px 8px;
}

/* Query 结果 */
.tl-query {
  margin-top: 8px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid #eef0f3;
  border-radius: 8px;
}
.tl-query-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tl-query-sub {
  font-size: 13px;
  color: #333;
}
.tl-query-status {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
  flex-shrink: 0;
}
.tl-query-status.success {
  background: #e8f8ee;
  color: #2e7d4f;
}
.tl-query-status.empty {
  background: #fff7e6;
  color: #b88200;
}
.tl-query-status.failed {
  background: #fdecea;
  color: #cf1322;
}
.tl-sql {
  margin-top: 6px;
}
.tl-sql summary {
  cursor: pointer;
  font-size: 12px;
  color: #2f6fed;
  user-select: none;
}
.tl-sql pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  background: #1e1e1e;
  color: #e6e6e6;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.tl-empty {
  margin-top: 6px;
  font-size: 12px;
  color: #999;
}
.tl-empty-fail {
  color: #cf1322;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
</style>
