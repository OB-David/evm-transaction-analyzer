<script setup lang="ts">
import { ref } from 'vue'
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import ReservedPanel from './components/ReservedPanel.vue'

const currentTxHash = ref<string | null>(null)
const highlightedBlockId = ref<number[] | null>(null)

function handleAnalysisComplete(txHash: string) {
  currentTxHash.value = txHash
  console.log('Analysis complete for:', txHash)
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
        class="input-panel"
        @analysis-complete="handleAnalysisComplete"
      />
      <ReservedPanel class="reserved-panel" />
    </div>
    <div class="right-col">
      <CfgPanel
        class="cfg-panel"
        :tx-hash="currentTxHash"
        :highlighted-block-id="highlightedBlockId"
        @cfg-navigate="handleCfgNavigate"
      />
      <AfgPanel
        class="afg-panel"
        :tx-hash="currentTxHash"
        :highlighted-block-id="highlightedBlockId"
        @cfg-navigate="handleCfgNavigate"
      />
    </div>
  </div>
</template>

<style scoped>
.app-grid {
  height: 100%;
  display: grid;
  grid-template-columns: 20% 80%;
  grid-template-rows: 36px 1fr;
  gap: 1px;
  background: var(--border);
}

.title-bar {
  grid-column: 1 / -1;
}

.left-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
}

.input-panel {
  height: 10vh;
  min-height: 80px;
}

.reserved-panel {
  flex: 1;
}

.right-col {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
}

.cfg-panel {
  flex: 1;
}

.afg-panel {
  flex: 1;
}
</style>
