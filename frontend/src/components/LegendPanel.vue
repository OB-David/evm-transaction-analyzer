<script setup lang="ts">
import { ref, watch } from 'vue'
import { fetchLegendData, type LegendData } from '../api/analyze'

const props = defineProps<{
  txHash: string | null
}>()

const legendData = ref<LegendData | null>(null)

watch(() => props.txHash, async (newHash) => {
  if (!newHash) {
    legendData.value = null
    return
  }
  try {
    legendData.value = await fetchLegendData(newHash)
  } catch (e) {
    console.warn('Failed to load legend data:', e)
    legendData.value = null
  }
}, { immediate: true })

function truncateAddr(addr: string): string {
  if (addr.length <= 14) return addr
  return addr.substring(0, 8) + '...' + addr.substring(addr.length - 4)
}
</script>

<template>
  <div v-if="legendData" class="legend-panel">
    <div class="legend-scroll">
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
              fill="#FFFFFF"
              stroke="#2C2C2C"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <span class="legend-addr">{{ truncateAddr(item.address) }}</span>
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
              :fill="item.color || '#4DD0E1'"
              stroke="#2C2C2C"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <span class="legend-addr">{{ truncateAddr(item.address) }}</span>
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
              :fill="item.color || '#FF9E9E'"
              stroke="#2C2C2C"
              stroke-width="1.2"
            />
          </svg>
          <div class="legend-text">
            <span class="legend-name">{{ item.name }}</span>
            <span class="legend-addr">{{ truncateAddr(item.address) }}</span>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.legend-panel {
  width: 133px;
  flex-shrink: 0;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid var(--border);
  border-radius: 3px;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin: 8px 10px 10px 0;
}

.legend-scroll {
  overflow-y: auto;
  padding: 8px 8px;
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
  font-size: 10px;
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
  font-size: 10px;
  color: var(--text);
  line-height: 1.3;
}

.legend-addr {
  font-size: 9px;
  color: var(--muted);
  font-family: 'Consolas', 'Monaco', monospace;
  line-height: 1.2;
}

svg {
  flex-shrink: 0;
}
</style>
