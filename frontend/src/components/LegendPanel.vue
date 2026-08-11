<script setup lang="ts">
import { ref, watch } from 'vue'
import { fetchLegendData, type LegendData } from '../api/analyze'
import { getDarkAccentForColor, getFillColorForColor } from '../visualTheme'
import CopyButton from './CopyButton.vue'

const props = defineProps<{
  txHash: string | null
  isAnalyzing?: boolean
}>()

const legendData = ref<LegendData | null>(null)
let loadRequestId = 0

watch(() => props.txHash, async (newHash) => {
  const requestId = ++loadRequestId
  if (!newHash) {
    legendData.value = null
    return
  }
  try {
    const nextLegend = await fetchLegendData(newHash)
    if (requestId === loadRequestId && props.txHash === newHash) {
      legendData.value = nextLegend
    }
  } catch (e) {
    if (requestId !== loadRequestId) return
    console.warn('Failed to load legend data:', e)
    legendData.value = null
  }
}, { immediate: true })

function truncateAddr(addr: string): string {
  if (addr.length <= 14) return addr
  return addr.substring(0, 8) + '...' + addr.substring(addr.length - 4)
}

function fillColor(color?: string) {
  return getFillColorForColor(color)
}

function strokeColor(color?: string) {
  return getDarkAccentForColor(color, '#2C2C2C')
}
</script>

<template>
  <div class="legend-panel">
    <div class="legend-header">
      <span class="panel-label">(F) Legend</span>
    </div>

    <div v-if="props.isAnalyzing" class="status-overlay">
      Loading legend...
    </div>

    <div v-else-if="legendData" class="legend-scroll">
      <!-- User Addresses -->
      <template v-if="legendData.user_addresses.length > 0">
        <div class="legend-title">User Addresses</div>
        <div
          v-for="item in legendData.user_addresses"
          :key="'user-' + item.address"
          class="legend-item"
        >
          <svg width="28" height="20" viewBox="0 0 28 20">
            <polygon
              points="14,3 23,10 14,17 5,10"
              fill="#E2E2E2"
              stroke="#B6B6B6"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <div class="legend-address-row">
              <span class="legend-addr" :title="item.address">{{ truncateAddr(item.address) }}</span>
              <CopyButton :value="item.address" :label="`${item.name} address`" />
            </div>
          </div>
        </div>
      </template>

      <!-- ERC20 Token Contracts -->
      <template v-if="legendData.erc20_tokens.length > 0">
        <div class="legend-title">ERC20 Tokens</div>
        <div
          v-for="item in legendData.erc20_tokens"
          :key="'erc20-' + item.address"
          class="legend-item"
        >
          <svg width="28" height="20" viewBox="0 0 28 20">
            <ellipse
              cx="14"
              cy="10"
              rx="10"
              ry="6"
              :fill="fillColor(item.color)"
              :stroke="strokeColor(item.color)"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <div class="legend-address-row">
              <span class="legend-addr" :title="item.address">{{ truncateAddr(item.address) }}</span>
              <CopyButton :value="item.address" :label="`${item.name} address`" />
            </div>
          </div>
        </div>
      </template>

      <!-- Normal Contracts -->
      <template v-if="legendData.normal_contracts.length > 0">
        <div class="legend-title">Other Contracts</div>
        <div
          v-for="item in legendData.normal_contracts"
          :key="'contract-' + item.address"
          class="legend-item"
        >
          <svg width="28" height="20" viewBox="0 0 28 20">
            <rect
              x="5"
              y="5"
              width="18"
              height="10"
              rx="1"
              :fill="fillColor(item.color)"
              :stroke="strokeColor(item.color)"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <div class="legend-address-row">
              <span class="legend-addr" :title="item.address">{{ truncateAddr(item.address) }}</span>
              <CopyButton :value="item.address" :label="`${item.name} address`" />
            </div>
          </div>
        </div>
      </template>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Run an analysis to view the legend</span>
    </div>
  </div>
</template>

<style scoped>
.legend-panel {
  position: relative;
  background: var(--panel-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.legend-header {
  flex: 0 0 30px;
  min-height: 30px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  padding: 0 12px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
  z-index: 2;
}

.legend-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 10px 10px;
}

.legend-scroll::-webkit-scrollbar {
  width: 5px;
}

.legend-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.legend-scroll::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.legend-scroll::-webkit-scrollbar-thumb:hover {
  background: var(--muted);
}

.legend-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
  margin-top: 8px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

.legend-title:first-child {
  margin-top: 0;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 3px;
}

.legend-item:last-child {
  margin-bottom: 0;
}

.legend-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.legend-name {
  font-size: 12px;
  color: var(--text);
  line-height: 1.35;
}

.legend-address-row {
  display: flex;
  align-items: center;
  gap: 3px;
  min-width: 0;
}

.legend-addr {
  font-size: 11px;
  color: var(--muted);
  font-family: 'Consolas', 'Monaco', monospace;
  line-height: 1.3;
}

.panel-label {
  font-size: 11px;
  color: #000000;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.status-overlay,
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  min-height: 0;
  padding: 16px;
}

.status-overlay {
  color: var(--accent);
  font-size: 12px;
}

.placeholder-text {
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}

svg {
  flex-shrink: 0;
}
</style>
