<script setup lang="ts">
import { ref } from 'vue'
import { analyzeTransaction, fetchTransactionBlock, type AnalyzeResult } from '../api/analyze'

async function copyToClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

const emit = defineEmits<{
  'analysis-complete': [txHash: string]
  'block-number-changed': [blockNumber: number]
}>()

const EXAMPLE_TX_HASH = '0x840ecb2b5d55a682afd529138b36e97992eda9706e206237b57ec4697e4f8186'
const blockNumber = ref('')
const txHash = ref('')
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const blockStatus = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const blockErrorMsg = ref('')
const result = ref<AnalyzeResult | null>(null)

// Expose methods to update fields from parent
function updateTxHash(hash: string) {
  txHash.value = hash
}

function updateBlockNumber(num: number) {
  blockNumber.value = num.toString()
}

defineExpose({
  updateTxHash,
  updateBlockNumber,
})

async function onSubmit() {
  const hash = txHash.value.trim() || EXAMPLE_TX_HASH

  // If using example, make it editable in the input
  if (!txHash.value.trim()) {
    txHash.value = EXAMPLE_TX_HASH
  }

  status.value = 'loading'
  errorMsg.value = ''
  result.value = null

  try {
    const res = await analyzeTransaction(hash)
    if (res.status === 'success') {
      status.value = 'success'
      result.value = res
      emit('analysis-complete', hash)

      // Fetch block number for this transaction
      try {
        const blockNum = await fetchTransactionBlock(hash)
        blockNumber.value = blockNum.toString()
        emit('block-number-changed', blockNum)
      } catch (e) {
        console.error('Failed to fetch block number:', e)
      }
    } else {
      status.value = 'error'
      errorMsg.value = res.error || 'Analysis failed'
    }
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Network error'
  }
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
    <label class="panel-label">(a) Input</label>

    <!-- Block Number Input -->
    <div class="field-label-row">
      <span class="field-label">Block Number</span>
      <button class="copy-btn" @click="copyToClipboard(blockNumber)" title="Copy">&#x2750;</button>
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
      <button class="copy-btn" @click="copyToClipboard(txHash)" title="Copy">&#x2750;</button>
    </div>
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
      <span v-if="blockStatus === 'loading'" class="status-loading">Loading block...</span>
      <span v-else-if="blockStatus === 'error'" class="status-error">{{ blockErrorMsg }}</span>
      <span v-else-if="status === 'loading'" class="status-loading">Processing transaction...</span>
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

.copy-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--muted);
  font-size: 12px;
  padding: 0 2px;
  line-height: 1;
}

.copy-btn:hover {
  color: var(--accent);
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
