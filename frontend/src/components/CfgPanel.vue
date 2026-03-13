<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { graphviz } from 'd3-graphviz'
import { zoomIdentity, zoomTransform } from 'd3-zoom'
import { select } from 'd3-selection'
import { fetchCfgDotFile, fetchBlockInformation, fetchLegendData, type BlockInformationMap, type BlockInformation } from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: number[] | null
  filteredEdgeIds: string[] | null
  isAnalyzing: boolean
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: number[] | null]
}>()

const dotContent = ref<string>('')
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')

// Block information state (from folded_blocks_information.json)
const blockInformation = ref<BlockInformationMap>({})
const selectedBlockId = ref<number | null>(null)
const selectedBlockInfo = ref<BlockInformation | null>(null)
const instructionsPanel = ref<HTMLElement | null>(null)

// Address-to-name mapping (from legend.json)
const addressNameMap = ref<Map<string, string>>(new Map())

const graphContainer = ref<HTMLElement | null>(null)
let graphvizInstance: any = null
let initialTransform: any = null

// 过滤和高亮状态
const highlightedNodes = ref<Set<string>>(new Set())
const visibleNodes = ref<Set<string>>(new Set())
const visibleEdges = ref<Set<string>>(new Set())
const edgeConnections = ref<Map<string, {source: string, target: string}>>(new Map())
const nodeNameToEl = ref<Map<string, Element>>(new Map())
// Map edge label number (e.g., "28") to edge title (e.g., "node_5->node_12")
const edgeLabelToTitle = ref<Map<string, string>>(new Map())

watch(() => props.txHash, (newHash) => {
  selectedBlockId.value = null
  selectedBlockInfo.value = null
  if (newHash) {
    loadCfgData(newHash)
  }
}, { immediate: true })

async function loadCfgData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''

  try {
    // DOT now only contains IDs, use directly without processing
    const dot = await fetchCfgDotFile(txHash)
    dotContent.value = dot
    status.value = 'success'

    // Fetch block information and legend data in parallel
    try {
      const [info, legend] = await Promise.all([
        fetchBlockInformation(txHash),
        fetchLegendData(txHash).catch(() => null)
      ])
      blockInformation.value = info

      // Build address-to-name map from legend
      addressNameMap.value.clear()
      if (legend) {
        for (const entry of [...legend.user_addresses, ...legend.erc20_tokens, ...legend.normal_contracts]) {
          addressNameMap.value.set(entry.address.toLowerCase(), entry.name)
        }
      }
    } catch (e) {
      console.warn('Failed to load block information:', e)
    }

    await nextTick()
    await renderGraph()
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load CFG data'
  }
}

async function renderGraph() {
  if (!graphContainer.value || !dotContent.value) return

  try {
    const container = graphContainer.value

    graphvizInstance = graphviz(container, {
      useWorker: false,
      zoom: true,
      fit: true,
      width: container.clientWidth,
      height: container.clientHeight,
    })

    await graphvizInstance
      .renderDot(dotContent.value)
      .on('end', attachInteractivity)
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

  try {
    initialTransform = zoomTransform(svg as Element)
  } catch {
    initialTransform = zoomIdentity
  }

  nodeNameToEl.value.clear()
  const nodes = svg.querySelectorAll('.node')
  nodes.forEach((node) => {
    const nodeEl = node as SVGElement
    const title = node.querySelector('title')?.textContent
    if (title) {
      nodeNameToEl.value.set(title, node)
    }

    nodeEl.addEventListener('mouseenter', () => {
      nodeEl.classList.add('hovered')
    })

    nodeEl.addEventListener('mouseleave', () => {
      nodeEl.classList.remove('hovered')
    })

    nodeEl.addEventListener('click', (e) => {
      e.stopPropagation()
      handleNodeClick(title || '')
    })
  })

  parseEdgeConnections()
  buildEdgeLabelMap()
}

function handleNodeClick(nodeName: string) {
  const match = nodeName.match(/^node_(\d+)$/)
  if (!match) return

  const blockId = parseInt(match[1], 10)

  // Remove previous selection highlight
  if (selectedBlockId.value !== null) {
    const prevNodeName = `node_${selectedBlockId.value}`
    const prevNode = nodeNameToEl.value.get(prevNodeName)
    if (prevNode) {
      prevNode.classList.remove('selected')
    }
  }

  // Toggle: if clicking the same node, deselect
  if (selectedBlockId.value === blockId) {
    selectedBlockId.value = null
    selectedBlockInfo.value = null
    return
  }

  // Set selected block and add highlight
  selectedBlockId.value = blockId
  selectedBlockInfo.value = blockInformation.value[String(blockId)] || null
  const currentNode = nodeNameToEl.value.get(nodeName)
  if (currentNode) {
    currentNode.classList.add('selected')
  }

  // Scroll to the block in the instructions panel
  nextTick(() => {
    const blockElement = document.getElementById(`block-${blockId}`)
    if (blockElement && instructionsPanel.value) {
      blockElement.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  })
}

function formatInstruction(instr: string): string {
  const match = instr.match(/\('([^']+)',\s*'([^']+)'\)/)
  if (match) {
    return `${match[1]}  ${match[2]}`
  }
  return instr
}

function addrToName(addr: string): string {
  if (!addr) return addr
  const name = addressNameMap.value.get(addr.toLowerCase())
  if (name) return name
  if (addr.length < 12) return addr
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

function hexToEth(hex: string): string {
  if (!hex) return '0'
  try {
    const wei = BigInt(hex)
    const ethWhole = wei / BigInt(1e14)  // 保留4位小数精度
    const ethNum = Number(ethWhole) / 10000
    if (ethNum === 0 && wei > 0n) return '<0.0001 ETH'
    return `${ethNum} ETH`
  } catch {
    return hex
  }
}

function formatAction(action: any, index: number): string {
  const prefix = `Action${index + 1}`

  if (action.action_type === 'eth_transfer' && action.eth_event) {
    const ev = action.eth_event
    return `${prefix}: Send_ETH ${addrToName(ev.from)} \u2192 ${addrToName(ev.to)} ${hexToEth(ev.amount)}`
  }

  if (action.erc20_events && action.erc20_events.length > 0) {
    return action.erc20_events.map((ev: any, i: number) => {
      const type = ev.type === 'read' ? 'Read' : 'Write'
      const token = ev.tokenname || 'ERC20'
      const user = addrToName(ev.user || '')
      return `${prefix}${action.erc20_events.length > 1 ? `.${i + 1}` : ''}: ${type}_${token} ${user} bal:${ev.balance}`
    }).join('\n')
  }

  return `${prefix}: ${action.action_type}`
}

function parseEdgeConnections() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  edgeConnections.value.clear()

  const edges = svg.querySelectorAll('.edge')
  edges.forEach((edge) => {
    const title = edge.querySelector('title')?.textContent
    if (!title) return

    const match = title.match(/^(node_\d+)->(node_\d+)$/)
    if (match) {
      edgeConnections.value.set(title, {
        source: match[1],
        target: match[2]
      })
    }
  })
}

function buildEdgeLabelMap() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  edgeLabelToTitle.value.clear()

  const edges = svg.querySelectorAll('.edge')
  edges.forEach((edge) => {
    const title = edge.querySelector('title')?.textContent || ''
    if (!title) return

    // Extract the edge label number from <text> elements
    const textEls = edge.querySelectorAll('text')
    textEls.forEach(t => {
      const label = (t.textContent || '').trim()
      if (label && /^\d+$/.test(label)) {
        edgeLabelToTitle.value.set(label, title)
      }
    })
  })
}

function applyEdgeFilter(edgeIds: string[]) {
  // Convert edge IDs like "edge_28" to label numbers "28"
  const labelNumbers = edgeIds.map(id => id.replace(/\D/g, ''))

  // Find matching edge titles and their connected nodes
  const matchedEdgeTitles = new Set<string>()
  const matchedNodes = new Set<string>()

  for (const label of labelNumbers) {
    const edgeTitle = edgeLabelToTitle.value.get(label)
    if (edgeTitle) {
      matchedEdgeTitles.add(edgeTitle)
      const conn = edgeConnections.value.get(edgeTitle)
      if (conn) {
        matchedNodes.add(conn.source)
        matchedNodes.add(conn.target)
      }
    }
  }

  if (matchedNodes.size === 0) {
    // No matches — show all
    visibleNodes.value.clear()
    visibleEdges.value.clear()
    highlightedNodes.value.clear()
    applyFilter()
    nextTick(() => resetZoom())
    return
  }

  visibleNodes.value = matchedNodes
  visibleEdges.value = matchedEdgeTitles
  highlightedNodes.value = new Set(matchedNodes)
  applyFilter()
}

function calculateVisibleElements(targetBlockIds: number[]) {
  const targetNodeIds = targetBlockIds.map(id => `node_${id}`)
  const visible = new Set<string>(targetNodeIds)

  edgeConnections.value.forEach(({source, target}) => {
    if (targetNodeIds.includes(source)) {
      visible.add(target)
    }
    if (targetNodeIds.includes(target)) {
      visible.add(source)
    }
  })

  visibleNodes.value = visible

  const visibleEdgeSet = new Set<string>()
  edgeConnections.value.forEach(({source, target}, edgeId) => {
    if (visible.has(source) && visible.has(target)) {
      visibleEdgeSet.add(edgeId)
    }
  })
  visibleEdges.value = visibleEdgeSet

  highlightedNodes.value = new Set(targetNodeIds)
}

function applyFilter() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  const nodes = svg.querySelectorAll('.node')
  nodes.forEach((node) => {
    const nodeName = node.querySelector('title')?.textContent || ''
    if (visibleNodes.value.size === 0) {
      node.classList.remove('filtered-out', 'highlighted')
    } else if (visibleNodes.value.has(nodeName)) {
      node.classList.remove('filtered-out')
      if (highlightedNodes.value.has(nodeName)) {
        node.classList.add('highlighted')
      } else {
        node.classList.remove('highlighted')
      }
    } else {
      node.classList.add('filtered-out')
      node.classList.remove('highlighted')
    }
  })

  const edges = svg.querySelectorAll('.edge')
  edges.forEach((edge) => {
    const title = edge.querySelector('title')?.textContent || ''
    if (visibleNodes.value.size === 0) {
      edge.classList.remove('filtered-out')
    } else if (visibleEdges.value.has(title)) {
      edge.classList.remove('filtered-out')
    } else {
      edge.classList.add('filtered-out')
    }
  })
}

function applyZoomTransform(transform: any) {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  try {
    const zoomSel = graphvizInstance.zoomSelection()
    const zoomBeh = graphvizInstance.zoomBehavior()
    zoomSel.transition().duration(300).call(zoomBeh.transform, transform)
  } catch {
    try {
      const svgSel = select(svg)
      const zoomBeh = (svgSel.node() as any).__zoom_behavior
      if (zoomBeh) {
        svgSel.transition().duration(300).call(zoomBeh.transform, transform)
      } else {
        const g = svg.querySelector('g')
        if (g) {
          const { x, y, k } = transform
          g.setAttribute('transform', `translate(${x}, ${y}) scale(${k})`)
        }
      }
    } catch {
      const g = svg.querySelector('g')
      if (g) {
        const { x, y, k } = transform
        g.setAttribute('transform', `translate(${x}, ${y}) scale(${k})`)
      }
    }
  }
}

watch(() => props.highlightedBlockId, (newBlockIds) => {
  // Skip if edge filter from flame graph is active
  if (props.filteredEdgeIds && props.filteredEdgeIds.length > 0) return

  if (newBlockIds && newBlockIds.length > 0) {
    calculateVisibleElements(newBlockIds)
    applyFilter()
  } else {
    visibleNodes.value.clear()
    visibleEdges.value.clear()
    highlightedNodes.value.clear()
    applyFilter()
    nextTick(() => {
      resetZoom()
    })
  }
})

watch(() => props.filteredEdgeIds, (edgeIds) => {
  if (edgeIds && edgeIds.length > 0) {
    applyEdgeFilter(edgeIds)
  } else if (!props.highlightedBlockId || props.highlightedBlockId.length === 0) {
    // Only clear if no block highlight is active either
    visibleNodes.value.clear()
    visibleEdges.value.clear()
    highlightedNodes.value.clear()
    applyFilter()
    nextTick(() => resetZoom())
  }
})

function resetZoom() {
  if (!graphvizInstance || !initialTransform) return
  applyZoomTransform(initialTransform)
}

function resetFilter() {
  emit('cfg-navigate', null)
}

const showEdgeTypes = ref(false)

const edgeTypes = [
  { type: 'NORMAL', color: '#939393', desc: 'Non-terminating opcodes' },
  { type: 'JUMP', color: '#242424', desc: 'JUMP, JUMPI' },
  { type: 'CALL', color: '#1F6800', desc: 'CALL, CALLCODE, STATICCALL' },
  { type: 'DELEGATECALL', color: '#009DFF', desc: 'DELEGATECALL' },
  { type: 'TERMINATE', color: '#C14A00', desc: 'RETURN, STOP, REVERT, INVALID, SELFDESTRUCT' },
]

</script>

<template>
  <div class="cfg-panel">
    <span class="panel-label">
      (e) Control Flow Graph
      <span
        class="edge-info-icon"
        :class="{ active: showEdgeTypes }"
        @mouseenter="showEdgeTypes = true"
        @mouseleave="showEdgeTypes = false"
      >
        <svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.2" />
          <text x="7" y="10.5" text-anchor="middle" font-size="9" font-weight="600" fill="currentColor">?</text>
        </svg>
        <div v-show="showEdgeTypes" class="edge-types-tooltip">
          <div class="edge-tooltip-title">CFG's Edge Types</div>
          <div v-for="edge in edgeTypes" :key="edge.type" class="edge-type-item">
            <svg width="32" height="12" viewBox="0 0 32 12">
              <line x1="2" y1="6" x2="24" y2="6" :stroke="edge.color" stroke-width="2.5" />
              <polygon :points="'24,3 30,6 24,9'" :fill="edge.color" />
            </svg>
            <div class="edge-type-text">
              <span class="edge-type-name">{{ edge.type }}</span>
              <span class="edge-type-desc">{{ edge.desc }}</span>
            </div>
          </div>
        </div>
      </span>
    </span>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading control flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="cfg-container">
      <div ref="graphContainer" class="graph-viewport"></div>
      <button
        v-if="visibleNodes.size > 0"
        class="reset-button"
        @click="resetFilter"
      >Show All</button>

      <!-- Right Side Panel: Information + Instructions -->
      <div class="side-panel">
        <!-- Upper: Information Panel -->
        <div class="info-panel">
          <div class="panel-section-header">Information</div>
          <div class="info-content">
            <template v-if="selectedBlockInfo">
              <div class="info-row">
                <span class="info-label">ID</span>
                <span class="info-value">{{ selectedBlockInfo.block_id }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Contract</span>
                <span class="info-value">{{ selectedBlockInfo.address }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Blocks</span>
                <span class="info-value">{{ selectedBlockInfo.blocks_number }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">StartPC</span>
                <span class="info-value">{{ selectedBlockInfo.start_pc }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">EndPC</span>
                <span class="info-value">{{ selectedBlockInfo.end_pc }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Gas</span>
                <span class="info-value">{{ selectedBlockInfo.gas }}</span>
              </div>
              <div v-if="selectedBlockInfo.actions.length > 0" class="info-actions">
                <div class="info-label">Actions</div>
                <div v-for="(action, idx) in selectedBlockInfo.actions" :key="idx" class="action-line">
                  {{ formatAction(action, idx) }}
                </div>
              </div>
            </template>
            <div v-else class="panel-placeholder">Click a node to view details</div>
          </div>
        </div>

        <!-- Divider -->
        <div class="panel-divider"></div>

        <!-- Lower: Instructions Panel -->
        <div ref="instructionsPanel" class="instructions-panel-section">
          <div class="panel-section-header">Instructions</div>
          <div class="instructions-content">
            <template v-if="selectedBlockInfo">
              <div
                :id="`block-${selectedBlockInfo.block_id}`"
                class="block-section selected"
              >
                <div class="block-header">Block {{ selectedBlockInfo.block_id }}</div>
                <div class="block-instructions">
                  <div
                    v-for="(instr, idx) in selectedBlockInfo.instructions"
                    :key="idx"
                    class="instruction-line"
                  >
                    {{ formatInstruction(instr) }}
                  </div>
                </div>
              </div>
            </template>
            <div v-else class="panel-placeholder">Click a node to view instructions</div>
          </div>
        </div>
      </div>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Enter a transaction hash to view control flow</span>
    </div>
  </div>
</template>

<style scoped>
.cfg-panel {
  position: relative;
  background: var(--panel-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-sizing: border-box;
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

.cfg-container {
  position: relative;
  width: 100%;
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 0;
  overflow: hidden;
  box-sizing: border-box;
}

.graph-viewport {
  flex: 1;
  overflow: hidden;
  min-width: 0;
  min-height: 0;
  margin-top: 28px;
}

.graph-viewport :deep(svg) {
  width: 100%;
  height: 100%;
}

.graph-viewport :deep(.node) {
  transition: opacity 0.15s;
  cursor: pointer;
}

.graph-viewport :deep(.node text) {
  cursor: pointer;
  user-select: none;
}

.graph-viewport :deep(.node.hovered ellipse),
.graph-viewport :deep(.node.hovered polygon),
.graph-viewport :deep(.node.hovered path) {
  stroke-width: 2;
  filter: brightness(1.05);
}

/* 过滤掉的元素 */
.graph-viewport :deep(.node.filtered-out),
.graph-viewport :deep(.edge.filtered-out) {
  opacity: 0.08;
  pointer-events: none;
}

/* 高亮的目标节点 */
.graph-viewport :deep(.node.highlighted) {
  filter: drop-shadow(0 0 8px rgba(255, 0, 0, 0.6));
}

.graph-viewport :deep(.node.highlighted ellipse),
.graph-viewport :deep(.node.highlighted polygon),
.graph-viewport :deep(.node.highlighted path) {
  stroke: #ff0000;
  stroke-width: 3;
}

/* 选中的节点 */
.graph-viewport :deep(.node.selected) {
  filter: drop-shadow(0 0 12px rgba(100, 120, 200, 1));
}

.graph-viewport :deep(.node.selected ellipse),
.graph-viewport :deep(.node.selected polygon),
.graph-viewport :deep(.node.selected path) {
  stroke: #6478c8;
  stroke-width: 4;
  fill: rgba(100, 120, 200, 0.15);
}

/* 平滑过渡 */
.graph-viewport :deep(.node),
.graph-viewport :deep(.edge) {
  transition: opacity 0.2s ease;
}

.reset-button {
  position: absolute;
  top: 32px;
  right: 168px;
  padding: 3px 10px;
  font-size: 10px;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 3px;
  cursor: pointer;
  z-index: 10;
  transition: background 0.15s;
}

.reset-button:hover {
  background: var(--accent-hover);
}

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

/* Right Side Panel */
.side-panel {
  width: 160px;
  background: var(--bg);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  min-height: 0;
  align-self: stretch;
  max-height: 100%;
}

.panel-section-header {
  padding: 4px 8px;
  font-size: 9px;
  color: var(--muted);
  font-weight: 500;
  letter-spacing: 0.3px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
  flex-shrink: 0;
}

.info-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.info-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 8px;
  min-height: 0;
}

.info-row {
  display: flex;
  justify-content: space-between;
  padding: 2px 0;
  font-size: 9px;
  gap: 4px;
}

.info-label {
  color: var(--muted);
  font-weight: 500;
  font-size: 9px;
  flex-shrink: 0;
}

.info-value {
  color: var(--text);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 9px;
  text-align: right;
  word-break: break-all;
}

.info-actions {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px solid var(--border);
}

.action-line {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 8px;
  color: #666;
  padding: 1px 0;
  line-height: 1.3;
  word-break: break-all;
}

.panel-placeholder {
  color: var(--muted);
  font-size: 9px;
  text-align: center;
  padding: 12px 4px;
}

.panel-divider {
  height: 1px;
  background: var(--border);
  flex-shrink: 0;
}

.instructions-panel-section {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* Scrollbar for info panel */
.info-content::-webkit-scrollbar {
  width: 6px;
}

.info-content::-webkit-scrollbar-track {
  background: var(--bg);
}

.info-content::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.info-content::-webkit-scrollbar-thumb:hover {
  background: var(--muted);
}

.instructions-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 0;
  min-height: 0;
}

.block-section {
  margin-bottom: 8px;
  padding: 0 8px;
  transition: background 0.15s;
}

.block-section.selected {
  background: rgba(100, 120, 200, 0.08);
  border-left: 2px solid var(--accent);
  padding-left: 6px;
}

.block-header {
  font-size: 9px;
  color: var(--muted);
  font-weight: 600;
  letter-spacing: 0.5px;
  margin-bottom: 3px;
  text-transform: uppercase;
}

.block-instructions {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 10px;
  line-height: 1.4;
}

.instruction-line {
  white-space: nowrap;
  color: #666666;
  padding: 0.5px 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Custom scrollbar for instructions panel */
.instructions-content::-webkit-scrollbar {
  width: 6px;
}

.instructions-content::-webkit-scrollbar-track {
  background: var(--bg);
}

.instructions-content::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.instructions-content::-webkit-scrollbar-thumb:hover {
  background: var(--muted);
}

/* Edge Types Info Icon & Tooltip */
.edge-info-icon {
  position: relative;
  display: inline-flex;
  align-items: center;
  margin-left: 4px;
  cursor: pointer;
  color: var(--muted);
  vertical-align: middle;
  transition: color 0.15s;
}

.edge-info-icon.active,
.edge-info-icon:hover {
  color: var(--accent);
}

.edge-types-tooltip {
  position: absolute;
  top: 20px;
  left: 0;
  background: rgba(255, 255, 255, 0.98);
  border: 1px solid var(--border);
  border-radius: 3px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  padding: 8px 10px;
  z-index: 200;
  min-width: 220px;
  white-space: nowrap;
}

.edge-tooltip-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.edge-type-item {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.edge-type-item:last-child {
  margin-bottom: 0;
}

.edge-type-item svg {
  flex-shrink: 0;
}

.edge-type-text {
  display: flex;
  flex-direction: column;
}

.edge-type-name {
  font-size: 10px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.3;
}

.edge-type-desc {
  font-size: 9px;
  color: var(--muted);
  line-height: 1.2;
}
</style>
