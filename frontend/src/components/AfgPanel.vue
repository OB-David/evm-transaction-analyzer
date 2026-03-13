<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { graphviz } from 'd3-graphviz'
import { fetchDotFile, fetchEdgeLink, type EdgeLink } from '../api/analyze'
import LegendPanel from './LegendPanel.vue'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: number[] | null
  isAnalyzing: boolean
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: number[] | null]
}>()

const dotContent = ref<string>('')
const edgeLinks = ref<EdgeLink[]>([])
const selectedEdgeId = ref<number | null>(null)
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')

const graphContainer = ref<HTMLElement | null>(null)
let graphvizInstance: any = null

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

  try {
    const [dot, links] = await Promise.all([
      fetchDotFile(txHash),
      fetchEdgeLink(txHash)
    ])

    dotContent.value = dot
    edgeLinks.value = links
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

  const nodes = svg.querySelectorAll('.node')
  nodes.forEach((node) => {
    const nodeEl = node as SVGElement

    nodeEl.addEventListener('mouseenter', () => {
      nodeEl.classList.add('hovered')
    })

    nodeEl.addEventListener('mouseleave', () => {
      nodeEl.classList.remove('hovered')
    })
  })

  const edges = svg.querySelectorAll('.edge')
  edges.forEach((edge) => {
    const edgeEl = edge as SVGElement

    // 从text元素中提取边ID (格式: "(1) ETH: ...")
    // graphviz SVG中,<title>是"source->target",标签文字在<text>中
    const textEls = edge.querySelectorAll('text')
    let edgeId: number | null = null
    textEls.forEach(t => {
      if (edgeId !== null) return
      const m = (t.textContent || '').match(/\((\d+)\)/)
      if (m) edgeId = parseInt(m[1])
    })

    if (edgeId !== null) {
      const capturedId = edgeId

      // 标记为可交互边,统一设置pointer光标
      edgeEl.classList.add('interactive')

      // 为每条边的path创建一个透明的宽stroke覆盖层,扩大可点击区域
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
        hitArea.addEventListener('mouseenter', () => {
          edgeEl.classList.add('hovered')
        })
        hitArea.addEventListener('mouseleave', () => {
          edgeEl.classList.remove('hovered')
        })
        path.parentNode?.insertBefore(hitArea, path)
      })

      // polygon(箭头)也可点击
      const polygons = edge.querySelectorAll('polygon')
      polygons.forEach(el => {
        el.addEventListener('click', (e) => {
          e.stopPropagation()
          handleEdgeClick(capturedId)
        })
        el.addEventListener('mouseenter', () => {
          edgeEl.classList.add('hovered')
        })
        el.addEventListener('mouseleave', () => {
          edgeEl.classList.remove('hovered')
        })
      })
    }
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

  const blockIds: number[] = []

  if (typeof link.matched_blocks === 'number') {
    blockIds.push(link.matched_blocks)
  } else if (Array.isArray(link.matched_blocks)) {
    blockIds.push(...link.matched_blocks)
  } else if (typeof link.matched_blocks === 'object') {
    if (link.matched_blocks.sender) {
      blockIds.push(...link.matched_blocks.sender)
    }
    if (link.matched_blocks.receiver) {
      blockIds.push(...link.matched_blocks.receiver)
    }
  }

  // 去重
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

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading asset flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="afg-container">
      <div ref="graphContainer" class="graph-viewport"></div>
      <LegendPanel :tx-hash="props.txHash" />
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

/* 可交互边: 整个edge组统一pointer光标 */
.graph-viewport :deep(.edge.interactive) {
  cursor: pointer;
}

/* 边的text不接收鼠标事件 */
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

/* hitArea不受任何样式影响,保持稳定的18px透明stroke */
.graph-viewport :deep(.hit-area) {
  stroke: transparent !important;
  stroke-width: 18px !important;
  fill: none !important;
  filter: none !important;
  cursor: pointer;
}

/* hover只影响原始path和polygon,排除hitArea */
.graph-viewport :deep(.edge.hovered path:not(.hit-area)),
.graph-viewport :deep(.edge.hovered polygon) {
  stroke-width: 3;
  filter: brightness(1.2);
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
</style>
