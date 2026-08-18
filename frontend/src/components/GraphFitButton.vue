<script setup lang="ts">
const props = defineProps<{
  graphName: string
  actionLabel?: string
  activeLabel?: string
  disabled?: boolean
  loading?: boolean
  active?: boolean
}>()

const emit = defineEmits<{
  fit: []
}>()
</script>

<template>
  <button
    type="button"
    class="graph-fit-button"
    :class="{ active: props.active, loading: props.loading }"
    :disabled="props.disabled || props.loading"
    :aria-label="props.active
      ? (props.activeLabel || `Restore full ${props.graphName} layout`)
      : (props.actionLabel || `Fit ${props.graphName} to view`)"
    :aria-pressed="props.actionLabel ? !!props.active : undefined"
    :title="props.active
      ? (props.activeLabel || `Restore full ${props.graphName} layout`)
      : (props.actionLabel || 'Fit graph to view')"
    @click="emit('fit')"
  >
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <g v-if="props.loading" class="fit-spinner">
        <path d="M19 12a7 7 0 1 1-2.05-4.95" />
        <path d="M17 3.8v3.7h-3.7" />
      </g>
      <g v-else-if="props.actionLabel">
        <path d="M6 6h5M13 6h5M6 18h5M13 18h5M6 7.5v9M18 7.5v9" />
        <path d="M9.5 12h5" />
        <circle cx="6" cy="6" r="1.6" />
        <circle cx="18" cy="6" r="1.6" />
        <circle cx="6" cy="18" r="1.6" />
        <circle cx="18" cy="18" r="1.6" />
      </g>
      <g v-else>
        <rect x="4" y="4" width="16" height="16" rx="1.5" />
        <circle cx="12" cy="12" r="1.75" class="fit-center-dot" />
      </g>
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

.graph-fit-button:hover:not(:disabled),
.graph-fit-button.active {
  color: #334155;
  border-color: var(--accent);
  background: #f1f5f9;
}

.graph-fit-button:active:not(:disabled) {
  background: #e2e8f0;
}

.graph-fit-button:disabled {
  color: #94a3b8;
  background: rgba(248, 250, 252, 0.9);
  cursor: not-allowed;
  opacity: 0.62;
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

.graph-fit-button .fit-center-dot,
.graph-fit-button circle {
  fill: currentColor;
  stroke: none;
}

.fit-spinner {
  transform-origin: 12px 12px;
  animation: fit-spin 800ms linear infinite;
}

@keyframes fit-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .graph-fit-button {
    transition: none;
  }
  .fit-spinner { animation: none; }
}
</style>
