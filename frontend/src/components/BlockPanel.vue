<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import {
  fetchBlockGasData,
  fetchBlocksHeatmap,
  fetchArbitrageTransactions,
  type BlockGasData,
  type BlocksHeatmapData,
  type TransactionGasInfo,
} from '../api/analyze'

type ViewMode = 'blocks' | 'transactions'

const props = defineProps<{
  blockNumber: number | null
  selectedTxHash?: string | null
}>()

const emit = defineEmits<{
  'transaction-selected': [txHash: string]
  'block-selected': [blockNumber: number]
  'latest-block': [blockNumber: number]
}>()

// Shared state
const plotlyReady = ref(false)
const viewMode = ref<ViewMode>('blocks')
const BLOCKS_HEATMAP_COUNT = 180
const BLOCKS_COLOR_P_LOW = 0.05
const BLOCKS_COLOR_P_HIGH = 0.95
const BLOCKS_COLOR_TAIL_SPAN = 0.12
const TX_COLOR_RANGE_MIN = 0.08
const TX_COLOR_RANGE_MAX = 0.86

// Auto-refresh
let refreshTimer: ReturnType<typeof setInterval> | null = null
const REFRESH_INTERVAL_MS = 12000  // ~1 Ethereum block time
const REQUEST_TIMEOUT_MS = 30000

let blocksAbortController: AbortController | null = null
let blockAbortController: AbortController | null = null
let pageMarkerAbortController: AbortController | null = null
let transactionMarkerAbortController: AbortController | null = null
let plotResizeObserver: ResizeObserver | null = null
let resizeFrame: number | null = null
let reloadBlocksWhenVisible = false
let reloadBlockWhenVisible = false
let visibilityRestoreId = 0

function startAutoRefresh() {
  stopAutoRefresh()
  if (document.hidden) return
  refreshTimer = setInterval(() => {
    if (viewMode.value === 'blocks' && blocksOffset.value === 0 && !blocksLoading.value) {
      loadBlocksHeatmap()
    }
  }, REFRESH_INTERVAL_MS)
}

function stopAutoRefresh() {
  if (refreshTimer !== null) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

// Per-second clock for "X seconds ago" display
const nowTs = ref(Math.floor(Date.now() / 1000))
let clockTimer: ReturnType<typeof setInterval> | null = null

function startClock() {
  clockTimer = setInterval(() => {
    nowTs.value = Math.floor(Date.now() / 1000)
  }, 1000)
}

function formatAge(blockTs: number): string {
  if (!blockTs) return ''
  const age = nowTs.value - blockTs
  if (age < 0) return 'just now'
  if (age < 60) return `${age}s ago`
  const m = Math.floor(age / 60)
  const s = age % 60
  if (age < 3600) return `${m}m ${s}s ago`
  return `${Math.floor(age / 3600)}h ago`
}

onUnmounted(() => {
  stopAutoRefresh()
  if (clockTimer !== null) clearInterval(clockTimer)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  plotResizeObserver?.disconnect()
  if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame)
  blocksAbortController?.abort()
  blockAbortController?.abort()
  pageMarkerAbortController?.abort()
  transactionMarkerAbortController?.abort()
})

// Blocks view state
const blockPlotContainer = ref<HTMLDivElement | null>(null)
const heatmapContainer = ref<HTMLDivElement | null>(null)
const blocksData = ref<BlocksHeatmapData | null>(null)
const blocksOffset = ref(0)
const blocksLoading = ref(false)
const blocksError = ref('')
const transactionArbitrageTxHashes = ref<Set<string>>(new Set())
const pageArbitrageBlockNumbers = ref<Set<number>>(new Set())
const latestArbitrageBlock = ref<number | null>(null)
const arbitrageCoverageMessage = ref('')
let pageArbitrageRequestId = 0
let transactionArbitrageRequestId = 0
let blocksRequestId = 0
let blockRequestId = 0

// Transactions view state
const txPlotContainer = ref<HTMLDivElement | null>(null)
const blockData = ref<BlockGasData | null>(null)
const loading = ref(false)
const error = ref('')
const selectedTxInfo = ref<TransactionGasInfo | null>(null)
const selectedTxIndex = ref<number | null>(null)

// Gas range for legend
const gasMin = ref<string>('')
const gasMax = ref<string>('')

// Load Plotly from CDN
onMounted(() => {
  startClock()
  document.addEventListener('visibilitychange', handleVisibilityChange)
  setupPlotResizeObserver()
  if (!(window as any).Plotly) {
    const script = document.createElement('script')
    script.src = 'https://cdn.plot.ly/plotly-2.24.1.min.js'
    script.onload = () => {
      plotlyReady.value = true
      loadBlocksHeatmap().then(startAutoRefresh)
    }
    script.onerror = () => {
      blocksError.value = 'Failed to load the chart renderer'
      blocksLoading.value = false
    }
    document.head.appendChild(script)
  } else {
    plotlyReady.value = true
    loadBlocksHeatmap().then(startAutoRefresh)
  }
})

function setupPlotResizeObserver() {
  if (typeof ResizeObserver === 'undefined') return
  plotResizeObserver = new ResizeObserver(() => scheduleVisiblePlotResize())
  if (heatmapContainer.value) plotResizeObserver.observe(heatmapContainer.value)
}

function scheduleVisiblePlotResize() {
  if (document.hidden || resizeFrame !== null) return
  resizeFrame = window.requestAnimationFrame(() => {
    resizeFrame = null
    const Plotly = (window as any).Plotly
    const container = viewMode.value === 'blocks' ? blockPlotContainer.value : txPlotContainer.value
    if (!Plotly || !container || container.clientWidth <= 0 || container.clientHeight <= 0) return
    try {
      Plotly.Plots.resize(container)
    } catch (e) {
      console.warn('Failed to resize block explorer chart:', e)
    }
  })
}

function handleVisibilityChange() {
  if (document.hidden) {
    visibilityRestoreId += 1
    stopAutoRefresh()
    reloadBlocksWhenVisible = blocksLoading.value
    reloadBlockWhenVisible = loading.value
    blocksRequestId += 1
    blockRequestId += 1
    pageArbitrageRequestId += 1
    transactionArbitrageRequestId += 1
    blocksAbortController?.abort()
    blockAbortController?.abort()
    pageMarkerAbortController?.abort()
    transactionMarkerAbortController?.abort()
    blocksLoading.value = false
    loading.value = false
    return
  }

  const restoreId = ++visibilityRestoreId
  void restoreVisiblePlot(restoreId)
}

async function restoreVisiblePlot(restoreId: number) {
  await nextTick()

  let previousWidth = -1
  let stableFrames = 0
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve()))
    if (document.hidden || restoreId !== visibilityRestoreId) return
    const container = viewMode.value === 'blocks' ? blockPlotContainer.value : txPlotContainer.value
    const width = container?.getBoundingClientRect().width ?? 0
    if (width > 0 && Math.abs(width - previousWidth) < 0.5) stableFrames += 1
    else stableFrames = 0
    previousWidth = width
    if (stableFrames >= 2) break
  }

  if (document.hidden || restoreId !== visibilityRestoreId) return
  if (viewMode.value === 'blocks') {
    if (blocksData.value) renderBlocksPlotly(blocksData.value)
    if (reloadBlocksWhenVisible || !blocksData.value) void loadBlocksHeatmap()
    if (blocksOffset.value === 0) startAutoRefresh()
  } else {
    if (blockData.value) renderPlotly(blockData.value)
    if (reloadBlockWhenVisible && props.blockNumber !== null) {
      void fetchAndRenderBlock(props.blockNumber)
    }
  }
  reloadBlocksWhenVisible = false
  reloadBlockWhenVisible = false
}

function startRequestTimeout(controller: AbortController): number {
  return window.setTimeout(() => {
    controller.abort(new DOMException('Request timed out', 'TimeoutError'))
  }, REQUEST_TIMEOUT_MS)
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

watch(() => props.blockNumber, async (newBlock) => {
  if (newBlock && plotlyReady.value) {
    viewMode.value = 'transactions'
    selectedTxInfo.value = null
    selectedTxIndex.value = null
    await fetchAndRenderBlock(newBlock)
  }
})

watch(viewMode, (_newMode, oldMode) => {
  const Plotly = (window as any).Plotly
  const oldContainer = oldMode === 'blocks' ? blockPlotContainer.value : txPlotContainer.value
  if (Plotly && oldContainer) Plotly.purge(oldContainer)
}, { flush: 'pre' })

watch(() => props.selectedTxHash, (newTxHash) => {
  if (viewMode.value !== 'transactions' || !blockData.value) return
  applySelectedTransaction(blockData.value, newTxHash)
  renderPlotly(blockData.value)
})

// ─── Blocks View ───

async function loadArbitrageMarkers(
  fromBlock: number,
  toBlock: number,
  scope: 'page' | 'transactions',
): Promise<void> {
  const requestId = scope === 'page' ? ++pageArbitrageRequestId : ++transactionArbitrageRequestId
  const previousController = scope === 'page' ? pageMarkerAbortController : transactionMarkerAbortController
  previousController?.abort()
  const controller = new AbortController()
  if (scope === 'page') pageMarkerAbortController = controller
  else transactionMarkerAbortController = controller
  const timeout = startRequestTimeout(controller)
  try {
    const data = await fetchArbitrageTransactions(fromBlock, toBlock, controller.signal)
    const isCurrent = scope === 'page'
      ? requestId === pageArbitrageRequestId
      : requestId === transactionArbitrageRequestId
    if (!isCurrent) return
    if (scope === 'page') {
      pageArbitrageBlockNumbers.value = new Set(data.transactions.map(tx => tx.block_number))
    } else {
      transactionArbitrageTxHashes.value = new Set(data.transactions.map(tx => tx.tx_hash.toLowerCase()))
    }
    if (scope === 'page') {
      latestArbitrageBlock.value = data.max_arbitrage_block
      if (toBlock < data.history_start_block || fromBlock < data.history_start_block) {
        arbitrageCoverageMessage.value = `Dune arbitrage coverage starts at #${data.history_start_block}`
      } else if (!data.coverage_complete) {
        arbitrageCoverageMessage.value = data.initial_sync_complete
          ? 'Dune arbitrage index is updating'
          : 'Dune arbitrage history is still syncing'
      } else {
        arbitrageCoverageMessage.value = ''
      }
    }
  } catch (e) {
    const isCurrent = scope === 'page'
      ? requestId === pageArbitrageRequestId
      : requestId === transactionArbitrageRequestId
    if (!isCurrent) return
    if (isAbortError(e)) return
    if (scope === 'page') pageArbitrageBlockNumbers.value = new Set()
    else transactionArbitrageTxHashes.value = new Set()
    if (scope === 'page') {
      latestArbitrageBlock.value = null
      arbitrageCoverageMessage.value = 'Dune arbitrage markers unavailable'
    }
    console.warn('Failed to load local arbitrage markers:', e)
  } finally {
    window.clearTimeout(timeout)
    if (scope === 'page' && pageMarkerAbortController === controller) pageMarkerAbortController = null
    if (scope === 'transactions' && transactionMarkerAbortController === controller) {
      transactionMarkerAbortController = null
    }
  }
}

async function loadBlocksHeatmap(): Promise<void> {
  const requestId = ++blocksRequestId
  const requestedOffset = blocksOffset.value
  blocksAbortController?.abort()
  const controller = new AbortController()
  blocksAbortController = controller
  const timeout = startRequestTimeout(controller)
  blocksLoading.value = true
  blocksError.value = ''

  try {
    const data = await fetchBlocksHeatmap(requestedOffset, BLOCKS_HEATMAP_COUNT, controller.signal)
    if (requestId !== blocksRequestId || requestedOffset !== blocksOffset.value) return
    if (data.status === 'error') {
      blocksError.value = data.error || 'Failed to fetch blocks'
      blocksLoading.value = false
      return
    }
    blocksData.value = data
    if (data.blocks.length > 0) {
      const blockNumbers = data.blocks.map(block => block.block_number)
      await loadArbitrageMarkers(Math.min(...blockNumbers), Math.max(...blockNumbers), 'page')
      if (requestId !== blocksRequestId || requestedOffset !== blocksOffset.value) return
    }
    blocksLoading.value = false

    emit('latest-block', data.latest_block)

    await nextTick()
    if (blockPlotContainer.value && data.blocks.length > 0) {
      renderBlocksPlotly(data)
    }
  } catch (e: any) {
    if (requestId !== blocksRequestId) return
    if (!isAbortError(e)) blocksError.value = e.message || 'Network error'
  } finally {
    window.clearTimeout(timeout)
    if (requestId === blocksRequestId) blocksLoading.value = false
    if (blocksAbortController === controller) blocksAbortController = null
  }
}

function renderBlocksPlotly(data: BlocksHeatmapData) {
  if (!blockPlotContainer.value) return
  const Plotly = (window as any).Plotly
  if (!Plotly) return

  const blocks = data.blocks.slice(0, BLOCKS_HEATMAP_COUNT)
  if (blocks.length === 0) return
  const avgGasValues = blocks.map(b => b.avg_gas)
  const colorMapping = mapGasValuesForColor(avgGasValues)
  gasMin.value = Math.round(colorMapping.actualMin).toLocaleString()
  gasMax.value = Math.round(colorMapping.actualMax).toLocaleString()

  const hoverTexts = blocks.map(b => {
    const arbitrageLabel = pageArbitrageBlockNumbers.value.has(b.block_number)
      ? '<br>Dune atomic arbitrage: Yes'
      : ''
    return `Block: ${b.block_number}<br>` +
      `Avg Gas: ${b.avg_gas.toFixed(0)}<br>` +
      `Tx Count: ${b.tx_count}${arbitrageLabel}`
  })

  const rows = Math.ceil(blocks.length / 10)
  const plotHeight = rows * 30 + 22

  const trace = {
    x: blocks.map(b => b.x),
    y: blocks.map(b => b.y),
    mode: 'markers',
    marker: {
      symbol: 'square',
      size: 30,
      color: colorMapping.scaledValues,
      cmin: 0,
      cmax: 1,
      colorscale: [
        [0, '#F2F4F8'],
        [0.25, '#E3E9F0'],
        [0.5, '#CAD5E4'],
        [0.75, '#A8B9D0'],
        [1, '#7F97B8'],
      ],
      showscale: false,
      line: { width: 1, color: 'white' },
    },
    hovertext: hoverTexts,
    hoverinfo: 'text',
    hoverlabel: {
      bgcolor: '#FFFFFF',
      bordercolor: '#D0D0D0',
      font: { size: 11, color: '#2C2C2C', family: 'Inter, sans-serif' },
    },
  }

  const layout = {
    autosize: true,
    height: plotHeight,
    showlegend: false,
    xaxis: { visible: false, fixedrange: true, range: [-0.5, 9.5] },
    yaxis: { visible: false, fixedrange: true, range: [rows - 0.5, -0.5] },
    margin: { l: 5, r: 5, t: 0, b: 0 },
    plot_bgcolor: 'rgba(0,0,0,0)',
    paper_bgcolor: 'rgba(0,0,0,0)',
  }

  const blockTraces: any[] = [trace]

  const arbBlocks = pageArbitrageBlockNumbers.value
  if (arbBlocks && arbBlocks.size > 0) {
    const flagged = blocks.filter(b => arbBlocks.has(b.block_number))
    if (flagged.length > 0) {
      blockTraces.push({
        x: flagged.map(b => b.x),
        y: flagged.map(b => b.y),
        mode: 'markers',
        marker: {
          symbol: 'square',
          size: 32,
          color: 'rgba(0,0,0,0)',
          line: { width: 2.5, color: '#e53935' },
        },
        hoverinfo: 'skip',
        showlegend: false,
      })
    }
  }

  Plotly.newPlot(blockPlotContainer.value, blockTraces, layout, {
    displayModeBar: false,
    responsive: false,
  })
  scheduleVisiblePlotResize()

  ;(blockPlotContainer.value as any).removeAllListeners('plotly_click')
  ;(blockPlotContainer.value as any).on('plotly_click', (eventData: any) => {
    const pt = eventData.points?.[0]
    if (!pt) return
    const block = blocks[pt.pointIndex as number]
    if (!block) return
    const blockNum = block.block_number
    emit('block-selected', blockNum)
  })
}

function navigateOlder() {
  stopAutoRefresh()  // 翻到历史页时停止自动刷新
  blocksOffset.value += 100
  loadBlocksHeatmap()
}

function navigateNewer() {
  blocksOffset.value = Math.max(0, blocksOffset.value - 100)
  loadBlocksHeatmap().then(() => {
    if (blocksOffset.value === 0) startAutoRefresh()
  })
}

function navigateLatest() {
  blocksOffset.value = 0
  loadBlocksHeatmap().then(startAutoRefresh)
}

function navigateLatestArbitrage() {
  const targetBlock = latestArbitrageBlock.value
  const latestBlock = blocksData.value?.latest_block
  if (targetBlock === null || latestBlock === undefined) return

  stopAutoRefresh()
  viewMode.value = 'blocks'

  // Keep the target near the center of the 180-block heatmap instead of
  // opening its transaction view directly.
  const blocksAfterTarget = Math.floor(BLOCKS_HEATMAP_COUNT / 2)
  const pageNewestBlock = Math.min(latestBlock, targetBlock + blocksAfterTarget)
  blocksOffset.value = Math.max(0, latestBlock - pageNewestBlock)
  loadBlocksHeatmap()
}

function backToBlocks() {
  viewMode.value = 'blocks'
  selectedTxInfo.value = null
  selectedTxIndex.value = null
  nextTick(() => {
    if (blocksData.value && blockPlotContainer.value) {
      renderBlocksPlotly(blocksData.value)
    }
  })
}

// ─── Transactions View ───

async function fetchAndRenderBlock(blockNum: number) {
  const requestId = ++blockRequestId
  blockAbortController?.abort()
  const controller = new AbortController()
  blockAbortController = controller
  const timeout = startRequestTimeout(controller)
  loading.value = true
  error.value = ''
  blockData.value = null

  try {
    const data = await fetchBlockGasData(blockNum, controller.signal)
    if (requestId !== blockRequestId || props.blockNumber !== blockNum) return
    if (data.status === 'error') {
      error.value = data.error || 'Failed to fetch block data'
      loading.value = false
      return
    }
    blockData.value = data
    await loadArbitrageMarkers(blockNum, blockNum, 'transactions')
    if (requestId !== blockRequestId || props.blockNumber !== blockNum) return
    loading.value = false

    await nextTick()
    if (txPlotContainer.value && data.transactions.length > 0) {
      applySelectedTransaction(data, props.selectedTxHash)
      renderPlotly(data)
    }
  } catch (e: any) {
    if (requestId !== blockRequestId) return
    if (!isAbortError(e)) error.value = e.message || 'Network error'
  } finally {
    window.clearTimeout(timeout)
    if (requestId === blockRequestId) loading.value = false
    if (blockAbortController === controller) blockAbortController = null
  }
}

function applySelectedTransaction(data: BlockGasData, txHash?: string | null) {
  if (!txHash) {
    selectedTxInfo.value = null
    selectedTxIndex.value = null
    return
  }
  const index = data.transactions.findIndex((tx) => tx.hash.toLowerCase() === txHash.toLowerCase())
  if (index === -1) {
    selectedTxInfo.value = null
    selectedTxIndex.value = null
    return
  }
  selectedTxInfo.value = data.transactions[index]!
  selectedTxIndex.value = index
}

function renderPlotly(data: BlockGasData) {
  if (!txPlotContainer.value) return
  const Plotly = (window as any).Plotly
  if (!Plotly) return

  const txs = data.transactions
  const logGasValues = txs.map(tx => tx.log_gas)
  const txColorMapping = mapGasValuesForColor(logGasValues)
  const txHeatColors = remapScaledRange(
    txColorMapping.scaledValues,
    TX_COLOR_RANGE_MIN,
    TX_COLOR_RANGE_MAX
  )

  const gasValues = txs.map(tx => tx.gas)
  gasMin.value = Math.min(...gasValues).toLocaleString()
  gasMax.value = Math.max(...gasValues).toLocaleString()

  const hoverTexts = txs.map((tx) => {
    const toDisplay = tx.to_addr
      ? `${tx.to_addr.substring(0, 10)}...`
      : 'Contract Creation'
    const arbitrageLabel = transactionArbitrageTxHashes.value.has(tx.hash.toLowerCase())
      ? '<br>Dune atomic arbitrage: Yes'
      : ''
    return `${tx.hash.substring(0, 10)}...${tx.hash.substring(tx.hash.length - 6)}<br>` +
           `Gas: ${tx.gas}<br>` +
           `Price: ${tx.gas_price_gwei.toFixed(2)} Gwei<br>` +
           `From: ${tx.from_addr.substring(0, 10)}...<br>` +
           `To: ${toDisplay}${arbitrageLabel}`
  })

  const rows = Math.ceil(txs.length / 10)
  const plotHeight = rows * 30 + 22

  const trace = {
    x: txs.map(tx => tx.x),
    y: txs.map(tx => tx.y),
    mode: 'markers' as const,
    customdata: txs.map(tx => tx.hash),
    marker: {
      symbol: 'square',
      size: 30,
      color: txHeatColors,
      cmin: 0,
      cmax: 1,
      colorscale: [
        [0, '#F2F6F6'],
        [0.25, '#E5EFEC'],
        [0.5, '#D2E3DE'],
        [0.75, '#B7CCC4'],
        [1, '#93A89F'],
      ],
      showscale: false,
      line: { width: 1, color: 'white' },
    },
    hovertext: hoverTexts,
    hoverinfo: 'text',
    hoverlabel: {
      bgcolor: '#FFFFFF',
      bordercolor: '#D0D0D0',
      font: { size: 11, color: '#2C2C2C', family: 'Inter, sans-serif' },
    },
  }

  // Highlight trace: draw selected tx as a separate layer on top
  const traces: any[] = [trace]

  // Arbitrage overlay: red border for transactions flagged by Dune
  const arbSet = transactionArbitrageTxHashes.value
  if (arbSet && arbSet.size > 0) {
    const arbTxs = txs.filter(tx => arbSet.has(tx.hash.toLowerCase()))
    if (arbTxs.length > 0) {
      traces.push({
        x: arbTxs.map(tx => tx.x),
        y: arbTxs.map(tx => tx.y),
        customdata: arbTxs.map(tx => tx.hash),
        mode: 'markers',
        marker: {
          symbol: 'square',
          size: 32,
          color: 'rgba(0,0,0,0)',
          line: { width: 2.5, color: '#e53935' },
        },
        hoverinfo: 'skip',
        showlegend: false,
      })
    }
  }

  if (selectedTxIndex.value !== null) {
    const sel = txs[selectedTxIndex.value]
    if (sel) {
      traces.push({
        x: [sel.x],
        y: [sel.y],
        customdata: [sel.hash],
        mode: 'markers',
        marker: {
          symbol: 'square',
          size: 32,
          color: 'rgba(0,0,0,0)',
          line: { width: 2.5, color: '#3B5998' },
        },
        hoverinfo: 'skip',
        showlegend: false,
      })
    }
  }

  const layout = {
    autosize: true,
    height: plotHeight,
    showlegend: false,
    xaxis: { visible: false, fixedrange: true, range: [-0.5, 9.5] },
    yaxis: { visible: false, fixedrange: true, range: [rows - 0.5, -0.5] },
    margin: { l: 5, r: 5, t: 0, b: 0 },
    plot_bgcolor: 'rgba(0,0,0,0)',
    paper_bgcolor: 'rgba(0,0,0,0)',
  }

  Plotly.newPlot(txPlotContainer.value, traces, layout, {
    displayModeBar: false,
    responsive: false,
  })
  scheduleVisiblePlotResize()

  ;(txPlotContainer.value as any).removeAllListeners('plotly_click')
  ;(txPlotContainer.value as any).on('plotly_click', (eventData: any) => {
    const pt = eventData.points?.[0]
    if (!pt) return
    const clickedHash = typeof pt.customdata === 'string' ? pt.customdata : null
    const idx = clickedHash
      ? txs.findIndex(tx => tx.hash.toLowerCase() === clickedHash.toLowerCase())
      : pt.pointIndex as number
    const tx = idx >= 0 ? txs[idx] : undefined
    if (!tx) return
    selectedTxInfo.value = tx
    selectedTxIndex.value = idx
    emit('transaction-selected', tx.hash)
    // Re-render to update highlight
    renderPlotly(data)
  })
}

// ─── Helpers ───

function truncateHash(hash: string): string {
  if (hash.length <= 20) return hash
  return hash.substring(0, 10) + '...' + hash.substring(hash.length - 10)
}

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}

function quantile(values: number[], p: number): number {
  if (values.length === 0) return NaN
  const sorted = [...values].sort((a, b) => a - b)
  const pos = (sorted.length - 1) * p
  const base = Math.floor(pos)
  const rest = pos - base
  const lower = sorted[base]!
  const upper = sorted[Math.min(base + 1, sorted.length - 1)]!
  return lower + (upper - lower) * rest
}

function mapGasValuesForColor(values: number[]): { scaledValues: number[], actualMin: number, actualMax: number } {
  const actualMin = Math.min(...values)
  const actualMax = Math.max(...values)
  if (!Number.isFinite(actualMin) || !Number.isFinite(actualMax)) {
    return { scaledValues: values.map(() => 0.5), actualMin: 0, actualMax: 0 }
  }
  if (actualMax <= actualMin) {
    return { scaledValues: values.map(() => 0.5), actualMin, actualMax }
  }

  const qLow = clamp(quantile(values, BLOCKS_COLOR_P_LOW), actualMin, actualMax)
  const qHigh = clamp(quantile(values, BLOCKS_COLOR_P_HIGH), qLow, actualMax)
  const midStart = BLOCKS_COLOR_TAIL_SPAN
  const midEnd = 1 - BLOCKS_COLOR_TAIL_SPAN
  const eps = 1e-9

  const scaledValues = values.map((v) => {
    if (v <= qLow) {
      if (qLow - actualMin < eps) return midStart * 0.5
      return ((v - actualMin) / (qLow - actualMin)) * midStart
    }
    if (v >= qHigh) {
      if (actualMax - qHigh < eps) return midEnd + (1 - midEnd) * 0.5
      return midEnd + ((v - qHigh) / (actualMax - qHigh)) * (1 - midEnd)
    }
    if (qHigh - qLow < eps) return 0.5
    return midStart + ((v - qLow) / (qHigh - qLow)) * (midEnd - midStart)
  })

  return { scaledValues, actualMin, actualMax }
}

function remapScaledRange(values: number[], minTarget: number, maxTarget: number): number[] {
  if (maxTarget <= minTarget) return values
  const span = maxTarget - minTarget
  return values.map(v => clamp(minTarget + v * span, 0, 1))
}

async function copyToClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // fallback
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}
</script>

<template>
  <div class="block-panel">
    <button v-if="viewMode === 'transactions'" class="nav-btn back-btn-top" @click="backToBlocks">← Back</button>

    <div class="top-row">
      <label class="panel-label">(B) Block Exploration</label>
      <span class="view-mode-pill">{{ viewMode === 'blocks' ? 'Block View' : 'Transaction View' }}</span>
    </div>

    <div v-if="gasMin && gasMax" class="gas-legend">
      <span class="gas-unit">{{ viewMode === 'blocks' ? 'Average Gas' : 'Gas' }}</span>
      <span class="gas-label">{{ gasMin }}</span>
      <div class="gas-bar" :class="viewMode"></div>
      <span class="gas-label">{{ gasMax }}</span>
    </div>

    <!-- Header area -->
    <div class="header-area">
      <!-- Blocks view: time + navigation -->
      <template v-if="viewMode === 'blocks'">
        <div class="time-banner" v-if="blocksData">
          <span class="latest-block-num">#{{ blocksData.latest_block }}</span>
          <span class="age-text">{{ formatAge(blocksData.latest_block_timestamp || blocksData.page_timestamp) }}</span>
          <span v-if="refreshTimer !== null" class="live-badge">LIVE</span>
        </div>
        <div v-if="arbitrageCoverageMessage" class="arbitrage-coverage">
          {{ arbitrageCoverageMessage }}
        </div>
        <div class="nav-buttons">
          <button class="nav-btn" @click="navigateOlder">Older 100</button>
          <button class="nav-btn nav-btn-home" @click="navigateLatest">Latest</button>
          <button
            class="nav-btn nav-btn-arbitrage"
            :disabled="latestArbitrageBlock === null"
            :title="latestArbitrageBlock === null
              ? 'No local Dune arbitrage record is available'
              : `Show blocks around latest recorded arbitrage block #${latestArbitrageBlock}`"
            @click="navigateLatestArbitrage"
          >
            Latest Arbitrage
          </button>
          <button class="nav-btn" @click="navigateNewer">Newer 100</button>
        </div>
      </template>

      <!-- Transactions view: selected tx info -->
      <template v-else>
        <div class="tx-info" v-if="selectedTxInfo">
          <div class="tx-info-row">
            <span class="tx-info-label">Hash:</span>
            <span class="tx-info-value mono">{{ truncateHash(selectedTxInfo.hash) }}</span>
            <button class="copy-btn" @click="copyToClipboard(selectedTxInfo.hash)" title="Copy hash">&#x2750;</button>
          </div>
          <div class="tx-info-row">
            <span class="tx-info-label">Gas:</span>
            <span class="tx-info-value">{{ selectedTxInfo.gas.toLocaleString() }}</span>
          </div>
          <div class="tx-info-row">
            <span class="tx-info-label">From:</span>
            <span class="tx-info-value mono">{{ selectedTxInfo.from_addr }}</span>
          </div>
          <div class="tx-info-row">
            <span class="tx-info-label">To:</span>
            <span class="tx-info-value mono">{{ selectedTxInfo.to_addr || 'Contract Creation' }}</span>
          </div>
        </div>
        <div class="tx-info-placeholder" v-else>
          Click a transaction to see details
        </div>
      </template>
    </div>

    <!-- Loading/Error states -->
    <div v-if="viewMode === 'blocks' && blocksLoading" class="status-loading">Loading blocks...</div>
    <div v-else-if="viewMode === 'blocks' && blocksError" class="status-error">{{ blocksError }}</div>
    <div v-if="viewMode === 'transactions' && loading" class="status-loading">Loading block data...</div>
    <div v-else-if="viewMode === 'transactions' && error" class="status-error">{{ error }}</div>

    <!-- Heatmap area -->
    <div ref="heatmapContainer" class="heatmap-container">
      <div v-if="viewMode === 'blocks'" ref="blockPlotContainer" class="plot-area"></div>
      <div v-else-if="blockData" ref="txPlotContainer" class="plot-area"></div>
    </div>
  </div>
</template>

<style scoped>
.block-panel {
  position: relative;
  width: 100%;
  min-width: 0;
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

.top-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex-shrink: 0;
}

.panel-label {
  font-size: 11px;
  color: #000000;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
}

.view-mode-pill {
  border: 1px solid rgba(148, 163, 184, 0.45);
  background: rgba(255, 255, 255, 0.94);
  color: #334155;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 9px;
  letter-spacing: 0.3px;
  line-height: 1.2;
}

.back-btn-top {
  position: absolute;
  top: 6px;
  right: clamp(8px, 1.2vw, 12px);
  z-index: 2;
  padding: 1px 6px;
  font-size: 9px;
  line-height: 1.1;
  border-radius: 999px;
}

.header-area {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}

.time-banner {
  font-size: 11px;
  color: var(--muted);
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.arbitrage-coverage {
  color: #9a6700;
  font-size: 9px;
  text-align: center;
}

.latest-block-num {
  font-family: 'Consolas', 'Monaco', monospace;
  color: var(--text);
}

.age-text {
  color: var(--muted);
}

.live-badge {
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.5px;
  color: #4caf50;
  border: 1px solid #4caf50;
  border-radius: 2px;
  padding: 1px 4px;
  line-height: 1.2;
  animation: pulse-live 2s ease-in-out infinite;
}

@keyframes pulse-live {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.nav-buttons {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 4px;
  justify-content: center;
}

.nav-btn {
  padding: 3px 10px;
  font-size: 11px;
  border: 1px solid var(--border);
  border-radius: 2px;
  background: var(--panel-bg);
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}

.nav-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.nav-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
  border-color: var(--border);
  color: var(--muted);
}

.nav-btn-home {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.nav-btn-home:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
  color: #fff;
}

.nav-btn-arbitrage {
  border-color: #e53935;
  color: #c62828;
  font-weight: 600;
}

.nav-btn-arbitrage:not(:disabled):hover {
  border-color: #c62828;
  color: #b71c1c;
}

.copy-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--muted);
  font-size: 12px;
  padding: 0 2px;
  line-height: 1;
  flex-shrink: 0;
}

.copy-btn:hover {
  color: var(--accent);
}

.tx-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px 0;
}

.tx-info-row {
  display: flex;
  gap: 6px;
  font-size: 11px;
  line-height: 1.4;
}

.tx-info-label {
  color: var(--muted);
  min-width: 36px;
  flex-shrink: 0;
}

.tx-info-value {
  color: var(--text);
  word-break: break-all;
}

.tx-info-value.mono {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 10px;
}

.tx-info-placeholder {
  font-size: 11px;
  color: var(--muted);
  padding: 4px 0;
}

.status-loading {
  color: var(--accent);
  font-size: 11px;
  padding: 10px;
}

.status-error {
  color: var(--error);
  font-size: 11px;
  padding: 10px;
}

.gas-legend {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 6px;
  padding: 2px 0;
  flex-shrink: 0;
}

.gas-unit {
  font-size: 9px;
  color: #000000;
  white-space: nowrap;
}

.gas-label {
  font-size: 9px;
  color: #000000;
  white-space: nowrap;
}

.gas-bar {
  flex: 1;
  max-width: 160px;
  height: 3px;
  border-radius: 1.5px;
}

.gas-bar.blocks {
  background: linear-gradient(to right, #F2F4F8, #CAD5E4, #7F97B8);
}

.gas-bar.transactions {
  background: linear-gradient(to right, #F2F6F6, #D2E3DE, #93A89F);
}

.heatmap-container {
  flex: 1;
  width: 100%;
  min-width: 0;
  overflow-x: hidden;
  overflow-y: visible;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 8px 0;
}

.plot-area {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  padding: 2px 0;
  flex: 0 0 auto;
}
</style>
