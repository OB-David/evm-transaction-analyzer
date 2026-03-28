<script setup lang="ts">
const props = withDefaults(defineProps<{
  exportDisabled?: boolean
}>(), {
  exportDisabled: false,
})

const emit = defineEmits<{
  'export-visible-svgs': []
  'export-full-ui-svg': []
}>()

function handleExportClick() {
  if (props.exportDisabled) return
  emit('export-visible-svgs')
}

function handleExportFullUiClick() {
  if (props.exportDisabled) return
  emit('export-full-ui-svg')
}
</script>

<template>
  <div class="title-bar">
    <span class="title-text">TraceWeaver: Visual Analytics of Ethereum Execution Traces for Arbitrage Identification</span>
    <span class="actions">
      <button
        class="export-btn"
        :disabled="exportDisabled"
        @click="handleExportClick"
      >
        Export Visible SVGs
      </button>
      <button
        class="export-btn"
        :disabled="exportDisabled"
        @click="handleExportFullUiClick"
      >
        Export Full UI SVG
      </button>
    </span>
  </div>
</template>

<style scoped>
.title-bar {
  background: #7B8FAD;
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 10px 0 14px;
  color: #FFFFFF;
  letter-spacing: 0.4px;
}

.title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.export-btn {
  border: 1px solid rgba(255, 255, 255, 0.64);
  background: rgba(255, 255, 255, 0.16);
  color: #FFFFFF;
  border-radius: 999px;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.2px;
  padding: 3px 11px;
  cursor: pointer;
  flex-shrink: 0;
}

.export-btn:hover:enabled {
  background: rgba(255, 255, 255, 0.24);
}

.export-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
