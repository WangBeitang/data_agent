<template>
  <div class="attr-report">
    <!-- 状态横幅 -->
    <div v-if="report.status === 'partial'" class="rp-banner partial">
      <strong>分析取得部分证据</strong>
      <span v-if="done && done.message" class="rp-banner-msg">{{ done.message }}</span>
      <span v-else class="rp-banner-msg">以下结果基于已取得的证据，存在数据缺口。</span>
    </div>
    <div v-else-if="report.status === 'failed'" class="rp-banner failed">
      <strong>归因未完成（部分失败）</strong>
      <span v-if="done && done.message" class="rp-banner-msg">{{ done.message }}</span>
    </div>

    <div class="rp-toolbar">
      <h3 class="rp-title">归因报告</h3>
      <button class="rp-copy" @click="copyReport">
        {{ copyState === "ok" ? "已复制" : copyState === "fail" ? "复制失败" : "复制报告" }}
      </button>
    </div>

    <!-- 1. 问题定义 -->
    <section class="rp-section">
      <h4 class="rp-h">一、问题定义</h4>
      <p class="rp-p">{{ report.question_definition }}</p>
    </section>

    <!-- 2. 核心结论 -->
    <section class="rp-section">
      <h4 class="rp-h">二、核心结论</h4>
      <p class="rp-p">{{ report.core_conclusion }}</p>
    </section>

    <!-- 3. 指标概览 -->
    <section class="rp-section">
      <h4 class="rp-h">三、指标概览</h4>
      <div v-if="!report.metric_overview || !report.metric_overview.length" class="rp-empty">
        无指标概览
      </div>
      <table v-else class="rp-table">
        <thead>
          <tr>
            <th>指标</th>
            <th>对比期</th>
            <th>本期</th>
            <th>变化额</th>
            <th>变化率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(m, i) in report.metric_overview" :key="i">
            <td>{{ metricText(m.metric) }}</td>
            <td>
              {{ m.comparison_period_label }}：{{ fmtNum(m.comparison_value) }}
            </td>
            <td>{{ m.current_period_label }}：{{ fmtNum(m.current_value) }}</td>
            <td :class="deltaClass(m.delta)">{{ fmtNum(m.delta) }}</td>
            <td :class="deltaClass(m.delta)">{{ formatPct(m.change_rate) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 4. 主要驱动因素 -->
    <section class="rp-section">
      <h4 class="rp-h">四、主要驱动因素</h4>
      <div v-if="!report.drivers || !report.drivers.length" class="rp-empty">无驱动因素</div>
      <div v-else class="rp-factors">
        <div v-for="(f, i) in report.drivers" :key="'d' + i" class="rp-factor driver">
          <div class="rp-factor-head">
            <span class="rp-factor-tag driver">驱动</span>
            <span class="rp-factor-title">{{ f.title }}</span>
          </div>
          <div class="rp-factor-meta">
            <span>{{ metricText(f.metric) }}</span>
            <span v-if="f.dimension">· {{ dimText(f.dimension) }}</span>
            <span v-if="f.member">· {{ f.member }}</span>
            <span>· 变化额 {{ fmtNum(f.delta) }}</span>
            <span>· 贡献率 {{ formatPct(f.contribution_rate) }}</span>
          </div>
          <p class="rp-p">{{ f.summary }}</p>
          <div class="rp-evi-ids">依据：{{ eviIdsText(f.evidence_ids) }}</div>
        </div>
      </div>
    </section>

    <!-- 贡献条形图（原生 Vue + CSS） -->
    <section class="rp-section">
      <h4 class="rp-h">贡献分布</h4>
      <ContributionChart :drivers="report.drivers" :offsets="report.offsets" />
    </section>

    <!-- 5. 抵消因素 -->
    <section class="rp-section">
      <h4 class="rp-h">五、抵消因素</h4>
      <div v-if="!report.offsets || !report.offsets.length" class="rp-empty">无抵消因素</div>
      <div v-else class="rp-factors">
        <div v-for="(f, i) in report.offsets" :key="'o' + i" class="rp-factor offset">
          <div class="rp-factor-head">
            <span class="rp-factor-tag offset">抵消</span>
            <span class="rp-factor-title">{{ f.title }}</span>
          </div>
          <div class="rp-factor-meta">
            <span>{{ metricText(f.metric) }}</span>
            <span v-if="f.dimension">· {{ dimText(f.dimension) }}</span>
            <span v-if="f.member">· {{ f.member }}</span>
            <span>· 变化额 {{ fmtNum(f.delta) }}</span>
            <span>· 贡献率 {{ formatPct(f.contribution_rate) }}</span>
          </div>
          <p class="rp-p">{{ f.summary }}</p>
          <div class="rp-evi-ids">依据：{{ eviIdsText(f.evidence_ids) }}</div>
        </div>
      </div>
    </section>

    <!-- 6. Evidence 明细 + 追溯 -->
    <section class="rp-section">
      <h4 class="rp-h">六、证据明细</h4>
      <div v-if="!evidences.length" class="rp-empty">无证据</div>
      <div v-else class="rp-evidences">
        <div v-for="(e, i) in evidences" :key="e.evidence_id" class="rp-evidence">
          <div class="rp-evidence-head">
            <span class="rp-evidence-id">{{ e.evidence_id }}</span>
            <span class="rp-evidence-title">{{ e.title }}</span>
            <span v-if="e.direction" class="rp-evi-dir" :class="e.direction">
              {{ directionText(e.direction) }}
            </span>
          </div>
          <p class="rp-p">{{ e.statement }}</p>
          <div class="rp-evidence-meta">
            <span>指标：{{ metricText(e.metric) }}</span>
            <span v-if="e.dimension">· 维度：{{ dimText(e.dimension) }}</span>
            <span v-if="e.member">· 成员：{{ e.member }}</span>
            <span>· 观察：{{ (e.observation_ids || []).join(", ") }}</span>
            <span v-if="e.calculation_ids && e.calculation_ids.length">
              · 计算：{{ e.calculation_ids.join(", ") }}
            </span>
          </div>

          <!-- 追溯：每个 observation_id 独立判断，存在则展示 SQL/QueryTable，缺失则提示不可用 -->
          <div class="rp-trace">
            <template v-if="(e.observation_ids || []).length">
              <div
                v-for="obsId in e.observation_ids"
                :key="obsId"
                class="rp-trace-item"
              >
                <div class="rp-trace-head">
                  <span class="rp-trace-obs">{{ obsId }}</span>
                  <span
                    v-if="queryResultMap[obsId]"
                    class="tl-query-status"
                    :class="queryResultMap[obsId].status"
                  >
                    {{ queryStatusText(queryResultMap[obsId].status) }}
                  </span>
                </div>
                <template v-if="queryResultMap[obsId]">
                  <details class="rp-trace-sql">
                    <summary>查看 SQL</summary>
                    <pre>{{ queryResultMap[obsId].sql || "（无可用 SQL）" }}</pre>
                  </details>
                  <ResultTable
                    v-if="queryResultMap[obsId].table && queryResultMap[obsId].table.columns.length"
                    :columns="queryResultMap[obsId].table.columns"
                    :rows="queryResultMap[obsId].table.rows"
                  />
                </template>
                <div v-else class="rp-trace-unavail">查询明细不可用</div>
              </div>
            </template>
            <div v-else class="rp-trace-unavail">查询明细不可用</div>
          </div>
        </div>
      </div>
    </section>

    <!-- 7. 数据边界与建议 -->
    <section class="rp-section">
      <h4 class="rp-h">七、数据边界与建议</h4>
      <div class="rp-sub-h">数据边界</div>
      <ul v-if="report.data_boundaries && report.data_boundaries.length" class="rp-list">
        <li v-for="(b, i) in report.data_boundaries" :key="'b' + i">{{ b }}</li>
      </ul>
      <div v-else class="rp-empty">无</div>

      <div class="rp-sub-h">建议</div>
      <div v-if="report.recommendations && report.recommendations.length" class="rp-recos">
        <div v-for="(r, i) in report.recommendations" :key="'r' + i" class="rp-reco">
          <p class="rp-p">{{ r.text }}</p>
          <div class="rp-evi-ids">依据：{{ eviIdsText(r.evidence_ids) }}</div>
        </div>
      </div>
      <div v-else class="rp-empty">无</div>
    </section>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";
import ResultTable from "./ResultTable.vue";
import ContributionChart from "./ContributionChart.vue";

const props = defineProps({
  report: { type: Object, required: true },
  evidences: { type: Array, default: () => [] },
  queryResults: { type: Array, default: () => [] },
  done: { type: Object, default: null },
});

const METRIC_TEXT = {
  sales_amount: "销售额",
  sales_quantity: "销售数量",
  order_count: "销售订单数",
  avg_unit_sales_amount: "平均单件销售额",
};

const DIM_TEXT = {
  region: "销售区域",
  category: "产品类别",
  product: "产品",
  customer: "客户",
  customer_level: "客户等级",
};

const DIRECTION_TEXT = {
  driver: "驱动",
  offset: "抵消",
  neutral: "中性",
};

function metricText(m) {
  return METRIC_TEXT[m] || m;
}
function dimText(d) {
  return DIM_TEXT[d] || d;
}
function directionText(d) {
  return DIRECTION_TEXT[d] || d || "—";
}
function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  return String(Number(v));
}
function formatPct(v) {
  if (v === null || v === undefined) return "—";
  const p = Number(v) * 100;
  const s = p.toFixed(2) + "%";
  return p > 0 ? "+" + s : s;
}
function deltaClass(delta) {
  const v = Number(delta || 0);
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "";
}
function eviIdsText(ids) {
  if (!ids || !ids.length) return "—";
  return ids.join(", ");
}
function queryStatusText(status) {
  return { success: "成功", empty: "无数据", failed: "失败" }[status] || status;
}

const queryResultMap = computed(() => {
  const map = {};
  for (const q of props.queryResults || []) {
    if (q.observation_id) map[q.observation_id] = q;
  }
  return map;
});

// ===================== 报告复制 =====================
const copyState = ref("");

function buildReportText() {
  const r = props.report;
  const lines = [];
  lines.push("制造业销售经营归因分析报告");
  lines.push("=".repeat(32));
  lines.push("");

  lines.push("一、问题定义");
  lines.push(r.question_definition || "");
  lines.push("");

  lines.push("二、核心结论");
  lines.push(r.core_conclusion || "");
  lines.push("");

  lines.push("三、指标概览");
  for (const m of r.metric_overview || []) {
    lines.push(
      `- ${metricText(m.metric)}：${m.comparison_period_label} ${fmtNum(m.comparison_value)} → ` +
        `${m.current_period_label} ${fmtNum(m.current_value)}，变化额 ${fmtNum(m.delta)}，变化率 ${formatPct(m.change_rate)}`
    );
  }
  lines.push("");

  lines.push("四、主要驱动因素");
  for (const f of r.drivers || []) {
    lines.push(
      `- [驱动] ${f.title}（${metricText(f.metric)}` +
        `${f.dimension ? "·" + dimText(f.dimension) : ""}` +
        `${f.member ? "·" + f.member : ""}）：变化额 ${fmtNum(f.delta)}，贡献率 ${formatPct(f.contribution_rate)}`
    );
    lines.push(`  ${f.summary}`);
    lines.push(`  依据：${eviIdsText(f.evidence_ids)}`);
  }
  lines.push("");

  lines.push("五、抵消因素");
  for (const f of r.offsets || []) {
    lines.push(
      `- [抵消] ${f.title}（${metricText(f.metric)}` +
        `${f.dimension ? "·" + dimText(f.dimension) : ""}` +
        `${f.member ? "·" + f.member : ""}）：变化额 ${fmtNum(f.delta)}，贡献率 ${formatPct(f.contribution_rate)}`
    );
    lines.push(`  ${f.summary}`);
    lines.push(`  依据：${eviIdsText(f.evidence_ids)}`);
  }
  lines.push("");

  lines.push("六、证据明细");
  for (const e of props.evidences || []) {
    lines.push(
      `[${e.evidence_id}] ${e.title}：${e.statement}` +
        `（${metricText(e.metric)}` +
        `${e.dimension ? "·" + dimText(e.dimension) : ""}` +
        `${e.member ? "·" + e.member : ""}` +
        `${e.direction ? "·" + directionText(e.direction) : ""}；` +
        `观察 ${eviIdsText(e.observation_ids)}` +
        `${e.calculation_ids && e.calculation_ids.length ? "；计算 " + e.calculation_ids.join(", ") : ""}）`
    );
  }
  lines.push("");

  lines.push("七、数据边界与建议");
  lines.push("数据边界：");
  for (const b of r.data_boundaries || []) {
    lines.push(`- ${b}`);
  }
  lines.push("");
  lines.push("建议：");
  for (const rec of r.recommendations || []) {
    lines.push(`- ${rec.text}（依据：${eviIdsText(rec.evidence_ids)}）`);
  }
  lines.push("");

  return lines.join("\n");
}

async function copyReport() {
  const text = buildReportText();
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    copyState.value = "ok";
  } catch {
    try {
      fallbackCopy(text);
      copyState.value = "ok";
    } catch {
      copyState.value = "fail";
    }
  }
  setTimeout(() => {
    copyState.value = "";
  }, 2500);
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  const ok = document.execCommand("copy");
  document.body.removeChild(ta);
  if (!ok) throw new Error("copy failed");
}
</script>

<style scoped>
.attr-report {
  margin-top: 12px;
  border-top: 1px dashed #e3e6ea;
  padding-top: 12px;
}

.rp-banner {
  margin-bottom: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rp-banner.partial {
  background: #fff7e6;
  border: 1px solid #ffe7ba;
  color: #ad6800;
}
.rp-banner.failed {
  background: #fff1f0;
  border: 1px solid #ffccc7;
  color: #cf1322;
}
.rp-banner-msg {
  font-weight: 400;
  opacity: 0.9;
}

.rp-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.rp-title {
  margin: 0;
  font-size: 15px;
  color: #1f2d3d;
}
.rp-copy {
  padding: 5px 14px;
  border-radius: 8px;
  border: 1px solid #409eff;
  background: #fff;
  color: #409eff;
  font-size: 13px;
  cursor: pointer;
}
.rp-copy:hover {
  background: #ecf5ff;
}

.rp-section {
  margin-bottom: 14px;
}
.rp-h {
  margin: 0 0 6px;
  font-size: 14px;
  color: #2f6fed;
  border-left: 3px solid #409eff;
  padding-left: 8px;
}
.rp-sub-h {
  margin: 8px 0 4px;
  font-size: 13px;
  font-weight: 600;
  color: #555;
}
.rp-p {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: #333;
}
.rp-empty {
  font-size: 13px;
  color: #999;
}

.rp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.rp-table th,
.rp-table td {
  border: 1px solid #e3e6ea;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
}
.rp-table th {
  background: #f7f9fc;
  font-weight: 600;
}
.rp-table td.pos {
  color: #cf1322;
}
.rp-table td.neg {
  color: #2e7d4f;
}

.rp-factors {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rp-factor {
  padding: 10px 12px;
  border-radius: 8px;
  background: #fafbfc;
  border: 1px solid #eef0f3;
}
.rp-factor.driver {
  border-left: 3px solid #f56c6c;
}
.rp-factor.offset {
  border-left: 3px solid #67c23a;
}
.rp-factor-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rp-factor-tag {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 4px;
  color: #fff;
}
.rp-factor-tag.driver {
  background: #f56c6c;
}
.rp-factor-tag.offset {
  background: #67c23a;
}
.rp-factor-title {
  font-weight: 600;
  font-size: 13px;
  color: #333;
}
.rp-factor-meta {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: #666;
}
.rp-evi-ids {
  margin-top: 4px;
  font-size: 12px;
  color: #999;
}

.rp-evidences {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rp-evidence {
  padding: 10px 12px;
  border-radius: 8px;
  background: #fafbfc;
  border: 1px solid #eef0f3;
}
.rp-evidence-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rp-evidence-id {
  font-family: monospace;
  font-size: 12px;
  color: #409eff;
  background: #ecf5ff;
  border-radius: 4px;
  padding: 1px 6px;
}
.rp-evidence-title {
  font-weight: 600;
  font-size: 13px;
}
.rp-evi-dir {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  color: #fff;
}
.rp-evi-dir.driver {
  background: #f56c6c;
}
.rp-evi-dir.offset {
  background: #67c23a;
}
.rp-evidence-meta {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: #666;
}
.rp-trace {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rp-trace-item {
  padding: 6px 8px;
  background: #fff;
  border: 1px dashed #e3e6ea;
  border-radius: 6px;
}
.rp-trace-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rp-trace-obs {
  font-family: monospace;
  font-size: 12px;
  color: #666;
}
.rp-trace-sql {
  margin-top: 4px;
}
.rp-trace-sql summary {
  cursor: pointer;
  font-size: 12px;
  color: #2f6fed;
  user-select: none;
}
.rp-trace-sql pre {
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
.rp-trace-unavail {
  font-size: 12px;
  color: #b88200;
  background: #fff7e6;
  border-radius: 6px;
  padding: 6px 8px;
}

.rp-list {
  margin: 0;
  padding-left: 18px;
}
.rp-list li {
  font-size: 13px;
  line-height: 1.7;
  color: #555;
}
.rp-recos {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rp-reco {
  padding: 8px 10px;
  background: #fafbfc;
  border: 1px solid #eef0f3;
  border-radius: 8px;
}

/* SQL 状态标签（与 timeline 共用语义，局部定义） */
.tl-query-status {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
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
</style>
