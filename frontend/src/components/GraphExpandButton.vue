<script setup lang="ts">
const props = defineProps<{
  expanded: boolean
  graphName: string
}>()

const emit = defineEmits<{
  toggle: []
}>()
</script>

<template>
  <button
    type="button"
    class="graph-expand-button"
    :class="{ expanded: props.expanded }"
    :aria-label="props.expanded ? `Exit expanded ${props.graphName} view` : `Expand ${props.graphName} view`"
    :aria-pressed="props.expanded"
    :title="props.expanded ? 'Exit expanded view (Esc)' : 'Expand graph'"
    @click="emit('toggle')"
  >
    <svg v-if="!props.expanded" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M9 3H3v6M3 3l7 7M15 3h6v6M21 3l-7 7M3 15v6h6M3 21l7-7M21 15v6h-6M21 21l-7-7" />
    </svg>
    <svg v-else viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 3l7 7M10 4v6H4M21 3l-7 7M14 4v6h6M3 21l7-7M10 20v-6H4M21 21l-7-7M14 20v-6h6" />
    </svg>
  </button>
</template>

<style scoped>
.graph-expand-button {
  position: relative;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(148, 163, 184, 0.55);
  border-radius: 4px;
  color: #475569;
  background: rgba(255, 255, 255, 0.94);
  cursor: pointer;
  touch-action: manipulation;
  transition: color 180ms ease, border-color 180ms ease, background 180ms ease, box-shadow 180ms ease;
}

.graph-expand-button::before {
  content: '';
  position: absolute;
  inset: -8px;
}

.graph-expand-button:hover {
  color: #334155;
  border-color: var(--accent);
  background: #f1f5f9;
}

.graph-expand-button:active {
  background: #e2e8f0;
}

.graph-expand-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.graph-expand-button.expanded {
  color: #334155;
  border-color: var(--accent);
  background: #eef2f7;
  box-shadow: 0 0 0 1px rgba(134, 152, 178, 0.16);
}

.graph-expand-button svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
  .graph-expand-button {
    transition: none;
  }
}
</style>
