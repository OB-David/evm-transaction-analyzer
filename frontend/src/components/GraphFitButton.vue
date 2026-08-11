<script setup lang="ts">
const props = defineProps<{
  graphName: string
}>()

const emit = defineEmits<{
  fit: []
}>()
</script>

<template>
  <button
    type="button"
    class="graph-fit-button"
    :aria-label="`Fit ${props.graphName} to view`"
    title="Fit graph to view"
    @click="emit('fit')"
  >
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 8V3h5M16 3h5v5M21 16v5h-5M8 21H3v-5" />
      <path d="M8 8l3 3M16 8l-3 3M16 16l-3-3M8 16l3-3" />
    </svg>
  </button>
</template>

<style scoped>
.graph-fit-button {
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
  transition: color 180ms ease, border-color 180ms ease, background 180ms ease;
}

.graph-fit-button::before {
  content: '';
  position: absolute;
  inset: -8px;
}

.graph-fit-button:hover {
  color: #334155;
  border-color: var(--accent);
  background: #f1f5f9;
}

.graph-fit-button:active {
  background: #e2e8f0;
}

.graph-fit-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.graph-fit-button svg {
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
  .graph-fit-button {
    transition: none;
  }
}
</style>
