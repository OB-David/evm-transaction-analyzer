<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { fetchBlockGasData, type BlockGasData } from '../api/analyze'

const props = defineProps<{
  blockNumber: number | null
}>()

const emit = defineEmits<{
  'transaction-selected': [txHash: string]
}>()

const plotContainer = ref<HTMLDivElement | null>(null)
const blockData = ref<BlockGasData | null>(null)
const loading = ref(false)
const error = ref('')
const plotlyReady = ref(false)

// Load Plotly from CDN
onMounted(() => {
  if (!(window as any).Plotly) {
    const script = document.createElement('script')
    script.src = 'https://cdn.plot.ly/plotly-2.24.1.min.js'
    script.onload = () => {
      plotlyReady.value = true
      if (props.blockNumber) {
        fetchAndRenderBlock(props.blockNumber)
      }
    }
    document.head.appendChild(script)
  } else {
    plotlyReady.value = true
  }
})

watch(() => props.blockNumber, async (newBlock) => {
  if (newBlock && plotlyReady.value) {
    await fetchAndRenderBlock(newBlock)
  }
})

async function fetchAndRenderBlock(blockNum: number) {
  loading.value = true
  error.value = ''
  blockData.value = null

  try {
    const data = await fetchBlockGasData(blockNum)

    if (data.status === 'error') {
      error.value = data.error || 'Failed to fetch block data'
      loading.value = false
      return
    }

    blockData.value = data
    loading.value = false

    // Wait for next tick to ensure DOM is updated
    await new Promise(resolve => setTimeout(resolve, 0))

    if (plotContainer.value && data.transactions.length > 0) {
      renderPlotly(data)
    }
  } catch (e: any) {
    error.value = e.message || 'Network error'
    loading.value = false
  }
}

function renderPlotly(data: BlockGasData) {
  if (!plotContainer.value) return

  const Plotly = (window as any).Plotly
  if (!Plotly) return

  const txs = data.transactions

  // Calculate min/max for color normalization
  const logGasValues = txs.map(tx => tx.log_gas)
  let vMin = Math.min(...logGasValues)
  let vMax = Math.max(...logGasValues)
  if (vMin === vMax) {
    vMin -= 0.1
    vMax += 0.1
  }

  // Generate hover text without emojis
  const hoverTexts = txs.map((tx, i) => {
    const toDisplay = tx.to_addr
      ? `${tx.to_addr.substring(0, 10)}...`
      : 'Contract Creation'

    return `Gas: ${tx.gas}<br>` +
           `Price: ${tx.gas_price_gwei.toFixed(2)} Gwei<br>` +
           `From: ${tx.from_addr.substring(0, 10)}...<br>` +
           `To: ${toDisplay}`
  })

  // Calculate height based on number of rows - give proper spacing
  const rows = Math.ceil(txs.length / 10)
  const plotHeight = rows * 36 + 30 // Proper spacing to prevent overlap

  const trace = {
    x: txs.map(tx => tx.x),
    y: txs.map(tx => tx.y),
    mode: 'markers+text',
    marker: {
      symbol: 'square',
      size: 36,
      color: logGasValues,
      colorscale: [
        [0, '#D5D9E8'],
        [0.5, '#7B88B8'],
        [1, '#3D4A6E']
      ],
      showscale: false,
      line: { width: 1, color: 'white' }
    },
    text: txs.map((_, i) => `${i + 1}`),
    textposition: 'middle',
    textfont: { size: 9, color: 'white', family: 'Arial, sans-serif' },
    hovertext: hoverTexts,
    hoverinfo: 'text',
    hoverlabel: {
      bgcolor: 'rgba(0, 0, 0, 0)',
      bordercolor: 'rgba(0, 0, 0, 0)',
      font: { size: 1, color: 'rgba(0, 0, 0, 0)' }
    }
  }

  const layout = {
    width: null,
    height: plotHeight,
    xaxis: {
      visible: false,
      fixedrange: true,
      range: [-0.5, 9.5] // Centered range
    },
    yaxis: {
      visible: false,
      fixedrange: true,
      range: [rows - 0.5, -0.5]
    },
    margin: { l: 5, r: 5, t: 0, b: 0 },
    plot_bgcolor: 'rgba(0,0,0,0)',
    paper_bgcolor: 'rgba(0,0,0,0)'
  }

  const config = {
    displayModeBar: false,
    responsive: true
  }

  Plotly.newPlot(plotContainer.value, [trace], layout, config)

  // Add click handler for transaction selection
  plotContainer.value.on('plotly_click', (eventData: any) => {
    const idx = eventData.points[0].pointIndex
    const txHash = txs[idx].hash
    emit('transaction-selected', txHash)
  })
}
</script>

<template>
  <div class="block-panel">
    <label class="panel-label">(d) Block Exploration</label>

    <div v-if="loading" class="status-loading">Loading block data...</div>
    <div v-else-if="error" class="status-error">{{ error }}</div>

    <div v-if="blockData" class="heatmap-container">
      <div ref="plotContainer" class="plot-area"></div>
    </div>
  </div>
</template>

<style scoped>
.block-panel {
  position: relative;
  background: var(--panel-bg);
  padding: 6px 12px 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  overflow-x: hidden;
  height: 100%;
}

.block-panel::-webkit-scrollbar {
  width: 6px;
}

.block-panel::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.block-panel::-webkit-scrollbar-track {
  background: transparent;
}

.panel-label {
  position: absolute;
  top: 8px;
  left: 12px;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.5px;
  z-index: 10;
}

.status-loading {
  color: var(--accent);
  font-size: 11px;
  padding: 10px;
  margin-top: 12px;
}

.status-error {
  color: var(--error);
  font-size: 11px;
  padding: 10px;
  margin-top: 12px;
}

.placeholder {
  color: var(--muted);
  font-size: 12px;
  padding: 20px;
  text-align: center;
}

.heatmap-container {
  flex: 1;
  overflow: visible;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-top: 18px;
  padding: 8px 0;
}

.plot-area {
  padding: 8px 12px 8px 2px;
  flex-shrink: 0;
}
</style>
