<script setup lang="ts">
import { ref, onMounted, watch, nextTick } from 'vue'
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
  if (newHash) {
    loadCfgData(newHash)
  }
}, { immediate: true })

async function loadCfgData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''

  try {
    const dot = await fetchCfgDotFile(txHash)

    dotContent.value = dot
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
    return
  }

  // Set selected block and add highlight
  selectedBlockId.value = blockId
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
  // 正确的做法是直接构造 transform 对象:
  const transform = {
    k: scale,
    x: translateX,
    y: translateY
  }

  console.log('Final transform:', transform)

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

onMounted(() => {
  if (props.txHash) {
    loadCfgData(props.txHash)
  }
})
</script>

<template>
  <div class="cfg-panel">
    <span class="panel-label">(d) Control Flow Graph</span>

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

      <!-- Fixed Instructions Panel -->
      <div ref="instructionsPanel" class="instructions-panel">
        <div class="instructions-header">Instructions</div>
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
  height: 100%;
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

/* Fixed Instructions Panel */
.instructions-panel {
  width: 140px;
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

.instructions-header {
  padding: 4px 8px;
  font-size: 9px;
  color: var(--muted);
  font-weight: 500;
  letter-spacing: 0.3px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
  flex-shrink: 0;
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
</style>
