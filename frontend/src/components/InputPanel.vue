<script setup lang="ts">
import { computed, ref } from 'vue'
import { normalizeAnalyzeError, type AnalyzeResult } from '../api/analyze'
import CopyButton from './CopyButton.vue'

const emit = defineEmits<{
  'analysis-requested': [txHash: string]
  'block-number-changed': [blockNumber: number]
}>()

const EXAMPLE_TX_HASH = '0x2d3f9a03793741f8155fc22371af0829ba934d4719f2ab54f58fbe0f37be4ea1'
const blockNumber = ref('')
const txHash = ref('')
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const blockStatus = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const blockErrorMsg = ref('')
const result = ref<AnalyzeResult | null>(null)
const progressLabel = computed(() => {
  switch (result.value?.stage) {
    case 'queued': return 'Analysis queued...'
    case 'analyzing': return 'Analyzing transaction...'
    case 'afg': return 'AFG ready · generating call tree...'
    case 'sequence': return 'Call tree ready · generating folded CFG...'
    case 'folded_cfg': return 'Folded CFG ready · generating plain CFG...'
    case 'plain_cfg': return 'CFG topology ready · computing folded details...'
    case 'folded_info': return 'Folded details ready · computing plain details...'
    default: return 'Processing transaction...'
  }
})

// Expose methods to update fields from parent
function updateTxHash(hash: string) {
  txHash.value = hash
}

function updateBlockNumber(num: number) {
  blockNumber.value = num.toString()
}

function setAnalyzeError(message: string) {
  status.value = 'error'
  errorMsg.value = normalizeAnalyzeError(message)
  result.value = null
}

function setAnalyzing(hash?: string) {
  if (hash) txHash.value = hash
  status.value = 'loading'
  errorMsg.value = ''
  result.value = null
}

function setAnalyzeSuccess() {
  status.value = 'success'
  errorMsg.value = ''
}

function setAnalysisProgress(progress: AnalyzeResult) {
  result.value = progress
  status.value = progress.status === 'processing' ? 'loading' : status.value
}

defineExpose({
  updateTxHash,
  updateBlockNumber,
  setAnalyzeError,
  setAnalyzing,
  setAnalyzeSuccess,
  setAnalysisProgress,
})

function onSubmit() {
  const hash = txHash.value.trim() || EXAMPLE_TX_HASH

  // If using example, make it editable in the input
  if (!txHash.value.trim()) {
    txHash.value = EXAMPLE_TX_HASH
  }

  if (!/^0x[0-9a-fA-F]{64}$/.test(hash)) {
    status.value = 'error'
    errorMsg.value = 'Invalid transaction hash'
    return
  }

  status.value = 'loading'
  errorMsg.value = ''
  result.value = null

  emit('analysis-requested', hash)
}

async function onBlockSubmit() {
  const blockNum = blockNumber.value.trim()
  if (!blockNum) return

  const num = parseInt(blockNum, 10)
  if (isNaN(num) || num < 0) {
    blockStatus.value = 'error'
    blockErrorMsg.value = 'Invalid block number'
    return
  }

  blockStatus.value = 'loading'
  blockErrorMsg.value = ''

  try {
    emit('block-number-changed', num)
    blockStatus.value = 'success'
  } catch (e: any) {
    blockStatus.value = 'error'
    blockErrorMsg.value = e.message || 'Failed to load block'
  }
}
</script>

<template>
  <div class="input-panel">
    <label class="panel-label">(A) Input</label>

    <!-- Block Number Input -->
    <div class="field-label-row">
      <span class="field-label">Block Number</span>
      <CopyButton :value="blockNumber" label="block number" />
    </div>
    <form class="input-form" @submit.prevent="onBlockSubmit">
      <input
        v-model="blockNumber"
        type="text"
        class="hash-input"
        placeholder="e.g., 12345678"
        :disabled="blockStatus === 'loading'"
      />
      <button
        type="submit"
        class="analyze-btn"
        :disabled="blockStatus === 'loading'"
      >
        {{ blockStatus === 'loading' ? 'Loading...' : 'Explore' }}
      </button>
    </form>

    <!-- Transaction Hash Input -->
    <div class="field-label-row">
      <span class="field-label">Tx Hash</span>
      <CopyButton :value="txHash" label="transaction hash" />
    </div>
    <form class="input-form" @submit.prevent="onSubmit">
      <input
        v-model="txHash"
        type="text"
        class="hash-input"
        :placeholder="EXAMPLE_TX_HASH"
      />
      <button
        type="submit"
        class="analyze-btn"
      >
        {{ status === 'loading' ? 'Switch' : 'Analyze' }}
      </button>
    </form>

    <div class="status-line">
      <span v-if="blockStatus === 'loading'" class="status-loading">Loading block...</span>
      <span v-else-if="blockStatus === 'error'" class="status-error">{{ blockErrorMsg }}</span>
      <span v-else-if="status === 'loading'" class="status-loading">{{ progressLabel }}</span>
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
  overflow: hidden;
}

.panel-label {
  font-size: 11px;
  color: #000000;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.field-label-row {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
}

.field-label {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.3px;
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
