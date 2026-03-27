<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import SequencePanel from './components/SequencePanel.vue'
import BlockPanel from './components/BlockPanel.vue'
import LegendPanel from './components/LegendPanel.vue'
import {
  analyzeTransaction,
  fetchEdgeStepMap,
  fetchArbitrageHashes,
  normalizeAnalyzeError,
  triggerArbitrageRefresh,
  type BlockId,
  type CfgMode,
  type EdgeStepMap
} from './api/analyze'

const currentTxHash = ref<string | null>(null)
const currentBlockNumber = ref<number | null>(null)
const highlightedBlockId = ref<BlockId[] | null>(null)
const inputPanelRef = ref<InstanceType<typeof InputPanel> | null>(null)
const isAnalyzing = ref(false)
const currentCfgMode = ref<CfgMode>('folded')

// Arbitrage hashes from Dune — stored as Sets for O(1) lookup in BlockPanel
const arbitrageTxHashes = ref<Set<string>>(new Set())
const arbitrageBlockNumbers = ref<Set<number>>(new Set())

function applyArbitrageData(data: Awaited<ReturnType<typeof fetchArbitrageHashes>>) {
  arbitrageTxHashes.value = new Set(data.transactions.map(t => t.tx_hash))
  arbitrageBlockNumbers.value = new Set(
    data.transactions.map(t => t.block_number).filter((n): n is number => n !== null)
  )
}

onMounted(async () => {
  try {
    applyArbitrageData(await fetchArbitrageHashes())
  } catch (e) {
    console.warn('Failed to load arbitrage hashes:', e)
  }
})

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

function handleLatestBlocksRefreshed() {
  triggerArbitrageRefresh().then(() => {
    setTimeout(async () => {
      try {
        applyArbitrageData(await fetchArbitrageHashes())
      } catch (e) {
        console.warn('Failed to refresh arbitrage hashes:', e)
      }
    }, 5_000)
  }).catch(() => {})
}

async function handleTransactionSelected(txHash: string) {
  console.log('Transaction selected from heatmap:', txHash)

  // Sync input and immediately switch status to processing.
  inputPanelRef.value?.setAnalyzing(txHash)

  // Show loading immediately
  isAnalyzing.value = true

  // First complete the analysis, THEN update the hash to trigger panel loading
  try {
    const res = await analyzeTransaction(txHash)
    if (res.status === 'success') {
      console.log('Analysis complete for selected transaction')
      // Now set the hash to trigger CFG/AFG loading
      await new Promise(resolve => setTimeout(resolve, 100))
      currentTxHash.value = txHash
      inputPanelRef.value?.setAnalyzeSuccess()
    } else {
      inputPanelRef.value?.setAnalyzeError(normalizeAnalyzeError(res.error))
    }
  } catch (e) {
    console.error('Failed to analyze selected transaction:', e)
    const message = e instanceof Error ? e.message : 'Network error'
    inputPanelRef.value?.setAnalyzeError(normalizeAnalyzeError(message))
  } finally {
    isAnalyzing.value = false
  }
}

function handleCfgNavigate(blockIds: BlockId[] | null) {
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
  highlightedBlockId.value = null
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
        :selected-tx-hash="currentTxHash"
        :arbitrage-tx-hashes="arbitrageTxHashes"
        :arbitrage-block-numbers="arbitrageBlockNumbers"
        @transaction-selected="handleTransactionSelected"
        @block-selected="handleBlockSelected"
        @latest-block="handleLatestBlock"
        @latest-blocks-refreshed="handleLatestBlocksRefreshed"
      />
    </div>
    <div class="right-col">
      <div class="top-row">
        <AfgPanel
          class="afg-panel"
          :tx-hash="currentTxHash"
          :cfg-mode="currentCfgMode"
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
        <LegendPanel
          class="legend-panel"
          :tx-hash="currentTxHash"
          :is-analyzing="isAnalyzing"
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
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(0, 4fr);
  grid-template-rows: 36px 1fr;
  gap: 6px;
  background: var(--bg);
  overflow: hidden;
  padding: 6px;
  box-sizing: border-box;
}

.title-bar {
  grid-column: 1 / -1;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.10), 0 0 0 1px rgba(0,0,0,0.06);
}

.left-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg);
  overflow: visible;
  min-height: 0;
  min-width: 180px;
}

.input-panel {
  flex-shrink: 0;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
  overflow: hidden;
}

.block-panel {
  flex: 1;
  min-height: 0;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.right-col {
  display: grid;
  grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
  gap: 6px;
  background: var(--bg);
  min-height: 0;
  overflow: visible;
}

.top-row {
  display: grid;
  grid-template-columns: minmax(0, 1.04fr) minmax(0, 1.22fr) minmax(160px, 196px);
  min-height: 0;
  gap: 6px;
}

.afg-panel {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.sequence-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.legend-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.cfg-panel {
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

@media (max-width: 1180px) {
  .app-grid {
    grid-template-columns: minmax(190px, 1fr) minmax(0, 3.8fr);
  }

  .top-row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(140px, 170px);
  }
}
</style>
