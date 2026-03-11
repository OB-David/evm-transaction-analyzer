<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { graphviz } from 'd3-graphviz'
import { zoomIdentity, zoomTransform } from 'd3-zoom'
import { select } from 'd3-selection'
import { fetchCfgDotFile, fetchBlockInstructions, type BlockInstructionsMap } from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: number[] | null
  isAnalyzing: boolean
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: number[] | null]
}>()

const dotContent = ref<string>('')
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')

// Node metadata extracted from DOT labels
interface NodeMetadata {
  id: number
  contractName: string
  blocks: number
  startPC: string
  endPC: string
  gas: string
  actions: string[]
  shape: 'record' | 'ellipse'
}

const nodeMetadataMap = ref<Map<number, NodeMetadata>>(new Map())
const selectedNodeMetadata = ref<NodeMetadata | null>(null)

// Block instructions state
const blockInstructions = ref<BlockInstructionsMap>({})
const selectedBlockId = ref<number | null>(null)
const instructionsPanel = ref<HTMLElement | null>(null)

const graphContainer = ref<HTMLElement | null>(null)
let graphvizInstance: any = null
let initialTransform: any = null  // 保存初始fit变换,用于reset

// 过滤和高亮状态
const highlightedNodes = ref<Set<string>>(new Set())
const visibleNodes = ref<Set<string>>(new Set())
const visibleEdges = ref<Set<string>>(new Set())
const edgeConnections = ref<Map<string, {source: string, target: string}>>(new Map())
// DOT节点名 -> SVG元素的映射 (graphviz自动生成的id与DOT名不同)
const nodeNameToEl = ref<Map<string, Element>>(new Map())

watch(() => props.txHash, (newHash) => {
  selectedBlockId.value = null // Reset selection when switching transactions
  selectedNodeMetadata.value = null
  nodeMetadataMap.value.clear()
  if (newHash) {
    loadCfgData(newHash)
  }
}, { immediate: true })

async function loadCfgData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''

  try {
    const dot = await fetchCfgDotFile(txHash)

    // Parse metadata from DOT labels and simplify node labels to ID-only
    const { simplifiedDot, metadataMap } = processDotContent(dot)
    nodeMetadataMap.value = metadataMap
    dotContent.value = simplifiedDot
    status.value = 'success'

    // Fetch block instructions
    try {
      const instructions = await fetchBlockInstructions(txHash)
      blockInstructions.value = instructions
    } catch (e) {
      console.warn('Failed to load block instructions:', e)
      // Don't fail the whole load if instructions fail
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

  // 保存初始fit变换(d3-graphviz渲染后的变换状态)
  try {
    initialTransform = zoomTransform(svg as Element)
  } catch {
    initialTransform = zoomIdentity
  }

  // 构建 DOT节点名 -> SVG元素 的映射
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

    // Add click handler for selecting block
    nodeEl.addEventListener('click', (e) => {
      e.stopPropagation()
      handleNodeClick(title || '')
    })
  })

  // 解析边连接关系
  parseEdgeConnections()
}

function handleNodeClick(nodeName: string) {
  // Extract block ID from node name (format: "node_X")
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
    selectedNodeMetadata.value = null
    return
  }

  // Set selected block and add highlight
  selectedBlockId.value = blockId
  selectedNodeMetadata.value = nodeMetadataMap.value.get(blockId) || null
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
  // Parse instruction format from "('0x2b', 'DUP1')" to "0x2b  DUP1"
  const match = instr.match(/\('([^']+)',\s*'([^']+)'\)/)
  if (match) {
    return `${match[1]}  ${match[2]}`
  }
  return instr
}

function processDotContent(dot: string): { simplifiedDot: string, metadataMap: Map<number, NodeMetadata> } {
  const metadataMap = new Map<number, NodeMetadata>()

  const simplifiedDot = dot.replace(
    /^(\s*node_(\d+)\s*\[)([^\]]*)\]/gm,
    (fullMatch, prefix: string, idStr: string, attrs: string) => {
      const nodeId = parseInt(idStr, 10)

      const shapeMatch = attrs.match(/shape="(record|ellipse)"/)
      const shape = (shapeMatch ? shapeMatch[1] : 'record') as 'record' | 'ellipse'

      const labelMatch = attrs.match(/label="((?:[^"\\]|\\.)*)"/)
      if (!labelMatch) return fullMatch
      const rawLabel = labelMatch[1]

      const metadata = shape === 'record'
        ? parseRecordLabel(nodeId, rawLabel)
        : parseEllipseLabel(nodeId, rawLabel)

      metadataMap.set(nodeId, metadata)

      const newLabel = shape === 'record' ? `{{${nodeId}}}` : `${nodeId}`
      const newAttrs = attrs
        .replace(/label="(?:[^"\\]|\\.)*"/, `label="${newLabel}"`)

      return `${prefix}${newAttrs}]`
    }
  )

  return { simplifiedDot, metadataMap }
}

function parseRecordLabel(nodeId: number, rawLabel: string): NodeMetadata {
  // Strip outer {{ and }}
  const inner = rawLabel.replace(/^\{\{/, '').replace(/\}\}$/, '')
  const fields = inner.split(/\s*\|\s*/)

  const contractName = fields[1]?.trim() || 'Unknown'
  const blocksMatch = fields[2]?.match(/Blocks:\s*(\d+)/)
  const blocks = blocksMatch ? parseInt(blocksMatch[1]) : 1
  const startPCMatch = fields[3]?.match(/StartPC:\s*(\S+)/)
  const startPC = startPCMatch ? startPCMatch[1] : '0x0'
  const endPCMatch = fields[4]?.match(/EndPC:\s*(\S+)/)
  const endPC = endPCMatch ? endPCMatch[1] : '0x0'
  const gasMatch = fields[5]?.match(/Gas:\s*(\S+)/)
  const gas = gasMatch ? gasMatch[1] : '0'

  const actions: string[] = []
  for (let i = 6; i < fields.length; i++) {
    const actionStr = fields[i].trim()
    if (actionStr) {
      const parts = actionStr.split(/\\n/)
      for (const part of parts) {
        const trimmed = part.trim()
        if (trimmed) actions.push(trimmed)
      }
    }
  }

  return { id: nodeId, contractName, blocks, startPC, endPC, gas, actions, shape: 'record' }
}

function parseEllipseLabel(nodeId: number, rawLabel: string): NodeMetadata {
  const lines = rawLabel.split(/\\n/).map(l => l.trim())

  const contractName = lines[1] || 'Unknown'
  const blocksMatch = lines[2]?.match(/Blocks:\s*(\d+)/)
  const blocks = blocksMatch ? parseInt(blocksMatch[1]) : 1

  const pcMatch = lines[3]?.match(/StartPC:\s*(\S+)\s*\\\|\s*EndPC:\s*(\S+)/)
  const startPC = pcMatch ? pcMatch[1] : '0x0'
  const endPC = pcMatch ? pcMatch[2] : '0x0'

  const gasMatch = lines[4]?.match(/Gas:\s*(\S+)/)
  const gas = gasMatch ? gasMatch[1] : '0'

  const actions: string[] = []
  for (let i = 5; i < lines.length; i++) {
    const trimmed = lines[i].trim()
    if (trimmed) actions.push(trimmed)
  }

  return { id: nodeId, contractName, blocks, startPC, endPC, gas, actions, shape: 'ellipse' }
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

    // 解析 "node_X->node_Y" 格式
    const match = title.match(/^(node_\d+)->(node_\d+)$/)
    if (match) {
      edgeConnections.value.set(title, {
        source: match[1],
        target: match[2]
      })
    }
  })
}

function calculateVisibleElements(targetBlockIds: number[]) {
  const targetNodeIds = targetBlockIds.map(id => `node_${id}`)
  const visible = new Set<string>(targetNodeIds)

  // 找出所有直接相连的节点
  edgeConnections.value.forEach(({source, target}) => {
    if (targetNodeIds.includes(source)) {
      visible.add(target)
    }
    if (targetNodeIds.includes(target)) {
      visible.add(source)
    }
  })

  visibleNodes.value = visible

  // 计算可见的边(两端节点都可见)
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

  // 处理节点 — 通过title匹配DOT节点名
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

  // 处理边
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

function calculateVisibleBBox(): DOMRect | null {
  if (!graphContainer.value || visibleNodes.value.size === 0) return null

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return null

  let minX = Infinity, minY = Infinity
  let maxX = -Infinity, maxY = -Infinity

  visibleNodes.value.forEach(nodeName => {
    const node = nodeNameToEl.value.get(nodeName)
    if (node) {
      const bbox = (node as SVGGraphicsElement).getBBox()
      minX = Math.min(minX, bbox.x)
      minY = Math.min(minY, bbox.y)
      maxX = Math.max(maxX, bbox.x + bbox.width)
      maxY = Math.max(maxY, bbox.y + bbox.height)
    }
  })

  if (minX === Infinity) return null

  return new DOMRect(minX, minY, maxX - minX, maxY - minY)
}

function zoomToVisibleNodes() {
  const bbox = calculateVisibleBBox()
  if (!bbox || !graphContainer.value) return

  const container = graphContainer.value
  const containerWidth = container.clientWidth
  const containerHeight = container.clientHeight

  if (bbox.width === 0 || bbox.height === 0) return

  console.log('Zoom to visible nodes:', {
    bbox: { x: bbox.x, y: bbox.y, width: bbox.width, height: bbox.height },
    container: { width: containerWidth, height: containerHeight }
  })

  // 计算缩放比例(留20%边距)
  const padding = 0.2
  const scaleX = (containerWidth * (1 - padding)) / bbox.width
  const scaleY = (containerHeight * (1 - padding)) / bbox.height
  const scale = Math.min(scaleX, scaleY, 3)

  // 计算bbox中心点在SVG坐标系中的位置
  const bboxCenterX = bbox.x + bbox.width / 2
  const bboxCenterY = bbox.y + bbox.height / 2

  // d3-zoom transform: translate(x, y) then scale(k)
  // To center bbox: we want (bboxCenter * k + translate) = containerCenter
  // So: translate = containerCenter - bboxCenter * k
  const translateX = containerWidth / 2 - bboxCenterX * scale
  const translateY = containerHeight / 2 - bboxCenterY * scale

  console.log('Transform calculation:', {
    scale,
    bboxCenter: { x: bboxCenterX, y: bboxCenterY },
    translate: { x: translateX, y: translateY }
  })

  // d3-zoom transform 的数学模型:
  // 屏幕坐标 = SVG坐标 * k + [x, y]
  // 我们要让 bboxCenter * k + [x, y] = containerCenter
  // 所以 [x, y] = containerCenter - bboxCenter * k
  //
  // 但是 d3-zoom 的 API 是:
  // zoomIdentity.translate(tx, ty).scale(k)
  // 这会产生: k=k, x=tx*k, y=ty*k (translate 会被 scale 影响!)
  //
  // 使用 d3 ZoomTransform 实例（而非普通对象），确保后续缩放交互正常
  const transform = zoomIdentity.translate(translateX, translateY).scale(scale)

  applyZoomTransform(transform)
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
    // fallback: 直接操作SVG transform
    try {
      const svgSel = select(svg)
      const zoomBeh = (svgSel.node() as any).__zoom_behavior
      if (zoomBeh) {
        svgSel.transition().duration(300).call(zoomBeh.transform, transform)
      } else {
        // 最终fallback
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
  if (newBlockIds && newBlockIds.length > 0) {
    calculateVisibleElements(newBlockIds)
    applyFilter()
    nextTick(() => {
      zoomToVisibleNodes()
    })
  } else {
    // 重置过滤
    visibleNodes.value.clear()
    visibleEdges.value.clear()
    highlightedNodes.value.clear()
    applyFilter()
    // 重置缩放到fit
    nextTick(() => {
      resetZoom()
    })
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
      (d) Control Flow Graph
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

    <button
      v-if="visibleNodes.size > 0"
      class="reset-button"
      @click="resetFilter"
    >
      Show All
    </button>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading control flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="cfg-container">
      <div ref="graphContainer" class="graph-viewport"></div>

      <!-- Right Side Panel: Information + Instructions -->
      <div class="side-panel">
        <!-- Upper: Information Panel -->
        <div class="info-panel">
          <div class="panel-section-header">Information</div>
          <div class="info-content">
            <template v-if="selectedNodeMetadata">
              <div class="info-row">
                <span class="info-label">ID</span>
                <span class="info-value">{{ selectedNodeMetadata.id }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Contract</span>
                <span class="info-value">{{ selectedNodeMetadata.contractName }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Blocks</span>
                <span class="info-value">{{ selectedNodeMetadata.blocks }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">StartPC</span>
                <span class="info-value">{{ selectedNodeMetadata.startPC }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">EndPC</span>
                <span class="info-value">{{ selectedNodeMetadata.endPC }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Gas</span>
                <span class="info-value">{{ selectedNodeMetadata.gas }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">Type</span>
                <span class="info-value">{{ selectedNodeMetadata.shape === 'ellipse' ? 'ERC20' : 'Contract' }}</span>
              </div>
              <div v-if="selectedNodeMetadata.actions.length > 0" class="info-actions">
                <div class="info-label">Actions</div>
                <div v-for="(action, idx) in selectedNodeMetadata.actions" :key="idx" class="action-line">
                  {{ action }}
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
            <div
              v-for="(block, blockId) in blockInstructions"
              :key="blockId"
              :id="`block-${blockId}`"
              class="block-section"
              :class="{ 'selected': selectedBlockId === parseInt(blockId) }"
            >
              <div class="block-header">Block {{ blockId }}</div>
              <div class="block-instructions">
                <div
                  v-for="(instr, idx) in block.instructions"
                  :key="idx"
                  class="instruction-line"
                >
                  {{ formatInstruction(instr) }}
                </div>
              </div>
            </div>
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
  display: none;
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
  top: 8px;
  right: 12px;
  padding: 4px 12px;
  font-size: 11px;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 4px;
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
