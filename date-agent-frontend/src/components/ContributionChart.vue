<template>
  <div class="contribution-chart">
    <div v-if="!items.length" class="cc-empty">暂无贡献数据</div>
    <div v-else class="cc-bars">
      <div v-for="(it, i) in items" :key="i" class="cc-row">
        <div class="cc-label">
          <span class="cc-dir" :class="dirClass(it.direction)">{{ dirText(it.direction) }}</span>
          <span class="cc-member" :title="it.member">{{ it.member }}</span>
        </div>
        <div class="cc-track">
          <div
            class="cc-bar"
            :class="barClass(it.delta)"
            :style="{ width: barWidth(it.delta) }"
          >
            <span class="cc-val">{{ fmtDelta(it.delta) }}</span>
          </div>
        </div>
        <div class="cc-rate">
          <template v-if="it.contribution_rate !== null && it.contribution_rate !== undefined">
            贡献率 {{ formatPct(it.contribution_rate) }}
          </template>
          <template v-else>贡献率 —</template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  drivers: { type: Array, default: () => [] },
  offsets: { type: Array, default: () => [] },
});

// 合并 drivers + offsets；delta 为条形长度主要依据，不重新计算后端数值
const items = computed(() => {
  const list = [];
  for (const d of props.drivers || []) {
    list.push({ ...d, direction: "driver" });
  }
  for (const o of props.offsets || []) {
    list.push({ ...o, direction: "offset" });
  }
  return list;
});

const maxAbs = computed(() => {
  let m = 0;
  for (const it of items.value) {
    const v = Math.abs(Number(it.delta) || 0);
    if (v > m) m = v;
  }
  return m;
});

function barWidth(delta) {
  const v = Math.abs(Number(delta) || 0);
  if (maxAbs.value <= 0) return "0%";
  // 同一图以最大 abs(delta) 归一化宽度；delta=0 显示最小标记
  const pct = (v / maxAbs.value) * 100;
  return Math.max(pct, 3) + "%";
}

function barClass(delta) {
  const v = Number(delta) || 0;
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "zero";
}

function dirClass(direction) {
  return direction === "offset" ? "offset" : "driver";
}

function dirText(direction) {
  return direction === "offset" ? "抵消" : "驱动";
}

function fmtDelta(delta) {
  if (delta === null || delta === undefined) return "—";
  return String(Number(delta));
}

function formatPct(v) {
  if (v === null || v === undefined) return "—";
  const p = Number(v) * 100;
  const s = p.toFixed(2) + "%";
  return p > 0 ? "+" + s : s;
}
</script>

<style scoped>
.contribution-chart {
  margin: 10px 0;
}
.cc-empty {
  padding: 12px;
  color: #999;
  font-size: 13px;
  background: #fafafa;
  border-radius: 8px;
  text-align: center;
}
.cc-bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cc-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.cc-label {
  width: 160px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
}
.cc-dir {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  flex-shrink: 0;
  color: #fff;
}
.cc-dir.driver {
  background: #f56c6c;
}
.cc-dir.offset {
  background: #67c23a;
}
.cc-member {
  font-size: 13px;
  color: #333;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cc-track {
  flex: 1;
  background: #f0f2f5;
  border-radius: 6px;
  height: 22px;
  overflow: hidden;
  display: flex;
  align-items: center;
}
.cc-bar {
  height: 100%;
  min-width: 2px;
  display: flex;
  align-items: center;
  padding-left: 6px;
  border-radius: 6px;
  box-sizing: border-box;
}
.cc-bar.pos {
  background: #f56c6c;
}
.cc-bar.neg {
  background: #67c23a;
}
.cc-bar.zero {
  background: #c0c4cc;
}
.cc-val {
  font-size: 12px;
  color: #fff;
  white-space: nowrap;
}
.cc-rate {
  width: 120px;
  flex-shrink: 0;
  font-size: 12px;
  color: #888;
  text-align: right;
}
</style>
