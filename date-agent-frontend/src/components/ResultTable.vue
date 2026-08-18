<template>
  <div class="result-table-wrap">
    <div v-if="!columns || columns.length === 0" class="result-empty">
      无结果
    </div>
    <div v-else-if="rows.length === 0" class="result-empty">
      查询成功，但没有匹配的数据
    </div>
    <div v-else class="table-scroll">
      <table class="result-table">
        <thead>
          <tr>
            <th v-for="col in columns" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rIdx) in rows" :key="rIdx">
            <td v-for="col in columns" :key="col">{{ row[col] }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
// 列结构只来自 table.columns / table.rows（正式 query_result 契约）
// 空结果时展示空状态；禁止用 Object.keys(rows[0]) 推导列
defineProps({
  columns: { type: Array, default: () => [] },
  rows: { type: Array, default: () => [] },
});
</script>

<style scoped>
.result-table-wrap {
  max-width: 100%;
}
.table-scroll {
  max-width: 100%;
  overflow-x: auto;
}
.result-table {
  width: max-content;
  min-width: 100%;
  table-layout: auto;
  border-collapse: collapse;
}
.result-table th,
.result-table td {
  border: 1px solid #ddd;
  padding: 6px 12px;
  white-space: nowrap;
  font-size: 13px;
  text-align: left;
}
.result-table th {
  background: #fafafa;
  font-weight: 600;
}
.result-empty {
  padding: 10px 0;
  color: #999;
  font-size: 13px;
}
</style>
