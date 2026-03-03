<script setup lang="ts">
import { ref } from 'vue'
import { analyzeTransaction, type AnalyzeResult } from '../api/analyze'

const emit = defineEmits<{
  'analysis-complete': [txHash: string]
}>()

const EXAMPLE_TX_HASH = '0x840ecb2b5d55a682afd529138b36e97992eda9706e206237b57ec4697e4f8186'
const txHash = ref('')
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const result = ref<AnalyzeResult | null>(null)

async function onSubmit() {
  const hash = txHash.value.trim() || EXAMPLE_TX_HASH

  status.value = 'loading'
  errorMsg.value = ''
  result.value = null

  try {
    const res = await analyzeTransaction(hash)
    if (res.status === 'success') {
      status.value = 'success'
      result.value = res
      emit('analysis-complete', hash)
    } else {
      status.value = 'error'
      errorMsg.value = res.error || 'Analysis failed'
    }
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Network error'
  }
}
</script>

<template>
  <div class="input-panel">
    <label class="panel-label"> (a) Input</label>
    <form class="input-form" @submit.prevent="onSubmit">
      <input
        v-model="txHash"
        type="text"
        class="hash-input"
        :placeholder="EXAMPLE_TX_HASH"
        :disabled="status === 'loading'"
      />
      <button
        type="submit"
        class="analyze-btn"
        :disabled="status === 'loading'"
      >
        {{ status === 'loading' ? 'Analyzing...' : 'Analyze' }}
      </button>
    </form>
    <div class="status-line">
      <span v-if="status === 'loading'" class="status-loading">Processing transaction...</span>
      <span v-else-if="status === 'success'" class="status-success">Done — {{ result?.files.length }} files generated</span>
      <span v-else-if="status === 'error'" class="status-error">{{ errorMsg }}</span>
    </div>
  </div>
</template>

<style scoped>
.input-panel {
  background: var(--panel-bg);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.panel-label {
  font-size: 11px;
  color: var(--muted);

  letter-spacing: 0.5px;
}

.input-form {
  display: flex;
  gap: 6px;
}

.hash-input {
  flex: 1;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 2px;
  background: var(--bg);
  color: var(--text);
  outline: none;
}

.hash-input:focus {
  border-color: var(--accent);
}

.hash-input::placeholder {
  color: #a0a0a0;
  opacity: 1;
}

.analyze-btn {
  padding: 4px 14px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 2px;
  cursor: pointer;
  font-size: 12px;
  white-space: nowrap;
}

.analyze-btn:hover:not(:disabled) {
  background: var(--accent-hover);
}

.analyze-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.status-line {
  font-size: 11px;
  min-height: 14px;
}

.status-loading { color: var(--accent); }
.status-success { color: var(--success); }
.status-error { color: var(--error); }
</style>
