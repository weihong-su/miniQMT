<script setup lang="ts">
import { useAdviceTooltip } from '../composables/useAdviceTooltip'
const { state } = useAdviceTooltip()
</script>

<template>
  <Teleport to="body">
    <div v-if="state.visible && state.data" class="advice-tooltip" :style="{ left: state.x + 'px', top: state.y + 'px' }">
      <div class="advice-trend">{{ state.data.trend }}</div>
      <div class="advice-line">底仓：<b>{{ state.data.base_position }}</b>；网格：<b>{{ state.data.grid }}</b></div>
      <div class="advice-meta">
        <template v-if="state.data.cross">{{ state.data.cross }}<br></template>
        <template v-if="state.data.dif != null">DIF {{ state.data.dif }} / </template>DEA {{ state.data.dea }}｜{{ state.data.updated }}（{{ state.data.code }}）
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.advice-tooltip {
  position: absolute;
  z-index: 9999;
  background: #fff;
  border: 2px solid #d97706;
  border-radius: 8px;
  padding: 10px 14px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
  font-size: 13px;
  line-height: 1.6;
  min-width: 220px;
  max-width: 320px;
  pointer-events: none;
}
.advice-trend { font-weight: 700; font-size: 14px; margin-bottom: 6px; color: #78350f; }
.advice-line b { color: #b45309; }
.advice-meta { margin-top: 6px; padding-top: 6px; border-top: 1px solid #eee; color: #888; font-size: 12px; }
</style>
