<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

const props = withDefaults(defineProps<{
  value: string
  label?: string
}>(), {
  label: 'value',
})

const copied = ref(false)
let resetTimer: number | null = null

async function writeToClipboard(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = value
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
  }
}

async function copy(): Promise<void> {
  if (!props.value) return
  await writeToClipboard(props.value)
  copied.value = true
  if (resetTimer !== null) window.clearTimeout(resetTimer)
  resetTimer = window.setTimeout(() => {
    copied.value = false
    resetTimer = null
  }, 1400)
}

onBeforeUnmount(() => {
  if (resetTimer !== null) window.clearTimeout(resetTimer)
})
</script>

<template>
  <button
    type="button"
    class="copy-btn"
    :class="{ copied }"
    :disabled="!value"
    :title="copied ? 'Copied' : `Copy ${label}`"
    :aria-label="copied ? `${label} copied` : `Copy ${label}`"
    @click="copy"
  >
    <span aria-hidden="true">{{ copied ? '✓' : '❐' }}</span>
    <span class="copy-feedback" aria-live="polite">{{ copied ? 'Copied' : '' }}</span>
  </button>
</template>

<style scoped>
.copy-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  flex-shrink: 0;
  transition: color 150ms ease, background-color 150ms ease, transform 150ms ease;
}

.copy-btn:hover:not(:disabled) {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.copy-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.copy-btn:active:not(:disabled) {
  transform: scale(0.88);
}

.copy-btn.copied {
  color: var(--success);
  background: color-mix(in srgb, var(--success) 12%, transparent);
}

.copy-btn:disabled {
  cursor: default;
  opacity: 0.35;
}

.copy-feedback {
  position: absolute;
  z-index: 5;
  left: 50%;
  bottom: calc(100% + 4px);
  transform: translateX(-50%);
  min-width: max-content;
  padding: 2px 5px;
  border-radius: 3px;
  background: #334155;
  color: #ffffff;
  font-size: 9px;
  line-height: 1.2;
  pointer-events: none;
  opacity: 0;
  transition: opacity 150ms ease;
}

.copy-feedback:not(:empty) {
  opacity: 1;
}
</style>
