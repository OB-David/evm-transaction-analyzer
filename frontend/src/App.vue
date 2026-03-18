<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import SequencePanel from './components/SequencePanel.vue'
import BlockPanel from './components/BlockPanel.vue'
import { analyzeTransaction, fetchEdgeStepMap, type CfgMode, type EdgeStepMap } from './api/analyze'

const currentTxHash = ref<string | null>(null)
const currentBlockNumber = ref<number | null>(null)
const highlightedBlockId = ref<number[] | null>(null)
const inputPanelRef = ref<InstanceType<typeof InputPanel> | null>(null)
const isAnalyzing = ref(false)
const currentCfgMode = ref<CfgMode>('semantic')

// Sequence diagram state
const sequenceStepRange = ref<{ entryStep: number; exitStep: number } | null>(null)
const edgeStepMap = ref<EdgeStepMap | null>(null)

// Load edge step map when txHash changes
watch([currentTxHash, currentCfgMode], async ([newHash, cfgMode]) => {
  edgeStepMap.value = null
  if (newHash) {
    try {
      edgeStepMap.value = await fetchEdgeStepMap(newHash, cfgMode)
    } catch (e) {
      console.warn('Failed to load edge step map:', e)
    }
  }
})

// Compute filtered edge IDs from sequence step range
const filteredEdgeIds = computed<string[] | null>(() => {
  if (!sequenceStepRange.value || !edgeStepMap.value) return null
  const { entryStep, exitStep } = sequenceStepRange.value
  const matched = Object.values(edgeStepMap.value)
    .filter(e => e.edge_step >= entryStep && e.edge_step <= exitStep)
    .map(e => e.edge_id)
  return matched.length > 0 ? matched : null
})

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
  // Clear sequence selection when AFG navigates
  sequenceStepRange.value = null
  highlightedBlockId.value = blockIds
  console.log('Navigate to CFG blocks:', blockIds)
}

function handleSequenceSelect(stepRange: { entryStep: number; exitStep: number } | null) {
  // Clear AFG highlight when sequence diagram selects
  highlightedBlockId.value = null
  sequenceStepRange.value = stepRange
  console.log('Sequence diagram selection:', stepRange)
}

function handleCfgModeChange(mode: CfgMode) {
  currentCfgMode.value = mode
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
      <div class="top-row">
        <AfgPanel
          class="afg-panel"
          :tx-hash="currentTxHash"
          :highlighted-block-id="highlightedBlockId"
          :is-analyzing="isAnalyzing"
          @cfg-navigate="handleCfgNavigate"
        />
        <SequencePanel
          class="sequence-panel"
          :tx-hash="currentTxHash"
          :is-analyzing="isAnalyzing"
          :selected-step-range="sequenceStepRange"
          @sequence-select="handleSequenceSelect"
        />
      </div>
      <CfgPanel
        class="cfg-panel"
        :tx-hash="currentTxHash"
        :highlighted-block-id="highlightedBlockId"
        :filtered-edge-ids="filteredEdgeIds"
        :is-analyzing="isAnalyzing"
        :edge-step-map="edgeStepMap"
        :preferred-mode="currentCfgMode"
        @cfg-navigate="handleCfgNavigate"
        @mode-change="handleCfgModeChange"
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

.top-row {
  display: flex;
  flex-direction: row;
  flex: 1;
  min-height: 0;
  gap: 1px;
}

.afg-panel {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.sequence-panel {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.cfg-panel {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
