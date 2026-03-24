<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { graphviz } from 'd3-graphviz'
import LegendPanel from './LegendPanel.vue'
import { fetchDotFile, fetchEdgeLink, fetchArbitrageResult, fetchAddressBalances, fetchLegendData, type BlockId, type EdgeLink } from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: BlockId[] | null
  isAnalyzing: boolean
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: BlockId[] | null]
}>()

const dotContent = ref<string>('')
const edgeLinks = ref<EdgeLink[]>([])
const selectedEdgeId = ref<number | null>(null)
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')

const graphContainer = ref<HTMLElement | null>(null)
let graphvizInstance: any = null

// 套利相关状态
const isArbitrage = ref(false)
const arbCycles = ref<number[][]>([])
const arbOrders = ref<Set<number>>(new Set())

// 地址余额数据：地址(小写) -> { token -> 净变化量 }
const addressBalances = ref<Record<string, Record<string, number>>>({})
// display name(小写) -> 地址(小写) 的反向映射，用于节点名匹配
const nameToAddress = ref<Record<string, string>>({})

// tooltip 状态
const tooltipVisible = ref(false)
const tooltipX = ref(0)
const tooltipY = ref(0)
const tooltipName = ref('')
const tooltipBalances = ref<Record<string, number>>({})

watch(() => props.txHash, (newHash) => {
  if (newHash) {
    loadAfgData(newHash)
  }
}, { immediate: true })

watch(() => props.highlightedBlockId, (newBlockIds) => {
  if (!newBlockIds || newBlockIds.length === 0) {
    selectedEdgeId.value = null
  }
})

async function loadAfgData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''
  selectedEdgeId.value = null
  tooltipVisible.value = false

  isArbitrage.value = false
  arbCycles.value = []
  arbOrders.value = new Set()
  addressBalances.value = {}
  nameToAddress.value = {}   // ← 重置

  try {
    const [dot, links, arb, balances, legend] = await Promise.all([
      fetchDotFile(txHash),
      fetchEdgeLink(txHash),
      fetchArbitrageResult(txHash),
      fetchAddressBalances(txHash),
      fetchLegendData(txHash),
    ])

    dotContent.value = dot
    edgeLinks.value = links

    isArbitrage.value = arb.is_arbitrage
    arbCycles.value = arb.cycles
    arbOrders.value = new Set(arb.arb_edge_orders)

    // 地址统一转小写
    addressBalances.value = Object.fromEntries(
      Object.entries(balances).map(([addr, tokens]) => [addr.toLowerCase(), tokens])
    )

    // ← 建立 displayName -> address 反向映射
    const nameMap: Record<string, string> = {}
    const allEntries = [
      ...legend.user_addresses,
      ...legend.erc20_tokens,
      ...legend.normal_contracts,
    ]
    for (const entry of allEntries) {
      if (entry.name && entry.address) {
        nameMap[entry.name.toLowerCase()] = entry.address.toLowerCase()
      }
    }
    nameToAddress.value = nameMap

    status.value = 'success'

    await nextTick()
    await renderGraph()
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load AFG data'
  }
}

async function renderGraph() {
  if (!graphContainer.value || !dotContent.value) return

  try {
    const container = graphContainer.value

    graphvizInstance = graphviz(container, {
      useWorker: true,
      zoom: true,
      fit: true,
      width: container.clientWidth,
      height: container.clientHeight,
    })

    await new Promise<void>((resolve, reject) => {
      try {
        graphvizInstance
          .renderDot(dotContent.value)
          .on('end', () => {
            attachInteractivity()
            resolve()
          })
      } catch (e) {
        reject(e)
      }
    })
  } catch (e) {
    console.error('Graphviz render error:', e)
    errorMsg.value = 'Failed to render graph'
    status.value = 'error'
  }
}

function attachInteractivity() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  const nodes = svg.querySelectorAll('.node')
  nodes.forEach((node) => {
    const nodeEl = node as SVGElement

    nodeEl.addEventListener('mouseenter', () => nodeEl.classList.add('hovered'))
    nodeEl.addEventListener('mouseleave', () => nodeEl.classList.remove('hovered'))

    // graphviz SVG 里 .node 的 <title> 存的是 DOT 节点名（display name 或地址）
    const titleEl = node.querySelector('title')
    const nodeId = titleEl?.textContent?.trim() || ''
    const balances = findBalancesForNode(nodeId)

    if (balances && Object.keys(balances).length > 0) {
      const capturedName = nodeId
      const capturedBalances = balances

      nodeEl.addEventListener('mouseenter', (e) => {
        showTooltip(e as MouseEvent, capturedName, capturedBalances)
      })
      nodeEl.addEventListener('mousemove', (e) => {
        moveTooltip(e as MouseEvent)
      })
      nodeEl.addEventListener('mouseleave', () => {
        hideTooltip()
      })
    }
  })

  const edges = svg.querySelectorAll('.edge')
  edges.forEach((edge) => {
    const edgeEl = edge as SVGElement

    const textEls = edge.querySelectorAll('text')
    let edgeId: number | null = null
    textEls.forEach(t => {
      if (edgeId !== null) return
      const m = (t.textContent || '').match(/\((\d+)\)/)
      if (m) edgeId = parseInt(m[1])
    })

    if (edgeId !== null && arbOrders.value.has(edgeId)) {
      edgeEl.classList.add('arb-highlight')
    }

    if (edgeId !== null) {
      const capturedId = edgeId

      edgeEl.classList.add('interactive')

      const paths = edge.querySelectorAll('path')
      paths.forEach(path => {
        const hitArea = path.cloneNode(false) as SVGPathElement
        hitArea.classList.add('hit-area')
        hitArea.setAttribute('stroke', 'transparent')
        hitArea.setAttribute('stroke-width', '18')
        hitArea.setAttribute('fill', 'none')
        hitArea.setAttribute('pointer-events', 'stroke')
        hitArea.addEventListener('click', (e) => {
          e.stopPropagation()
          handleEdgeClick(capturedId)
        })
        hitArea.addEventListener('mouseenter', () => edgeEl.classList.add('hovered'))
        hitArea.addEventListener('mouseleave', () => edgeEl.classList.remove('hovered'))
        path.parentNode?.insertBefore(hitArea, path)
      })

      const polygons = edge.querySelectorAll('polygon')
      polygons.forEach(el => {
        el.addEventListener('click', (e) => {
          e.stopPropagation()
          handleEdgeClick(capturedId)
        })
        el.addEventListener('mouseenter', () => edgeEl.classList.add('hovered'))
        el.addEventListener('mouseleave', () => edgeEl.classList.remove('hovered'))
      })
    }
  })
}

// 根据节点 DOT 名查找余额
// 匹配顺序：① legend name -> address 映射 ② 节点名本身就是地址
function findBalancesForNode(nodeId: string): Record<string, number> | null {
  if (!nodeId) return null
  const lower = nodeId.toLowerCase()

  // ① 用 legend 映射把 display name 转成地址再查
  const addr = nameToAddress.value[lower]
  if (addr && addressBalances.value[addr]) return addressBalances.value[addr]

  // ② 节点名本身就是地址（0x...）
  if (addressBalances.value[lower]) return addressBalances.value[lower]

  return null
}

// tooltip 显示 / 移动 / 隐藏
function showTooltip(e: MouseEvent, name: string, balances: Record<string, number>) {
  tooltipName.value = name
  tooltipBalances.value = balances
  tooltipVisible.value = true
  moveTooltip(e)
}

function moveTooltip(e: MouseEvent) {
  if (!graphContainer.value) return
  const rect = graphContainer.value.getBoundingClientRect()
  let x = e.clientX - rect.left + 14
  let y = e.clientY - rect.top + 14
  if (x + 200 > rect.width) x = e.clientX - rect.left - 214
  tooltipX.value = x
  tooltipY.value = y
}

function hideTooltip() {
  tooltipVisible.value = false
}

// 格式化余额：正数显示 +，负数显示 -，保留5位有效数字
function formatAmount(val: number): string {
  if (val === 0) return '0'
  const sign = val > 0 ? '+' : ''
  const abs = Math.abs(val)
  if (abs >= 0.0001 && abs < 1e8) return sign + parseFloat(val.toPrecision(5)).toString()
  return sign + val.toExponential(3)
}

function highlightAllArb() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return
  svg.querySelectorAll('.edge.arb-highlight').forEach(el => {
    el.classList.add('arb-flash')
    setTimeout(() => (el as SVGElement).classList.remove('arb-flash'), 900)
  })
}

function handleEdgeClick(edgeId: number) {
  const hasActiveFilter = !!(props.highlightedBlockId && props.highlightedBlockId.length > 0)
  if (selectedEdgeId.value === edgeId && hasActiveFilter) {
    selectedEdgeId.value = null
    emit('cfg-navigate', null)
    return
  }

  selectedEdgeId.value = edgeId

  const link = edgeLinks.value.find(l => l.edge_id === edgeId)
  if (!link) {
    console.warn(`No CFG mapping found for edge ${edgeId}`)
    return
  }

  const blockIds: BlockId[] = []

  if (typeof link.matched_blocks === 'number' || typeof link.matched_blocks === 'string') {
    blockIds.push(link.matched_blocks)
  } else if (Array.isArray(link.matched_blocks)) {
    blockIds.push(...link.matched_blocks)
  } else if (typeof link.matched_blocks === 'object') {
    if (link.matched_blocks.sender) blockIds.push(...link.matched_blocks.sender)
    if (link.matched_blocks.receiver) blockIds.push(...link.matched_blocks.receiver)
  }

  const uniqueBlockIds = [...new Set(blockIds)]

  if (uniqueBlockIds.length > 0) {
    console.log(`Navigating to CFG blocks ${uniqueBlockIds.join(', ')} from AFG edge ${edgeId}`)
    emit('cfg-navigate', uniqueBlockIds)
  }
}
</script>

<template>
  <div class="afg-panel">
    <span class="panel-label">(c) Asset Flow Graph</span>

    <!-- 套利 badge -->
    <div v-if="status === 'success' && isArbitrage" class="arb-badge">
      <span class="arb-icon">⚠</span>
      Arbitrage detected — {{ arbCycles.length }} cycle(s)
      <button class="arb-btn" @click="highlightAllArb">Flash edges</button>
    </div>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading asset flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="afg-container">
      <div ref="graphContainer" class="graph-viewport"></div>
      <LegendPanel :tx-hash="props.txHash" />

      <!-- 节点余额悬浮卡片 -->
      <div
        v-if="tooltipVisible"
        class="node-tooltip"
        :style="{ left: tooltipX + 'px', top: tooltipY + 'px' }"
      >
        <div class="tooltip-title">{{ tooltipName }}</div>
        <div class="tooltip-divider"></div>
        <div
          v-for="(val, token) in tooltipBalances"
          :key="token"
          class="tooltip-row"
        >
          <span class="tooltip-token">{{ token }}</span>
          <span
            class="tooltip-amount"
            :class="val > 0 ? 'positive' : val < 0 ? 'negative' : 'zero'"
          >{{ formatAmount(Number(val)) }}</span>
        </div>
      </div>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Enter a transaction hash to view asset flow</span>
    </div>
  </div>
</template>

<style scoped>
.afg-panel {
  position: relative;
  background: var(--panel-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
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

/* 套利 badge */
.arb-badge {
  position: absolute;
  top: 6px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 6px;
  background: rgba(220, 80, 30, 0.12);
  border: 1px solid rgba(220, 80, 30, 0.5);
  border-radius: 6px;
  padding: 3px 10px;
  font-size: 11px;
  color: #dc501e;
  white-space: nowrap;
}

.arb-icon {
  font-size: 12px;
}

.arb-btn {
  background: rgba(220, 80, 30, 0.15);
  border: 1px solid rgba(220, 80, 30, 0.4);
  border-radius: 4px;
  color: #dc501e;
  font-size: 10px;
  padding: 1px 7px;
  cursor: pointer;
  transition: background 0.15s;
}

.arb-btn:hover {
  background: rgba(220, 80, 30, 0.28);
}

.afg-container {
  position: relative;
  flex: 1;
  min-height: 0;
  padding-top: 28px;
  display: flex;
  flex-direction: row;
  overflow: hidden;
}

.graph-viewport {
  flex: 1;
  min-width: 0;
  height: 100%;
  overflow: hidden;
}

.graph-viewport :deep(svg) {
  width: 100%;
  height: 100%;
}

.graph-viewport :deep(.edge.interactive) {
  cursor: pointer;
}

.graph-viewport :deep(.edge text) {
  pointer-events: none;
}

.graph-viewport :deep(.node) {
  transition: opacity 0.15s;
}

.graph-viewport :deep(.node.hovered ellipse),
.graph-viewport :deep(.node.hovered polygon),
.graph-viewport :deep(.node.hovered path) {
  stroke-width: 2;
  filter: brightness(1.05);
}

.graph-viewport :deep(.edge) {
  transition: opacity 0.15s;
}

.graph-viewport :deep(.hit-area) {
  stroke: transparent !important;
  stroke-width: 18px !important;
  fill: none !important;
  filter: none !important;
  cursor: pointer;
}

.graph-viewport :deep(.edge.hovered path:not(.hit-area)),
.graph-viewport :deep(.edge.hovered polygon) {
  stroke-width: 3;
  filter: brightness(1.2);
}

/* 套利边加粗，不改色 */
.graph-viewport :deep(.edge.arb-highlight path:not(.hit-area)) {
  stroke-width: 4px;
}

@keyframes arb-flash {
  0%, 100% { opacity: 1; }
  40%       { opacity: 0.15; }
}

.graph-viewport :deep(.edge.arb-flash path:not(.hit-area)),
.graph-viewport :deep(.edge.arb-flash polygon) {
  animation: arb-flash 0.9s ease;
}

/* 节点余额悬浮卡片 */
.node-tooltip {
  position: absolute;
  z-index: 100;
  pointer-events: none;
  background: var(--panel-bg, #1a1a2e);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 7px;
  padding: 8px 11px;
  min-width: 160px;
  max-width: 220px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}

.tooltip-title {
  font-size: 10px;
  color: var(--muted, #888);
  margin-bottom: 6px;
  word-break: break-all;
  line-height: 1.4;
}

.tooltip-divider {
  height: 1px;
  background: rgba(255, 255, 255, 0.08);
  margin-bottom: 6px;
}

.tooltip-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 2px 0;
}

.tooltip-token {
  font-size: 11px;
  color: var(--text, #ccc);
  font-weight: 500;
}

.tooltip-amount {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.tooltip-amount.positive { color: #4ade80; }
.tooltip-amount.negative { color: #f87171; }
.tooltip-amount.zero     { color: var(--muted, #888); }

.status-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: var(--accent);
  font-size: 12px;
}

.status-overlay.error {
  color: var(--error);
}

.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.placeholder-text {
  color: var(--muted);
  font-size: 12px;
}
</style>
