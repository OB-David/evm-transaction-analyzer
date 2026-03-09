<script setup lang="ts">
import { ref } from 'vue'
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import BlockPanel from './components/BlockPanel.vue'
import { analyzeTransaction } from './api/analyze'

const currentTxHash = ref<string | null>(null)
const currentBlockNumber = ref<number | null>(null)
const highlightedBlockId = ref<number[] | null>(null)
const inputPanelRef = ref<InstanceType<typeof InputPanel> | null>(null)
const isAnalyzing = ref(false)

function handleAnalysisComplete(txHash: string) {
  currentTxHash.value = txHash
  console.log('Analysis complete for:', txHash)
}

function handleBlockNumberChanged(blockNum: number) {
  currentBlockNumber.value = blockNum
  console.log('Block number changed:', blockNum)
}

function handleBlockSelected(blockNum: number) {
  currentBlockNumber.value = blockNum
  if (inputPanelRef.value) {
    inputPanelRef.value.updateBlockNumber(blockNum)
  }
  console.log('Block selected from heatmap:', blockNum)
}

function handleLatestBlock(blockNum: number) {
  if (!currentBlockNumber.value) {
    if (inputPanelRef.value) {
      inputPanelRef.value.updateBlockNumber(blockNum)
    }
  }
}

async function handleTransactionSelected(txHash: string) {
  console.log('Transaction selected from heatmap:', txHash)

  // Update the input field with the selected transaction hash
  if (inputPanelRef.value) {
    inputPanelRef.value.updateTxHash(txHash)
  }

  // Show loading immediately
  isAnalyzing.value = true
  currentTxHash.value = null

  // First complete the analysis, THEN update the hash to trigger panel loading
  try {
    const res = await analyzeTransaction(txHash)
    if (res.status === 'success') {
      console.log('Analysis complete for selected transaction')
      // Now set the hash to trigger CFG/AFG loading
      await new Promise(resolve => setTimeout(resolve, 100))
      currentTxHash.value = txHash
    }
  } catch (e) {
    console.error('Failed to analyze selected transaction:', e)
  } finally {
    isAnalyzing.value = false
  }
}

function handleCfgNavigate(blockIds: number[] | null) {
  highlightedBlockId.value = blockIds
  console.log('Navigate to CFG blocks:', blockIds)
}
</script>

<template>
  <div class="app-grid">
    <TitleBar class="title-bar" />
    <div class="left-col">
      <InputPanel
        ref="inputPanelRef"
        class="input-panel"
        @analysis-complete="handleAnalysisComplete"
        @block-number-changed="handleBlockNumberChanged"
      />
      <BlockPanel
        class="block-panel"
        :block-number="currentBlockNumber"
        @transaction-selected="handleTransactionSelected"
        @block-selected="handleBlockSelected"
        @latest-block="handleLatestBlock"
      />
    </div>
    <div class="right-col">
      <AfgPanel
        class="afg-panel"
        :tx-hash="currentTxHash"
        :highlighted-block-id="highlightedBlockId"
        :is-analyzing="isAnalyzing"
        @cfg-navigate="handleCfgNavigate"
      />
      <CfgPanel
        class="cfg-panel"
        :tx-hash="currentTxHash"
        :highlighted-block-id="highlightedBlockId"
        :is-analyzing="isAnalyzing"
        @cfg-navigate="handleCfgNavigate"
      />
    </div>
  </div>
</template>

<style scoped>
.app-grid {
  height: 100vh;
  min-width: 900px;
  display: grid;
  grid-template-columns: 20% 80%;
  grid-template-rows: 36px 1fr;
  gap: 1px;
  background: var(--border);
  overflow: hidden;
}

.title-bar {
  grid-column: 1 / -1;
}

.left-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
  overflow: hidden;
  min-height: 0;
  min-width: 180px;
}

.input-panel {
  flex-shrink: 0;
}

.block-panel {
  flex: 1;
  min-height: 0;
}

.right-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
  min-height: 0;
  overflow: hidden;
}

.cfg-panel {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.afg-panel {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
