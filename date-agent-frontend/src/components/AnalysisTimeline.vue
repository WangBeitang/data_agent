<template>
  <div class="analysis-timeline">
    <div v-if="stages.length === 0 && !hasError" class="timeline-empty">
      <span class="timeline-dot running"></span>
      <span>等待分析开始...</span>
    </div>

    <div v-for="(s, i) in stages" :key="i" class="timeline-item">
      <span class="timeline-dot" :class="s.status"></span>
      <span class="timeline-label">{{ s.stage || s.stage_code }}</span>
      <span class="timeline-status" :class="s.status">{{ statusText(s.status) }}</span>
    </div>

    <div v-if="hasError" class="timeline-item">
      <span class="timeline-dot failed"></span>
      <span class="timeline-label">分析失败</span>
    </div>
  </div>
</template>

<script setup>
// 只展示正式 stage / status（running / success / failed），不展示模型隐藏推理
defineProps({
  stages: { type: Array, default: () => [] },
  hasError: { type: Boolean, default: false },
});

function statusText(status) {
  return (
    {
      running: "执行中",
      success: "完成",
      failed: "失败",
    }[status] || status
  );
}
</script>

<style scoped>
.analysis-timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}
.timeline-item,
.timeline-empty {
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
