<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom'
import { select } from 'd3-selection'
import {
  type BlockId,
  fetchCfgViewData,
  fetchLegendData,
  type BlockAction,
  type BlockInformation,
  type BlockInformationMap,
  type CfgMode,
  type CfgViewBundle,
  type EdgeStepMap,
} from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: BlockId[] | null
  filteredEdgeIds: string[] | null
  isAnalyzing: boolean
  edgeStepMap: EdgeStepMap | null
  preferredMode: CfgMode
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: BlockId[] | null]
  'mode-change': [mode: CfgMode]
}>()

type CfgEdgeType = 'NORMAL' | 'JUMP' | 'CALL' | 'DELEGATECALL' | 'TERMINATE'

const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const cfgMode = ref<CfgMode>('folded')
const cfgViews = ref<CfgViewBundle | null>(null)
const blockInformation = ref<BlockInformationMap>({})
const selectedNodeName = ref<string | null>(null)
const selectedBlockInfo = ref<BlockInformation | null>(null)
const graphContainer = ref<HTMLElement | null>(null)
const addressNameMap = ref<Map<string, string>>(new Map())
const highlightedNodes = ref<Set<string>>(new Set())
const visibleNodes = ref<Set<string>>(new Set())
const visibleEdges = ref<Set<string>>(new Set())
const edgeConnections = ref<Map<string, { source: string, target: string }>>(new Map())
const nodeNameToEl = ref<Map<string, Element>>(new Map())
const edgeIdToSvgTitle = ref<Map<string, string>>(new Map())
const activeEdgeType = ref<CfgEdgeType | null>(null)
const showEdgeTypes = ref(false)
const PLAIN_SCALE_MULTIPLIER = 1.35
const PLAIN_MIN_WIDTH_RATIO = 1.15
const PLAIN_VIEW_PADDING = 20

let zoomBehavior: ZoomBehavior<SVGSVGElement, unknown> | null = null
let resizeObserver: ResizeObserver | null = null
let edgeTooltipHideTimer: number | null = null

const cfgModeBadge = computed(() => cfgMode.value === 'folded' ? 'Folded CFG' : 'Plain CFG')
const toggleButtonLabel = computed(() => cfgMode.value === 'folded' ? 'Plain CFG' : 'Folded CFG')
const hasSelection = computed(() => Boolean(selectedBlockInfo.value))

watch(() => props.txHash, (newHash) => {
  resetSelection()
  if (newHash) {
    loadCfgData(newHash)
  } else {
    status.value = 'idle'
    cfgViews.value = null
    blockInformation.value = {}
    clearFilterState()
    if (graphContainer.value) {
      graphContainer.value.innerHTML = ''
    }
  }
}, { immediate: true })

watch(() => props.highlightedBlockId, (newBlockIds) => {
  if (cfgMode.value !== 'folded') {
    clearFilterState()
    applyFilter()
    return
  }

  if (props.filteredEdgeIds && props.filteredEdgeIds.length > 0) return

  if (newBlockIds && newBlockIds.length > 0) {
    calculateVisibleElements(newBlockIds)
    applyFilter()
  } else {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
  }
})

watch(() => props.edgeStepMap, () => {
  buildEdgeIdToTitleMap()
  if (status.value === 'success') {
    syncFilterStateWithProps()
  }
})

watch(graphContainer, (container, oldContainer) => {
  if (oldContainer && resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }

  if (!container || typeof ResizeObserver === 'undefined') return

  resizeObserver = new ResizeObserver(() => {
    window.requestAnimationFrame(() => fitGraphToViewport())
  })
  resizeObserver.observe(container)
})

watch(hasSelection, () => {
  nextTick(() => fitGraphToViewport({ animate: true }))
})

watch(() => props.filteredEdgeIds, (edgeIds) => {
  if (cfgMode.value !== 'folded') {
    clearFilterState()
    applyFilter()
    return
  }

  if (edgeIds && edgeIds.length > 0) {
    applyEdgeFilter(edgeIds)
  } else if (!props.highlightedBlockId || props.highlightedBlockId.length === 0) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
  }
})

watch(() => props.preferredMode, (mode) => {
  if (status.value !== 'success' || mode === cfgMode.value) return
  if (!getCfgView(mode)) return
  void switchCfgMode(mode)
})

async function loadCfgData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''
  activeEdgeType.value = null
  cfgViews.value = null
  blockInformation.value = {}
  clearFilterState()
  resetSelection()

  try {
    const [cfgViewBundle, legend] = await Promise.all([
      fetchCfgViewData(txHash, props.preferredMode),
      fetchLegendData(txHash),
    ])

    cfgViews.value = cfgViewBundle

    addressNameMap.value.clear()
    for (const entry of [...legend.user_addresses, ...legend.erc20_tokens, ...legend.normal_contracts]) {
      addressNameMap.value.set(entry.address.toLowerCase(), entry.name)
    }

    status.value = 'success'
    await switchCfgMode(cfgViewBundle.initialMode)
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load CFG data'
  }
}

function getCfgView(mode: CfgMode) {
  if (!cfgViews.value) return null
  return mode === 'plain' ? cfgViews.value.plain : cfgViews.value.folded
}

async function switchCfgMode(mode: CfgMode) {
  const nextView = getCfgView(mode)
  if (!nextView) return

  cfgMode.value = mode
  blockInformation.value = nextView.blockInformation
  resetSelection()
  emit('mode-change', mode)

  await nextTick()
  renderSvg(nextView.svgContent)
}

function toggleCfgMode() {
  const nextMode: CfgMode = cfgMode.value === 'folded' ? 'plain' : 'folded'
  if (!getCfgView(nextMode)) return
  void switchCfgMode(nextMode)
}

function renderSvg(svgContent: string) {
  if (!graphContainer.value) return

  const container = graphContainer.value
  container.innerHTML = svgContent

  const svg = container.querySelector('svg')
  if (!svg) return

  svg.removeAttribute('width')
  svg.removeAttribute('height')
  svg.style.display = 'block'
  svg.style.width = '100%'
  svg.style.height = '100%'
  svg.style.maxWidth = 'none'

  const svgSel = select(svg as SVGSVGElement)
  const origG = svg.querySelector('g')
  if (!origG) return

  const zoomG = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  zoomG.setAttribute('id', 'zoom-layer')
  svg.insertBefore(zoomG, origG)
  zoomG.appendChild(origG)

  if (cfgMode.value === 'folded') {
    setFoldedViewBox(svg as SVGSVGElement, origG as SVGGElement)
    normalizeFoldedTextScale(svg as SVGSVGElement)
  } else {
    configurePlainViewport(svg as SVGSVGElement, origG as SVGGElement)
  }

  zoomBehavior = zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.005, 10])
    .on('zoom', (event: any) => {
      zoomG.setAttribute('transform', event.transform.toString())
    })

  svgSel.call(zoomBehavior)

  attachInteractivity()
  buildEdgeIdToTitleMap()
  syncFilterStateWithProps()
  if (cfgMode.value === 'folded') {
    nextTick(() => resetZoom())
  } else {
    nextTick(() => fitGraphToViewport())
  }
}

function setFoldedViewBox(svg: SVGSVGElement, graphContent: SVGGElement) {
  const bounds = getGraphContentBounds(graphContent)
  if (bounds.width <= 0 || bounds.height <= 0) return

  const padding = 20
  svg.setAttribute(
    'viewBox',
    `${bounds.x - padding} ${bounds.y - padding} ${bounds.width + padding * 2} ${bounds.height + padding * 2}`,
  )
  svg.setAttribute('preserveAspectRatio', 'none')
}

function normalizeFoldedTextScale(svg: SVGSVGElement) {
  const viewBox = svg.viewBox.baseVal
  const viewportWidth = svg.clientWidth
  const viewportHeight = svg.clientHeight
  if (viewBox.width <= 0 || viewBox.height <= 0 || viewportWidth <= 0 || viewportHeight <= 0) return

  const scaleX = viewportWidth / viewBox.width
  const scaleY = viewportHeight / viewBox.height
  if (scaleX <= 0 || scaleY <= 0) return

  const correctionX = scaleY / scaleX
  svg.querySelectorAll<SVGTextElement>('text').forEach((textEl) => {
    const originalTransform = textEl.dataset.originalTransform ?? textEl.getAttribute('transform') ?? ''
    textEl.dataset.originalTransform = originalTransform

    if (Math.abs(correctionX - 1) < 0.001) {
      if (originalTransform) {
        textEl.setAttribute('transform', originalTransform)
      } else {
        textEl.removeAttribute('transform')
      }
      return
    }

    const x = Number.parseFloat(textEl.getAttribute('x') || '')
    const y = Number.parseFloat(textEl.getAttribute('y') || '')
    if (!Number.isFinite(x) || !Number.isFinite(y)) return

    const compensation = `translate(${x} ${y}) scale(${correctionX} 1) translate(${-x} ${-y})`
    textEl.setAttribute('transform', originalTransform ? `${originalTransform} ${compensation}` : compensation)
  })
}

function configurePlainViewport(svg: SVGSVGElement, graphContent: SVGGElement) {
  if (!graphContainer.value) return

  const bounds = getGraphContentBounds(graphContent)
  const containerWidth = graphContainer.value.clientWidth
  const containerHeight = graphContainer.value.clientHeight
  if (bounds.width <= 0 || bounds.height <= 0 || containerWidth <= 0 || containerHeight <= 0) return

  const paddedWidth = bounds.width + PLAIN_VIEW_PADDING * 2
  const paddedHeight = bounds.height + PLAIN_VIEW_PADDING * 2
  svg.setAttribute(
    'viewBox',
    `${bounds.x - PLAIN_VIEW_PADDING} ${bounds.y - PLAIN_VIEW_PADDING} ${paddedWidth} ${paddedHeight}`,
  )
  svg.setAttribute('preserveAspectRatio', 'xMinYMid meet')

  const widthFromHeight = containerHeight * (paddedWidth / paddedHeight)
  const enlargedWidth = widthFromHeight * PLAIN_SCALE_MULTIPLIER
  const minimumWidth = containerWidth * PLAIN_MIN_WIDTH_RATIO
  const targetWidth = Math.max(enlargedWidth, minimumWidth)

  svg.style.width = `${Math.ceil(targetWidth)}px`
  svg.style.height = '100%'
}

function attachInteractivity() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  prepareSvgMetadata(svg)
  nodeNameToEl.value.clear()

  svg.addEventListener('click', (event) => {
    const target = event.target as Element | null
    if (!target?.closest('.node') && !target?.closest('.edge')) {
      resetSelection()
    }
  })

  svg.querySelectorAll('.node').forEach((node) => {
    const nodeEl = node as SVGElement
    const nodeName = getNodeName(node)
    if (nodeName) {
      nodeNameToEl.value.set(nodeName, node)
    }

    nodeEl.addEventListener('mouseenter', () => nodeEl.classList.add('hovered'))
    nodeEl.addEventListener('mouseleave', () => nodeEl.classList.remove('hovered'))
    nodeEl.addEventListener('click', (e) => {
      e.stopPropagation()
      handleNodeClick(nodeName)
    })
  })

  parseEdgeConnections()
}

function prepareSvgMetadata(svg: SVGSVGElement) {
  const edgeColorMap = new Map<string, CfgEdgeType>(
    edgeTypes.map(edge => [edge.color.toLowerCase(), edge.type]),
  )

  svg.querySelectorAll('.node').forEach((node) => {
    const titleEl = node.querySelector('title')
    const nodeName = titleEl?.textContent?.trim() || ''
    if (nodeName) {
      ;(node as HTMLElement).dataset.nodeName = nodeName
    }
    titleEl?.remove()
  })

  svg.querySelectorAll('.cluster').forEach((cluster) => {
    cluster.querySelector('title')?.remove()
  })

  svg.querySelectorAll('.edge').forEach((edge) => {
    const titleEl = edge.querySelector('title')
    const edgeTitle = titleEl?.textContent?.trim() || ''
    if (edgeTitle) {
      ;(edge as HTMLElement).dataset.edgeTitle = edgeTitle
    }
    titleEl?.remove()

    const colorCandidates = Array.from(edge.querySelectorAll<SVGElement>('[stroke], [fill]'))
      .flatMap((el) => [el.getAttribute('stroke'), el.getAttribute('fill')])
      .map((value) => normalizeColor(value))
      .filter(Boolean)

    const matchedType = colorCandidates
      .map((color) => edgeColorMap.get(color))
      .find((type): type is CfgEdgeType => Boolean(type))

    if (matchedType) {
      ;(edge as HTMLElement).dataset.edgeType = matchedType
    }
  })

  svg.querySelectorAll('title').forEach((titleEl) => titleEl.remove())
}

function getNodeName(node: Element): string {
  return (node as HTMLElement).dataset.nodeName || ''
}

function getEdgeTitle(edge: Element): string {
  return (edge as HTMLElement).dataset.edgeTitle || edge.querySelector('title')?.textContent || ''
}

function getEdgeType(edge: Element): CfgEdgeType | null {
  return ((edge as HTMLElement).dataset.edgeType as CfgEdgeType | undefined) || null
}

function normalizeColor(color: string | null): string {
  return (color || '').trim().toLowerCase()
}

function parseEdgeConnections() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  edgeConnections.value.clear()
  svg.querySelectorAll('.edge').forEach((edge) => {
    const title = getEdgeTitle(edge)
    if (!title) return

    const match = title.match(/^([A-Za-z0-9_]+)->([A-Za-z0-9_]+)$/)
    if (match) {
      edgeConnections.value.set(title, {
        source: match[1]!,
        target: match[2]!,
      })
    }
  })
}

function buildEdgeIdToTitleMap() {
  edgeIdToSvgTitle.value.clear()
  if (!props.edgeStepMap) return

  for (const [edgeId, entry] of Object.entries(props.edgeStepMap)) {
    edgeIdToSvgTitle.value.set(edgeId, `${entry.source_node}->${entry.target_node}`)
  }
}

function syncFilterStateWithProps() {
  if (cfgMode.value !== 'folded') {
    clearFilterState()
    applyFilter()
    return
  }

  if (props.filteredEdgeIds && props.filteredEdgeIds.length > 0) {
    applyEdgeFilter(props.filteredEdgeIds)
    return
  }

  if (props.highlightedBlockId && props.highlightedBlockId.length > 0) {
    calculateVisibleElements(props.highlightedBlockId)
    applyFilter()
    return
  }

  clearFilterState()
  applyFilter()
}

function extractBlockIdFromNodeName(nodeName: string): string | null {
  if (!nodeName.startsWith('node_')) return null
  const rawId = nodeName.slice(5)
  return rawId ? rawId : null
}

function handleNodeClick(nodeName: string) {
  if (!nodeName) return

  if (selectedNodeName.value) {
    nodeNameToEl.value.get(selectedNodeName.value)?.classList.remove('selected')
  }

  if (selectedNodeName.value === nodeName) {
    resetSelection()
    return
  }

  selectedNodeName.value = nodeName
  nodeNameToEl.value.get(nodeName)?.classList.add('selected')

  const blockId = extractBlockIdFromNodeName(nodeName)
  selectedBlockInfo.value = blockId !== null ? blockInformation.value[String(blockId)] || null : null

  nextTick(() => {
    const targetId = selectedBlockInfo.value ? `block-${selectedBlockInfo.value.block_id}` : null
    if (!targetId) return
    document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

function resetSelection() {
  if (selectedNodeName.value) {
    nodeNameToEl.value.get(selectedNodeName.value)?.classList.remove('selected')
  }
  selectedNodeName.value = null
  selectedBlockInfo.value = null
}

function fitGraphToViewport(options: { animate?: boolean } = {}) {
  if (!graphContainer.value || !zoomBehavior) return

  const svg = graphContainer.value.querySelector('svg') as SVGSVGElement | null
  const zoomLayer = svg?.querySelector('#zoom-layer') as SVGGElement | null
  const graphContent = zoomLayer?.firstElementChild as SVGGElement | null
  if (!svg || !zoomLayer || !graphContent) return

  if (cfgMode.value === 'folded') {
    setFoldedViewBox(svg, graphContent)
    normalizeFoldedTextScale(svg)
    const svgSelection = select(svg)

    if (options.animate) {
      svgSelection
        .transition()
        .duration(240)
        .call(zoomBehavior.transform, zoomIdentity)
      return
    }

    svgSelection.call(zoomBehavior.transform, zoomIdentity)
    return
  }

  configurePlainViewport(svg, graphContent)
  const svgSelection = select(svg)

  svgSelection.call(zoomBehavior.transform, zoomIdentity)
}

function getGraphContentBounds(graphContent: SVGGElement) {
  const bbox = graphContent.getBBox()
  const baseMatrix = graphContent.transform.baseVal.consolidate()?.matrix

  if (!baseMatrix) {
    return bbox
  }

  const corners = [
    { x: bbox.x, y: bbox.y },
    { x: bbox.x + bbox.width, y: bbox.y },
    { x: bbox.x, y: bbox.y + bbox.height },
    { x: bbox.x + bbox.width, y: bbox.y + bbox.height },
  ].map(({ x, y }) => ({
    x: baseMatrix.a * x + baseMatrix.c * y + baseMatrix.e,
    y: baseMatrix.b * x + baseMatrix.d * y + baseMatrix.f,
  }))

  const xs = corners.map((point) => point.x)
  const ys = corners.map((point) => point.y)
  const minX = Math.min(...xs)
  const maxX = Math.max(...xs)
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys)

  return {
    x: minX,
    y: minY,
    width: maxX - minX,
    height: maxY - minY,
  }
}

function applyEdgeFilter(edgeIds: string[]) {
  const matchedEdgeTitles = new Set<string>()
  const matchedNodes = new Set<string>()

  for (const edgeId of edgeIds) {
    const svgTitle = edgeIdToSvgTitle.value.get(edgeId)
    const entry = props.edgeStepMap?.[edgeId]
    if (entry) {
      matchedNodes.add(entry.source_node)
      matchedNodes.add(entry.target_node)
    }
    if (svgTitle) {
      matchedEdgeTitles.add(svgTitle)
      const conn = edgeConnections.value.get(svgTitle)
      if (conn) {
        matchedNodes.add(conn.source)
        matchedNodes.add(conn.target)
      }
    }
  }

  if (matchedNodes.size === 0) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
    return
  }

  if (matchedEdgeTitles.size === 0) {
    edgeConnections.value.forEach(({ source, target }, edgeTitle) => {
      if (matchedNodes.has(source) && matchedNodes.has(target)) {
        matchedEdgeTitles.add(edgeTitle)
      }
    })

    if (matchedEdgeTitles.size === 0) {
      clearFilterState()
      applyFilter()
      return
    }
  }

  visibleNodes.value = matchedNodes
  visibleEdges.value = matchedEdgeTitles
  highlightedNodes.value = new Set(matchedNodes)
  applyFilter()
}

function calculateVisibleElements(targetBlockIds: BlockId[]) {
  const targetNodeSet = new Set<string>()
  targetBlockIds.forEach((id) => targetNodeSet.add(`node_${String(id)}`))

  const visible = new Set<string>(targetNodeSet)
  edgeConnections.value.forEach(({ source, target }) => {
    if (targetNodeSet.has(source)) visible.add(target)
    if (targetNodeSet.has(target)) visible.add(source)
  })

  visibleNodes.value = visible
  highlightedNodes.value = new Set(targetNodeSet)

  const visibleEdgeSet = new Set<string>()
  edgeConnections.value.forEach(({ source, target }, edgeId) => {
    if (visible.has(source) && visible.has(target)) {
      visibleEdgeSet.add(edgeId)
    }
  })
  visibleEdges.value = visibleEdgeSet
}

function clearFilterState() {
  visibleNodes.value.clear()
  visibleEdges.value.clear()
  highlightedNodes.value.clear()
}

function applyFilter() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  svg.querySelectorAll('.node').forEach((node) => {
    const nodeName = getNodeName(node)
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

  svg.querySelectorAll('.edge').forEach((edge) => {
    const title = getEdgeTitle(edge)
    const matchesVisibleSelection =
      visibleNodes.value.size === 0 ||
      visibleEdges.value.size === 0 ||
      visibleEdges.value.has(title)
    const matchesEdgeType =
      activeEdgeType.value === null || getEdgeType(edge) === activeEdgeType.value

    if (matchesVisibleSelection && matchesEdgeType) {
      edge.classList.remove('filtered-out')
    } else {
      edge.classList.add('filtered-out')
    }
  })
}

function resetZoom() {
  fitGraphToViewport({ animate: true })
}

function resetFilter() {
  activeEdgeType.value = null
  emit('cfg-navigate', null)
  if (!props.filteredEdgeIds?.length && !props.highlightedBlockId?.length) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
  }
}

function toggleEdgeTypeFilter(edgeType: CfgEdgeType) {
  activeEdgeType.value = activeEdgeType.value === edgeType ? null : edgeType
  applyFilter()
}

function clearEdgeTypeFilter() {
  activeEdgeType.value = null
  applyFilter()
}

function openEdgeTypesTooltip() {
  if (edgeTooltipHideTimer !== null) {
    window.clearTimeout(edgeTooltipHideTimer)
    edgeTooltipHideTimer = null
  }
  showEdgeTypes.value = true
}

function queueCloseEdgeTypesTooltip() {
  if (edgeTooltipHideTimer !== null) {
    window.clearTimeout(edgeTooltipHideTimer)
  }
  edgeTooltipHideTimer = window.setTimeout(() => {
    showEdgeTypes.value = false
    edgeTooltipHideTimer = null
  }, 180)
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
    const ethWhole = wei / BigInt(1e14)
    const ethNum = Number(ethWhole) / 10000
    if (ethNum === 0 && wei > 0n) return '<0.0001 ETH'
    return `${ethNum} ETH`
  } catch {
    return hex
  }
}

function formatAction(action: BlockAction, index: number): string {
  const prefix = `Action${index + 1}`

  if (action.action_type === 'eth_transfer' && action.eth_event) {
    return `${prefix}: Send_ETH ${addrToName(action.eth_event.from)} -> ${addrToName(action.eth_event.to)} ${hexToEth(action.eth_event.amount)}`
  }

  if (action.erc20_events && action.erc20_events.length > 0) {
    return action.erc20_events.map((ev, i) => {
      const type = ev.type === 'read' ? 'Read' : 'Write'
      const token = ev.tokenname || 'ERC20'
      const user = addrToName(ev.user || '')
      return `${prefix}${action.erc20_events.length > 1 ? `.${i + 1}` : ''}: ${type}_${token} ${user} bal:${ev.balance}`
    }).join('\n')
  }

  return `${prefix}: ${action.action_type}`
}

function formatGas(gas: number | null | undefined): string {
  if (gas === null || gas === undefined) return 'Unknown'
  return Number(gas).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function normalizePcValue(pc: string | null | undefined): string {
  const text = String(pc ?? '').trim()
  return text ? text : 'Unknown'
}

function formatPcRange(startPc: string | null | undefined, endPc: string | null | undefined): string {
  return `${normalizePcValue(startPc)} - ${normalizePcValue(endPc)}`
}

const edgeTypes: Array<{ type: CfgEdgeType, color: string, desc: string }> = [
  { type: 'NORMAL', color: '#939393', desc: 'Non-terminating opcodes' },
  { type: 'JUMP', color: '#242424', desc: 'JUMP, JUMPI' },
  { type: 'CALL', color: '#1F6800', desc: 'CALL, CALLCODE, STATICCALL' },
  { type: 'DELEGATECALL', color: '#009DFF', desc: 'DELEGATECALL' },
  { type: 'TERMINATE', color: '#C14A00', desc: 'RETURN, STOP, REVERT, INVALID, SELFDESTRUCT' },
]

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (edgeTooltipHideTimer !== null) {
    window.clearTimeout(edgeTooltipHideTimer)
    edgeTooltipHideTimer = null
  }
})
</script>

<template>
  <div class="cfg-panel">
    <span class="panel-label">
      (e) Control Flow Graph
      <span class="mode-badge" :class="cfgMode">{{ cfgModeBadge }}</span>
      <button
        class="cfg-mode-toggle"
        @click="toggleCfgMode"
      >
        {{ toggleButtonLabel }}
      </button>
      <span
        class="edge-info-icon"
        :class="{ active: showEdgeTypes || !!activeEdgeType }"
        @mouseenter="openEdgeTypesTooltip"
        @mouseleave="queueCloseEdgeTypesTooltip"
      >
        <svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.2" />
          <text x="7" y="10.5" text-anchor="middle" font-size="9" font-weight="600" fill="currentColor">?</text>
        </svg>
        <div
          v-show="showEdgeTypes"
          class="edge-types-tooltip"
          @mouseenter="openEdgeTypesTooltip"
          @mouseleave="queueCloseEdgeTypesTooltip"
        >
          <div class="edge-tooltip-title">CFG Edge Types</div>
          <div
            v-for="edge in edgeTypes"
            :key="edge.type"
            class="edge-type-item"
            :class="{ active: activeEdgeType === edge.type }"
            @click.stop="toggleEdgeTypeFilter(edge.type)"
          >
            <svg width="32" height="12" viewBox="0 0 32 12">
              <line x1="2" y1="6" x2="24" y2="6" :stroke="edge.color" stroke-width="2.5" />
              <polygon :points="'24,3 30,6 24,9'" :fill="edge.color" />
            </svg>
            <div class="edge-type-text">
              <span class="edge-type-name">{{ edge.type }}</span>
              <span class="edge-type-desc">{{ edge.desc }}</span>
            </div>
          </div>
          <button
            v-if="activeEdgeType"
            class="edge-filter-reset"
            @click.stop="clearEdgeTypeFilter"
          >
            Show all edge types
          </button>
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
      <div class="graph-stage">
        <div
          ref="graphContainer"
          class="graph-viewport"
          :class="{ 'folded-viewport': cfgMode === 'folded', 'plain-viewport': cfgMode === 'plain' }"
        ></div>
        <button
          v-if="visibleNodes.size > 0 || activeEdgeType"
          class="reset-button"
          @click="resetFilter"
        >Show All</button>
      </div>

      <transition name="drawer">
        <div v-if="hasSelection" class="side-panel">
          <div class="panel-section-header">information</div>
          <div class="information-content">
            <template v-if="selectedBlockInfo">
              <div :id="`block-${selectedBlockInfo.block_id}`" class="information-stack">
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
                  <span class="info-label">PC</span>
                  <span class="info-value">{{ formatPcRange(selectedBlockInfo.start_pc, selectedBlockInfo.end_pc) }}</span>
                </div>
                <div class="info-row">
                  <span class="info-label">Gas</span>
                  <span class="info-value">{{ formatGas(selectedBlockInfo.gas) }}</span>
                </div>

                <div v-if="selectedBlockInfo.actions.length > 0" class="summary-group">
                  <div class="info-label">Actions</div>
                  <div v-for="(action, idx) in selectedBlockInfo.actions" :key="idx" class="summary-line mono">
                    {{ formatAction(action, idx) }}
                  </div>
                </div>

                <div class="summary-group">
                  <div class="info-label">Instructions</div>
                  <div class="block-section selected">
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
                </div>
              </div>
            </template>
          </div>
        </div>
      </transition>
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
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.5px;
  z-index: 10;
}

.mode-badge {
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 9px;
  letter-spacing: 0.3px;
  color: #475569;
  background: #e2e8f0;
}

.cfg-mode-toggle {
  border: 1px solid rgba(148, 163, 184, 0.45);
  background: rgba(255, 255, 255, 0.94);
  color: #334155;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 9px;
  letter-spacing: 0.3px;
  cursor: pointer;
  transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.cfg-mode-toggle:hover {
  background: #f8fafc;
  border-color: rgba(100, 116, 139, 0.55);
}

.cfg-container {
  position: relative;
  width: 100%;
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
  box-sizing: border-box;
}

.graph-stage {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}

.graph-viewport {
  flex: 1;
  overflow: hidden;
  min-width: 0;
  min-height: 0;
}

.graph-viewport.folded-viewport {
  box-sizing: border-box;
  padding-top: 34px;
}

.graph-viewport.plain-viewport {
  box-sizing: border-box;
  padding-top: 34px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-gutter: stable both-edges;
}

.graph-viewport.plain-viewport :deep(svg) {
  display: block;
  min-width: 100%;
  max-width: none;
}

.graph-viewport :deep(svg) {
  width: 100%;
  height: 100%;
}

.graph-viewport :deep(.node) {
  cursor: pointer;
}

.graph-viewport :deep(.node text) {
  cursor: pointer;
  user-select: none;
}

.graph-viewport :deep(.node.hovered ellipse),
.graph-viewport :deep(.node.hovered polygon),
.graph-viewport :deep(.node.hovered path),
.graph-viewport :deep(.node.hovered rect) {
  stroke-width: 2;
  filter: brightness(1.05);
}

.graph-viewport :deep(.node.filtered-out),
.graph-viewport :deep(.edge.filtered-out) {
  opacity: 0.08;
  pointer-events: none;
}

.graph-viewport :deep(.node.highlighted) {
  filter: drop-shadow(0 0 8px rgba(255, 0, 0, 0.55));
}

.graph-viewport :deep(.node.highlighted ellipse),
.graph-viewport :deep(.node.highlighted polygon),
.graph-viewport :deep(.node.highlighted path),
.graph-viewport :deep(.node.highlighted rect) {
  stroke: #ef4444;
  stroke-width: 3;
}

.graph-viewport :deep(.node.selected) {
  filter: drop-shadow(0 0 12px rgba(99, 102, 241, 0.9));
}

.graph-viewport :deep(.node.selected ellipse),
.graph-viewport :deep(.node.selected polygon),
.graph-viewport :deep(.node.selected path),
.graph-viewport :deep(.node.selected rect) {
  stroke: #4f46e5;
  stroke-width: 4;
}

.reset-button {
  position: absolute;
  top: 32px;
  right: 12px;
  padding: 4px 10px;
  font-size: 10px;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 999px;
  cursor: pointer;
  z-index: 10;
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

.side-panel {
  width: 324px;
  background: var(--bg);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  min-height: 0;
}

.drawer-enter-active,
.drawer-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}

.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
  transform: translateX(12px);
}

.panel-section-header {
  padding: 6px 10px;
  font-size: 9px;
  color: var(--muted);
  font-weight: 600;
  letter-spacing: 0.35px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
  flex-shrink: 0;
  text-transform: uppercase;
}

.information-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 10px;
  min-height: 0;
}

.information-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
  font-size: 10px;
}

.info-label {
  color: var(--muted);
  font-weight: 600;
  font-size: 10px;
  flex-shrink: 0;
}

.info-value {
  color: var(--text);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 10px;
  text-align: right;
  word-break: break-all;
}

.summary-group {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.summary-line {
  margin-top: 4px;
  font-size: 10px;
  line-height: 1.45;
  color: #475569;
}

.summary-line.mono,
.action-line,
.instruction-line {
  font-family: 'Consolas', 'Monaco', monospace;
}

.block-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.block-section.selected {
  padding: 10px;
  border: 1px solid rgba(79, 70, 229, 0.2);
  border-radius: 10px;
  background: rgba(99, 102, 241, 0.05);
}

.block-instructions {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.instruction-line {
  white-space: nowrap;
  color: #475569;
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.action-line {
  font-size: 9px;
  line-height: 1.35;
  color: #64748b;
}

.information-content::-webkit-scrollbar {
  width: 6px;
}

.information-content::-webkit-scrollbar-track {
  background: var(--bg);
}

.information-content::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.edge-info-icon {
  position: relative;
  display: inline-flex;
  align-items: center;
  padding-bottom: 6px;
  cursor: pointer;
  color: var(--muted);
}

.edge-info-icon.active,
.edge-info-icon:hover {
  color: var(--accent);
}

.edge-types-tooltip {
  position: absolute;
  top: calc(100% - 2px);
  left: 0;
  background: rgba(255, 255, 255, 0.98);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.16);
  padding: 10px 12px;
  z-index: 200;
  min-width: 230px;
  white-space: nowrap;
}

.edge-tooltip-title {
  font-size: 10px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 8px;
  text-transform: uppercase;
}

.edge-type-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  padding: 5px 6px;
  border-radius: 6px;
  cursor: pointer;
}

.edge-type-item:hover {
  background: rgba(56, 189, 248, 0.08);
}

.edge-type-item.active {
  background: rgba(56, 189, 248, 0.12);
  box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.35);
}

.edge-type-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.edge-type-name {
  font-size: 10px;
  font-weight: 700;
  color: #1f2937;
}

.edge-type-desc {
  font-size: 9px;
  color: #64748b;
}

.edge-filter-reset {
  margin-top: 8px;
  width: 100%;
  border: none;
  border-radius: 6px;
  padding: 6px 8px;
  background: rgba(79, 70, 229, 0.08);
  color: #4338ca;
  font-size: 10px;
  cursor: pointer;
}
</style>
