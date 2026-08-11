<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom'
import { select } from 'd3-selection'
import {
  type BlockId,
  fetchCfgViewData,
  fetchLegendData,
  fetchPlainBlockLlmAnalysis,
  fetchCallTreeData,
  fetchSwapPatternResult,
  type BlockAction,
  type BlockInformation,
  type BlockInformationMap,
  type CfgMode,
  type CfgViewData,
  type EdgeStepMap,
  type PlainBlockLlmAnalysisResponse,
  type CallTreePayload,
  type StepRange,
  type SwapPatternResult,
  fetchBlockInformation,
} from '../api/analyze'
import { CFG_EDGE_COLORS, getDarkAccentForColor, getFillColorForColor } from '../visualTheme'
import GraphExpandButton from './GraphExpandButton.vue'
import GraphFitButton from './GraphFitButton.vue'

const props = defineProps<{
  txHash: string | null
  highlightedBlockId: BlockId[] | null
  filteredEdgeIds: string[] | null
  selectedStepRange: { entryStep: number; exitStep: number } | null
  isAnalyzing: boolean
  edgeStepMap: EdgeStepMap | null
  playbackCutoffStep: number | null
  playbackActive: boolean
  playbackVisibleBlockIds: BlockId[] | null
  playbackCurrentBlockId: BlockId | null
  preferredMode: CfgMode
  plainReady: boolean
  foldedInformationReady: boolean
  plainInformationReady: boolean
  expanded: boolean
}>()

const emit = defineEmits<{
  'cfg-navigate': [blockIds: BlockId[] | null]
  'mode-change': [mode: CfgMode]
  'toggle-expanded': []
}>()

type CfgEdgeType = 'NORMAL' | 'JUMP' | 'CALL' | 'DELEGATECALL' | 'TERMINATE'
type LlmAnalysisState = 'idle' | 'loading' | 'success' | 'error'
type SwapHighlightGroup = {
  key: string
  nodes: string[]
}
type ActionTone = 'read' | 'write' | 'send' | 'other'
type ActionDisplay = {
  key: string
  title: string
  tone: ActionTone
  details: Array<{ label: string, value: string }>
}

const ACTION_NODE_STROKE = '#DC2626'
const ACTION_NODE_GLOW = 'rgba(239, 68, 68, 0.82)'
const ACTION_NODE_GLOW_SOFT = 'rgba(248, 113, 113, 0.48)'
const ACTION_NODE_GLOW_WIDE = 'rgba(252, 165, 165, 0.28)'
const PRIORITY_SIGNATURES = new Set<string>([
  'transfer()',
  'transferFrom()',
  'approve()',
  'safeTransfer()',
  'safeTransferFrom()',
  'swapExactTokensForTokens()',
  'swapTokensForExactTokens()',
  'swapExactETHForTokens()',
  'swapTokensForExactETH()',
  'swapExactTokensForETH()',
  'swap()',
  'exactInputSingle()',
  'exactOutputSingle()',
  'multicall()',
  'flashLoan()',
  'flashSwap()',
  'executeOperation()',
  'flashLoanSimple()',
  'getReserves()',
  'getAmountOut()',
  'getAmountIn()',
  'balanceOf()',
  'decimals()',
  'factory()',
  'pairFor()',
  'getPool()',
  'deposit()',
  'withdraw()',
  'safeApprove()',
  'pull()',
  'call()',
])

const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const cfgMode = ref<CfgMode>('folded')
const cfgViews = ref<Partial<Record<CfgMode, CfgViewData>>>({})
const plainLoading = ref(false)
const informationLoadedByMode = ref<Record<CfgMode, boolean>>({ folded: false, plain: false })
const informationLoadingByMode = ref<Record<CfgMode, boolean>>({ folded: false, plain: false })
const informationErrorByMode = ref<Record<CfgMode, string>>({ folded: '', plain: '' })
const blockInformation = ref<BlockInformationMap>({})
const blockInformationByMode = ref<Record<CfgMode, BlockInformationMap>>({ folded: {}, plain: {} })
const selectedNodeName = ref<string | null>(null)
const selectedBlockInfo = ref<BlockInformation | null>(null)
const stepRangesExpanded = ref(false)
const graphContainer = ref<HTMLElement | null>(null)
const addressNameMap = ref<Map<string, string>>(new Map())
const highlightedNodes = ref<Set<string>>(new Set())
const visibleNodes = ref<Set<string>>(new Set())
const visibleEdges = ref<Set<string>>(new Set())
const edgeConnections = ref<Map<string, { source: string, target: string }>>(new Map())
const nodeNameToEl = ref<Map<string, Element>>(new Map())
const edgeIdToSvgTitle = ref<Map<string, string>>(new Map())
const edgeFirstStepByTitle = ref<Map<string, number>>(new Map())
const activeEdgeType = ref<CfgEdgeType | null>(null)
const showEdgeTypes = ref(false)
const llmAnalysisState = ref<LlmAnalysisState>('idle')
const llmAnalysisResponse = ref<PlainBlockLlmAnalysisResponse | null>(null)
const llmAnalysisError = ref('')
const sequenceCallMapping = ref<CallTreePayload | null>(null)
const swapSeedNodesByMode = ref<Record<CfgMode, Set<string>>>({
  folded: new Set(),
  plain: new Set(),
})
let llmRequestToken = 0
const PLAIN_SCALE_MULTIPLIER = 1.35
const PLAIN_MIN_WIDTH_RATIO = 1.15
const PLAIN_VIEW_PADDING = 20

let zoomBehavior: ZoomBehavior<SVGSVGElement, unknown> | null = null
let resizeObserver: ResizeObserver | null = null
let edgeTooltipHideTimer: number | null = null
let focusFrameId: number | null = null
let cfgLoadRequestId = 0

const cfgModeBadge = computed(() => cfgMode.value === 'folded' ? 'Folded CFG' : 'Plain CFG')
const toggleButtonLabel = computed(() => cfgMode.value === 'folded' ? 'Plain CFG' : 'Folded CFG')
const hasSelection = computed(() => Boolean(selectedBlockInfo.value))
const currentInformationReady = computed(() => informationLoadedByMode.value[cfgMode.value])
const currentInformationError = computed(() => informationErrorByMode.value[cfgMode.value])
const selectedFoldedStepRanges = computed<StepRange[]>(
  () => selectedBlockInfo.value?.step_ranges ?? [],
)
const visibleFoldedStepRanges = computed<StepRange[]>(() => (
  stepRangesExpanded.value
    ? selectedFoldedStepRanges.value
    : selectedFoldedStepRanges.value.slice(0, 1)
))
const selectedActionDisplays = computed<ActionDisplay[]>(() => {
  const actions = selectedBlockInfo.value?.actions ?? []
  return actions.flatMap((action, index) => buildActionDisplays(action, index))
})

watch(() => props.txHash, (newHash) => {
  cfgLoadRequestId += 1
  resetSelection()
  if (newHash) {
    loadCfgData(newHash)
  } else {
    status.value = 'idle'
    cfgViews.value = {}
    sequenceCallMapping.value = null
    blockInformation.value = {}
    blockInformationByMode.value = { folded: {}, plain: {} }
    informationLoadedByMode.value = { folded: false, plain: false }
    informationLoadingByMode.value = { folded: false, plain: false }
    informationErrorByMode.value = { folded: '', plain: '' }
    resetSwapSeedNodes()
    clearFilterState()
    if (graphContainer.value) {
      graphContainer.value.innerHTML = ''
    }
  }
}, { immediate: true })

watch(() => props.highlightedBlockId, (newBlockIds) => {
  if (cfgMode.value === 'folded' && props.filteredEdgeIds?.length) return
  if (cfgMode.value === 'plain' && props.selectedStepRange) return

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

watch([
  () => props.playbackCutoffStep,
  () => props.playbackActive,
  () => props.playbackCurrentBlockId,
], ([cutoffStep, active, currentBlockId]) => {
  if (selectedNodeName.value && !isPlaybackNodeVisible(selectedNodeName.value)) {
    resetSelection()
  }
  applyFilter()
  if (active && cfgMode.value === 'plain') {
    nextTick(() => followPlainPlaybackBlock(currentBlockId, cutoffStep))
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
  if (cfgMode.value !== 'folded') return

  if (edgeIds && edgeIds.length > 0) {
    applyEdgeFilter(edgeIds)
  } else if (!props.highlightedBlockId || props.highlightedBlockId.length === 0) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
  }
})

watch(() => props.selectedStepRange, (stepRange) => {
  if (cfgMode.value !== 'plain') return

  if (stepRange) {
    applyPlainStepFilter(stepRange)
  } else if (!props.highlightedBlockId || props.highlightedBlockId.length === 0) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
  }
})

watch(() => props.preferredMode, (mode) => {
  if (status.value !== 'success' || mode === cfgMode.value) return
  void switchCfgMode(mode)
})

watch(() => props.plainReady, (ready) => {
  if (ready && props.txHash && status.value === 'success') {
    void ensureCfgView('plain')
  }
})

watch(() => props.foldedInformationReady, (ready) => {
  if (ready) void loadCfgInformation('folded')
})

watch(() => props.plainInformationReady, (ready) => {
  if (ready) void loadCfgInformation('plain')
})

watch(
  [() => props.txHash, () => cfgMode.value, () => selectedBlockInfo.value?.block_id],
  ([txHash, mode, selectedBlockId]) => {
    if (!txHash || mode !== 'plain' || selectedBlockId === undefined || selectedBlockId === null) {
      resetLlmAnalysis()
      return
    }
    void loadLlmAnalysis(txHash, selectedBlockId)
  },
)

async function loadCfgData(txHash: string) {
  const requestId = cfgLoadRequestId
  status.value = 'loading'
  errorMsg.value = ''
  activeEdgeType.value = null
  cfgViews.value = {}
  informationLoadedByMode.value = { folded: false, plain: false }
  informationLoadingByMode.value = { folded: false, plain: false }
  informationErrorByMode.value = { folded: '', plain: '' }
  sequenceCallMapping.value = null
  blockInformation.value = {}
  blockInformationByMode.value = { folded: {}, plain: {} }
  clearFilterState()
  resetSelection()

  try {
    const [foldedView, legend, callMapping] = await Promise.all([
      fetchCfgViewData(txHash, 'folded'),
      fetchLegendData(txHash),
      fetchCallTreeData(txHash).catch(() => null),
    ])
    if (requestId !== cfgLoadRequestId || props.txHash !== txHash) return

    cfgViews.value = { folded: foldedView }
    sequenceCallMapping.value = callMapping
    swapSeedNodesByMode.value = {
      folded: new Set(),
      plain: new Set(),
    }

    addressNameMap.value.clear()
    for (const entry of [...legend.user_addresses, ...legend.erc20_tokens, ...legend.normal_contracts]) {
      addressNameMap.value.set(entry.address.toLowerCase(), entry.name)
    }

    status.value = 'success'
    await switchCfgMode('folded')
    if (requestId !== cfgLoadRequestId) return
    if (props.foldedInformationReady) void loadCfgInformation('folded')
    if (props.plainReady) void ensureCfgView('plain')
  } catch (e: any) {
    if (requestId !== cfgLoadRequestId) return
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load CFG data'
    resetSwapSeedNodes()
  }
}

function getCfgView(mode: CfgMode) {
  return cfgViews.value[mode] ?? null
}

async function switchCfgMode(mode: CfgMode) {
  if (mode === 'plain' && !props.plainReady) return
  const requestId = cfgLoadRequestId
  const nextView = getCfgView(mode) ?? await ensureCfgView(mode)
  if (!nextView || requestId !== cfgLoadRequestId) return

  cfgMode.value = mode
  const cachedInformation = blockInformationByMode.value[mode]
  const nextInformation = Object.keys(cachedInformation).length > 0
    ? cachedInformation
    : nextView.blockInformation ?? {}
  blockInformation.value = nextInformation
  if (Object.keys(cachedInformation).length === 0 && Object.keys(nextInformation).length > 0) {
    blockInformationByMode.value = { ...blockInformationByMode.value, [mode]: nextInformation }
  }
  resetSelection()
  emit('mode-change', mode)

  await nextTick()
  renderSvg(nextView.svgContent)
}

async function ensureCfgView(mode: CfgMode): Promise<CfgViewData | null> {
  const existing = getCfgView(mode)
  if (existing) return existing
  if (!props.txHash || (mode === 'plain' && !props.plainReady)) return null
  const txHash = props.txHash
  const requestId = cfgLoadRequestId

  if (mode === 'plain') plainLoading.value = true
  try {
    const view = await fetchCfgViewData(txHash, mode)
    if (requestId !== cfgLoadRequestId || props.txHash !== txHash) return null
    cfgViews.value = { ...cfgViews.value, [mode]: view }
    const informationReady = mode === 'folded'
      ? props.foldedInformationReady
      : props.plainInformationReady
    if (informationReady) void loadCfgInformation(mode)
    return view
  } catch (e: any) {
    if (requestId !== cfgLoadRequestId) return null
    errorMsg.value = e.message || `Failed to load ${mode} CFG data`
    return null
  } finally {
    if (mode === 'plain' && requestId === cfgLoadRequestId) plainLoading.value = false
  }
}

async function loadCfgInformation(mode: CfgMode) {
  if (!props.txHash || informationLoadedByMode.value[mode] || informationLoadingByMode.value[mode]) return
  const informationReady = mode === 'folded'
    ? props.foldedInformationReady
    : props.plainInformationReady
  if (!informationReady) return

  const txHash = props.txHash
  const requestId = cfgLoadRequestId
  informationLoadingByMode.value = { ...informationLoadingByMode.value, [mode]: true }
  informationErrorByMode.value = { ...informationErrorByMode.value, [mode]: '' }
  try {
    const view = getCfgView(mode) ?? await ensureCfgView(mode)
    if (!view || requestId !== cfgLoadRequestId) return
    const [information, swapPatterns] = await Promise.all([
      fetchBlockInformation(txHash, mode),
      fetchSwapPatternResult(txHash, mode),
    ])
    if (requestId !== cfgLoadRequestId || props.txHash !== txHash) return
    cfgViews.value = {
      ...cfgViews.value,
      [mode]: { ...view, blockInformation: information },
    }
    blockInformationByMode.value = { ...blockInformationByMode.value, [mode]: information }
    informationLoadedByMode.value = { ...informationLoadedByMode.value, [mode]: true }
    swapSeedNodesByMode.value = {
      ...swapSeedNodesByMode.value,
      [mode]: collectSwapSeedNodes(swapPatterns),
    }
    if (cfgMode.value === mode) {
      blockInformation.value = information
      resetSelection()
      if (view) {
        await nextTick()
        renderSvg(view.svgContent)
      }
    }
  } catch (e: any) {
    if (requestId === cfgLoadRequestId) {
      informationErrorByMode.value = {
        ...informationErrorByMode.value,
        [mode]: e.message || `Failed to load ${mode} CFG information`,
      }
    }
  } finally {
    if (requestId === cfgLoadRequestId) {
      informationLoadingByMode.value = { ...informationLoadingByMode.value, [mode]: false }
    }
  }
}

function toggleCfgMode() {
  const nextMode: CfgMode = cfgMode.value === 'folded' ? 'plain' : 'folded'
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
    applyNonScalingStrokes(origG as SVGGElement)
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
  if (props.playbackActive && cfgMode.value === 'plain') {
    nextTick(() => followPlainPlaybackBlock(props.playbackCurrentBlockId, props.playbackCutoffStep))
  }
  if (!hasActiveLinkedSelection()) {
    if (cfgMode.value === 'folded') {
      nextTick(() => resetZoom())
    } else {
      nextTick(() => fitGraphToViewport())
    }
  }
}

function setFoldedViewBox(svg: SVGSVGElement, graphContent: SVGGElement) {
  const bounds = getGraphContentBounds(graphContent)
  if (bounds.width <= 0 || bounds.height <= 0) return

  const padding = 16
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

function applyNonScalingStrokes(graphContent: SVGGElement) {
  graphContent.querySelectorAll<SVGElement>('line, path, rect, ellipse, polygon').forEach((el) => {
    el.setAttribute('vector-effect', 'non-scaling-stroke')
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
  annotatePlainCallEdges(svg as SVGSVGElement)
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
  renderSwapHighlightOverlay()
}

function prepareSvgMetadata(svg: SVGSVGElement) {
  const edgeColorMap = new Map<string, CfgEdgeType>(
    edgeTypes.flatMap(edge => edge.aliases.map(color => [color.toLowerCase(), edge.type] as const)),
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
    cluster.querySelectorAll('text').forEach((textEl) => textEl.remove())
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

    harmonizeEdgeAppearance(edge as SVGGElement, matchedType ?? null)
  })

  svg.querySelectorAll('.node').forEach((node) => {
    harmonizeNodeAppearance(node as SVGGElement)
  })

  svg.querySelectorAll('title').forEach((titleEl) => titleEl.remove())
}

type PlainStepBlock = {
  nodeName: string
  startStep: number
  endStep: number
  span: number
}

function toFiniteStep(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function formatCompactCallSignature(signatures: string[]): string {
  if (!signatures.length) return ''
  const first = signatures[0]!
  if (PRIORITY_SIGNATURES.has(first)) {
    return `${first}/...`
  }
  const top2 = signatures.slice(0, 2).join('/')
  return signatures.length > 2 ? `${top2}/...` : top2
}

function buildPlainStepBlocks(): PlainStepBlock[] {
  const blocks: PlainStepBlock[] = []
  Object.values(blockInformation.value).forEach((block) => {
    const start = toFiniteStep(block.start_step)
    const end = toFiniteStep(block.end_step)
    if (start === null || end === null || end < start) return
    const nodeName = `node_${String(block.block_id)}`
    blocks.push({
      nodeName,
      startStep: start,
      endStep: end,
      span: end - start,
    })
  })

  blocks.sort((a, b) => {
    if (a.span !== b.span) return a.span - b.span
    if (a.startStep !== b.startStep) return a.startStep - b.startStep
    return a.nodeName.localeCompare(b.nodeName)
  })
  return blocks
}

function findPlainNodeByStep(step: number, blocks: PlainStepBlock[]): string | null {
  for (const block of blocks) {
    if (block.startStep <= step && step <= block.endStep) {
      return block.nodeName
    }
  }
  return null
}

function getEdgeLabelPlacement(edgeEl: SVGGElement, fallbackBox: DOMRect | SVGRect) {
  const pathCandidates = Array.from(edgeEl.querySelectorAll<SVGPathElement>('path'))
  const mainPath =
    pathCandidates.find((path) => path.getAttribute('fill') === 'none') ||
    pathCandidates[0] ||
    null

  if (mainPath && typeof mainPath.getTotalLength === 'function') {
    try {
      const total = mainPath.getTotalLength()
      if (Number.isFinite(total) && total > 0) {
        const midLen = total * 0.5
        const sample = Math.min(4, total * 0.2)
        const pMid = mainPath.getPointAtLength(midLen)
        const pPrev = mainPath.getPointAtLength(Math.max(0, midLen - sample))
        const pNext = mainPath.getPointAtLength(Math.min(total, midLen + sample))

        const tx = pNext.x - pPrev.x
        const ty = pNext.y - pPrev.y
        const norm = Math.hypot(tx, ty) || 1
        const nx = -ty / norm
        const ny = tx / norm
        const offset = 16

        return {
          x: pMid.x + nx * offset,
          y: pMid.y + ny * offset,
          anchor: nx >= 0 ? 'start' : 'end',
        } as const
      }
    } catch {
      // Fall through to bbox placement
    }
  }

  return {
    x: fallbackBox.x + fallbackBox.width + 24,
    y: fallbackBox.y + fallbackBox.height * 0.5,
    anchor: 'start',
  } as const
}

function annotatePlainCallEdges(svg: SVGSVGElement) {
  if (cfgMode.value !== 'plain') return

  svg.querySelectorAll('.call-signature-label').forEach((el) => el.remove())

  const mapping = sequenceCallMapping.value
  if (!mapping?.calls?.length) return

  const blocks = buildPlainStepBlocks()
  if (!blocks.length) return

  const edgeByTitle = new Map<string, SVGGElement>()
  svg.querySelectorAll<SVGGElement>('.edge').forEach((edgeEl) => {
    const title = getEdgeTitle(edgeEl)
    if (title) edgeByTitle.set(title, edgeEl)
  })
  if (!edgeByTitle.size) return

  const labelByEdgeTitle = new Map<string, string>()
  for (const call of mapping.calls) {
    const signatures = call.probable_text_signatures || []
    if (!signatures.length) continue

    const signatureLabel = formatCompactCallSignature(signatures)
    if (!signatureLabel) continue

    const callStep = toFiniteStep(call.entry_step)
    if (callStep === null) continue

    const sourceNode = findPlainNodeByStep(callStep, blocks)
    const targetNode = findPlainNodeByStep(callStep + 1, blocks)
    if (!sourceNode || !targetNode) continue

    const edgeTitle = `${sourceNode}->${targetNode}`
    const edgeEl = edgeByTitle.get(edgeTitle)
    if (!edgeEl) continue

    const edgeType = getEdgeType(edgeEl)
    if (edgeType && edgeType !== 'CALL' && edgeType !== 'DELEGATECALL') continue
    if (!labelByEdgeTitle.has(edgeTitle)) {
      labelByEdgeTitle.set(edgeTitle, signatureLabel)
    }
  }

  for (const [edgeTitle, label] of labelByEdgeTitle.entries()) {
    const edgeEl = edgeByTitle.get(edgeTitle)
    if (!edgeEl) continue

    const edgeBox = edgeEl.getBBox()
    if (edgeBox.width <= 0 || edgeBox.height <= 0) continue

    const placement = getEdgeLabelPlacement(edgeEl, edgeBox)
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text')
    text.setAttribute('class', 'call-signature-label')
    text.setAttribute('x', String(placement.x))
    text.setAttribute('y', String(placement.y))
    text.setAttribute('text-anchor', placement.anchor)
    text.setAttribute('dominant-baseline', 'middle')
    text.textContent = label
    edgeEl.appendChild(text)
  }
}

function harmonizeNodeAppearance(node: SVGGElement) {
  const shape = node.querySelector<SVGElement>('ellipse, polygon, rect, path')
  if (!shape) return

  const originalFill = shape.getAttribute('fill')
  const fill = getFillColorForColor(originalFill)
  const dark = getDarkAccentForColor(originalFill)
  const sourceStroke = normalizeColor(shape.getAttribute('stroke'))
  const strokeWidth = Number.parseFloat(shape.getAttribute('stroke-width') || '0')
  const isActionNode =
    sourceStroke === normalizeColor(ACTION_NODE_STROKE) ||
    sourceStroke === '#d06a54' ||
    sourceStroke === '#ff0000' ||
    strokeWidth >= 4

  node.classList.toggle('action-node', isActionNode)
  node.dataset.themeDark = dark

  if (isActionNode) {
    shape.setAttribute('fill', fill)
    shape.style.fill = fill
    shape.setAttribute('stroke', ACTION_NODE_STROKE)
    shape.style.stroke = ACTION_NODE_STROKE
    shape.setAttribute('stroke-width', '1.8')
    shape.style.filter = `drop-shadow(0 0 12px ${ACTION_NODE_GLOW}) drop-shadow(0 0 28px ${ACTION_NODE_GLOW_SOFT}) drop-shadow(0 0 52px ${ACTION_NODE_GLOW_WIDE})`
  } else {
    shape.setAttribute('fill', fill)
    shape.style.fill = fill
    shape.setAttribute('stroke', dark)
    shape.style.stroke = dark
    shape.setAttribute('stroke-width', '1.8')
    shape.style.filter = ''
  }

  node.querySelectorAll<SVGTextElement>('text').forEach((textEl) => {
    textEl.setAttribute('fill', '#000000')
    textEl.style.fill = '#000000'
  })
}

function harmonizeEdgeAppearance(edge: SVGGElement, edgeType: CfgEdgeType | null) {
  if (!edgeType) return

  const edgeColor = edgeTypes.find(item => item.type === edgeType)?.color || CFG_EDGE_COLORS.NORMAL
  const width = edgeType === 'JUMP' ? '1.5' : edgeType === 'NORMAL' ? '0.56' : '1.62'
  const opacity = edgeType === 'JUMP' ? '0.82' : edgeType === 'NORMAL' ? '0.88' : '0.90'
  const arrowScale = edgeType === 'NORMAL' ? 1.5 : edgeType === 'JUMP' ? 2 : 3

  edge.querySelectorAll<SVGPathElement>('path').forEach((path) => {
    if (path.getAttribute('fill') === 'none') {
      path.setAttribute('stroke', edgeColor)
      path.style.stroke = edgeColor
      path.setAttribute('stroke-width', width)
      path.style.strokeWidth = width
      path.style.opacity = opacity
    }
  })

  edge.querySelectorAll<SVGPolygonElement>('polygon').forEach((polygon) => {
    polygon.setAttribute('stroke', edgeColor)
    polygon.style.stroke = edgeColor
    polygon.setAttribute('fill', edgeColor)
    polygon.style.fill = edgeColor
    polygon.setAttribute('stroke-width', width)
    polygon.style.strokeWidth = width
    polygon.style.opacity = opacity
    scalePolygon(polygon, arrowScale)
  })
}

function scalePolygon(polygon: SVGPolygonElement, scale: number) {
  const rawPoints = (polygon.getAttribute('points') || '')
    .trim()
    .split(/\s+/)
    .map((pair) => pair.split(',').map(Number))
    .filter((pair): pair is [number, number] => pair.length === 2 && pair.every(Number.isFinite))

  if (rawPoints.length === 0) return

  const center = rawPoints.reduce((acc, [x, y]) => ({
    x: acc.x + x / rawPoints.length,
    y: acc.y + y / rawPoints.length,
  }), { x: 0, y: 0 })

  const scaled = rawPoints
    .map(([x, y]) => {
      const nextX = center.x + (x - center.x) * scale
      const nextY = center.y + (y - center.y) * scale
      return `${nextX.toFixed(2)},${nextY.toFixed(2)}`
    })
    .join(' ')

  polygon.setAttribute('points', scaled)
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

function collectSwapSeedNodes(result: SwapPatternResult): Set<string> {
  const entries = [...result.pattern_1, ...result.pattern_2]
  const nodeNames = new Set<string>()
  entries.forEach((entry) => {
    const rawId = entry?.id
    if (rawId === undefined || rawId === null || rawId === '') return
    nodeNames.add(`node_${String(rawId)}`)
  })
  return nodeNames
}

function getNumericNodeId(nodeName: string): number | null {
  const blockId = extractBlockIdFromNodeName(nodeName)
  if (blockId === null) return null

  const numericId = Number(blockId)
  return Number.isFinite(numericId) ? numericId : null
}

function resetSwapSeedNodes() {
  swapSeedNodesByMode.value = {
    folded: new Set(),
    plain: new Set(),
  }
}

function buildSwapHighlightGroups(seedNodes: Set<string>): SwapHighlightGroup[] {
  const groups: SwapHighlightGroup[] = []
  const seenKeys = new Set<string>()

  seedNodes.forEach((seedNode) => {
    const nodes = new Set<string>()
    const seedNodeId = getNumericNodeId(seedNode)
    if (nodeNameToEl.value.has(seedNode)) {
      nodes.add(seedNode)
    }

    edgeConnections.value.forEach(({ source, target }) => {
      const sourceId = getNumericNodeId(source)
      const targetId = getNumericNodeId(target)

      if (
        source === seedNode &&
        nodeNameToEl.value.has(target) &&
        seedNodeId !== null &&
        targetId !== null &&
        targetId > seedNodeId
      ) {
        nodes.add(target)
      }
      if (
        target === seedNode &&
        nodeNameToEl.value.has(source) &&
        seedNodeId !== null &&
        sourceId !== null &&
        sourceId > seedNodeId
      ) {
        nodes.add(source)
      }
    })

    if (nodes.size === 0) return

    const nodeList = Array.from(nodes)
    const key = nodeList.slice().sort().join('|')
    if (seenKeys.has(key)) return
    seenKeys.add(key)

    groups.push({
      key,
      nodes: nodeList,
    })
  })

  return groups
}

function getSwapHighlightBounds(nodes: string[]) {
  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY

  nodes.forEach((nodeName) => {
    const nodeEl = nodeNameToEl.value.get(nodeName)
    if (!(nodeEl instanceof SVGGraphicsElement)) return

    const bbox = nodeEl.getBBox()
    minX = Math.min(minX, bbox.x)
    minY = Math.min(minY, bbox.y)
    maxX = Math.max(maxX, bbox.x + bbox.width)
    maxY = Math.max(maxY, bbox.y + bbox.height)
  })

  if (!Number.isFinite(minX) || !Number.isFinite(minY) || !Number.isFinite(maxX) || !Number.isFinite(maxY)) {
    return null
  }

  return {
    x: minX,
    y: minY,
    width: maxX - minX,
    height: maxY - minY,
  }
}

function clearSwapHighlightOverlay() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  svg?.querySelector('#swap-highlight-layer')?.remove()
}

function renderSwapHighlightOverlay() {
  clearSwapHighlightOverlay()

  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg') as SVGSVGElement | null
  const zoomLayer = svg?.querySelector('#zoom-layer') as SVGGElement | null
  const graphContent = zoomLayer?.firstElementChild as SVGGElement | null
  if (!svg || !zoomLayer || !graphContent) return

  const seedNodes = swapSeedNodesByMode.value[cfgMode.value]
  if (!seedNodes || seedNodes.size === 0) return

  const groups = buildSwapHighlightGroups(seedNodes)
  if (groups.length === 0) return

  const overlayLayer = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  overlayLayer.setAttribute('id', 'swap-highlight-layer')
  overlayLayer.setAttribute('pointer-events', 'none')

  const paddingX = 38
  const paddingY = 32

  groups.forEach((group) => {
    const bounds = getSwapHighlightBounds(group.nodes)
    if (!bounds) return

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
    rect.classList.add('swap-highlight-box')
    rect.dataset.groupKey = group.key
    rect.dataset.nodes = group.nodes.join('|')
    rect.setAttribute('x', String(bounds.x - paddingX))
    rect.setAttribute('y', String(bounds.y - paddingY))
    rect.setAttribute('width', String(bounds.width + paddingX * 2))
    rect.setAttribute('height', String(bounds.height + paddingY * 2))
    rect.setAttribute('rx', '14')
    rect.setAttribute('ry', '14')
    overlayLayer.appendChild(rect)
  })

  if (!overlayLayer.childNodes.length) return

  graphContent.appendChild(overlayLayer)
  updateSwapHighlightOverlayVisibility()
}

function updateSwapHighlightOverlayVisibility() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  svg.querySelectorAll<SVGRectElement>('#swap-highlight-layer .swap-highlight-box').forEach((rect) => {
    const nodes = (rect.dataset.nodes || '').split('|').filter(Boolean)
    const matchesLinkedFilter = visibleNodes.value.size === 0 || nodes.every(node => visibleNodes.value.has(node))
    const shouldShow = matchesLinkedFilter && nodes.every(node => isPlaybackNodeVisible(node))
    rect.classList.toggle('hidden', !shouldShow)
  })
}

function buildEdgeIdToTitleMap() {
  edgeIdToSvgTitle.value.clear()
  edgeFirstStepByTitle.value.clear()
  if (!props.edgeStepMap) return

  for (const [edgeId, entry] of Object.entries(props.edgeStepMap)) {
    const title = `${entry.source_node}->${entry.target_node}`
    edgeIdToSvgTitle.value.set(edgeId, title)
    const previous = edgeFirstStepByTitle.value.get(title)
    if (previous === undefined || entry.edge_step < previous) {
      edgeFirstStepByTitle.value.set(title, entry.edge_step)
    }
  }
}

function syncFilterStateWithProps() {
  if (cfgMode.value === 'folded' && props.filteredEdgeIds && props.filteredEdgeIds.length > 0) {
    applyEdgeFilter(props.filteredEdgeIds)
    return
  }

  if (cfgMode.value === 'plain' && props.selectedStepRange) {
    applyPlainStepFilter(props.selectedStepRange)
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

function hasActiveLinkedSelection(): boolean {
  if (props.highlightedBlockId && props.highlightedBlockId.length > 0) return true
  if (cfgMode.value === 'folded') return Boolean(props.filteredEdgeIds?.length)
  return Boolean(props.selectedStepRange)
}

function extractBlockIdFromNodeName(nodeName: string): string | null {
  if (!nodeName.startsWith('node_')) return null
  const rawId = nodeName.slice(5)
  return rawId ? rawId : null
}

function isPlaybackNodeVisible(nodeName: string): boolean {
  const cutoff = props.playbackCutoffStep
  if (cutoff === null || !currentInformationReady.value) return true
  const blockId = extractBlockIdFromNodeName(nodeName)
  const block = blockId === null ? null : blockInformation.value[String(blockId)]
  if (!block) return false

  if (cfgMode.value === 'plain' && props.playbackActive && props.playbackVisibleBlockIds !== null) {
    return props.playbackVisibleBlockIds.some(id => String(id) === String(block.block_id))
  }

  if (cfgMode.value === 'folded') {
    const ranges = block.step_ranges || []
    return ranges.some(range => Number(range.start_step) <= cutoff)
  }
  const startStep = Number(block.start_step)
  return Number.isFinite(startStep) && startStep <= cutoff
}

function isPlaybackEdgeVisible(edgeTitle: string): boolean {
  const cutoff = props.playbackCutoffStep
  if (cutoff === null || !currentInformationReady.value) return true

  if (cfgMode.value === 'folded') {
    const firstStep = edgeFirstStepByTitle.value.get(edgeTitle)
    if (firstStep !== undefined) return firstStep <= cutoff
  }

  const connection = edgeConnections.value.get(edgeTitle)
  return Boolean(
    connection
    && isPlaybackNodeVisible(connection.source)
    && isPlaybackNodeVisible(connection.target),
  )
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
  stepRangesExpanded.value = false

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
  stepRangesExpanded.value = false
  resetLlmAnalysis()
}

function resetLlmAnalysis() {
  llmRequestToken += 1
  llmAnalysisState.value = 'idle'
  llmAnalysisResponse.value = null
  llmAnalysisError.value = ''
}

async function loadLlmAnalysis(txHash: string, blockId: BlockId) {
  const requestToken = ++llmRequestToken
  llmAnalysisState.value = 'loading'
  llmAnalysisResponse.value = null
  llmAnalysisError.value = ''

  try {
    const response = await fetchPlainBlockLlmAnalysis(txHash, blockId)
    if (requestToken !== llmRequestToken) return
    llmAnalysisResponse.value = response
    llmAnalysisState.value = 'success'
  } catch (e: any) {
    if (requestToken !== llmRequestToken) return
    llmAnalysisState.value = 'error'
    llmAnalysisResponse.value = null
    const message = e?.message || 'Failed to generate LLM analysis'
    llmAnalysisError.value = message.includes('exceeds limit')
      ? 'Context too large. Strict context mode is enabled, so this block cannot be analyzed.'
      : message
  }
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
    applyNonScalingStrokes(graphContent)
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
  scheduleFocusOnNodes(matchedNodes)
}

function applyPlainStepFilter(stepRange: { entryStep: number; exitStep: number }) {
  const entryStep = Math.min(stepRange.entryStep, stepRange.exitStep)
  const exitStep = Math.max(stepRange.entryStep, stepRange.exitStep)
  const blocks = buildPlainStepBlocks()
  const matchedNodes = new Set(
    blocks
      .filter(block => block.startStep <= exitStep && block.endStep >= entryStep)
      .map(block => block.nodeName),
  )

  if (matchedNodes.size === 0) {
    clearFilterState()
    applyFilter()
    nextTick(() => resetZoom())
    return
  }

  const callSourceNode = findPlainNodeByStep(entryStep, blocks)
  const callTargetNode = findPlainNodeByStep(entryStep + 1, blocks)
  const entryNodes = [callSourceNode, callTargetNode].filter(
    (nodeName): nodeName is string => Boolean(nodeName),
  )
  entryNodes.forEach(nodeName => matchedNodes.add(nodeName))

  const matchedEdges = new Set<string>()
  edgeConnections.value.forEach(({ source, target }, edgeTitle) => {
    if (matchedNodes.has(source) && matchedNodes.has(target)) {
      matchedEdges.add(edgeTitle)
    }
  })

  visibleNodes.value = matchedNodes
  visibleEdges.value = matchedEdges
  highlightedNodes.value = new Set(entryNodes)
  applyFilter()
  scheduleFocusOnNodes(matchedNodes)
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
  scheduleFocusOnNodes(visible)
}

function scheduleFocusOnNodes(nodeNames: Iterable<string>) {
  const targets = [...new Set(nodeNames)]
  if (targets.length === 0) return

  if (focusFrameId !== null) window.cancelAnimationFrame(focusFrameId)
  focusFrameId = window.requestAnimationFrame(() => {
    focusFrameId = null
    focusNodesInViewport(targets)
  })
}

function focusNodesInViewport(nodeNames: string[]) {
  if (!graphContainer.value || !zoomBehavior) return

  const svg = graphContainer.value.querySelector('svg') as SVGSVGElement | null
  const zoomLayer = svg?.querySelector('#zoom-layer') as SVGGElement | null
  if (!svg || !zoomLayer) return

  const nodeElements = nodeNames
    .map(nodeName => nodeNameToEl.value.get(nodeName))
    .filter((element): element is SVGGraphicsElement => (
      Boolean(element) && typeof (element as SVGGraphicsElement).getBBox === 'function'
    ))
  const visibleEdgeElements = Array.from(
    svg.querySelectorAll<SVGGraphicsElement>('.edge:not(.filtered-out)'),
  )
  const elements = [...new Set([...nodeElements, ...visibleEdgeElements])]

  if (cfgMode.value === 'plain') {
    // Plain CFG deliberately uses a wide, horizontally scrollable SVG. Keep
    // that original scale and layout; linkage only restores the neutral zoom
    // transform and moves the scrollbar to the linked region.
    select(svg).call(zoomBehavior.transform, zoomIdentity)
    window.requestAnimationFrame(() => scrollPlainCfgToElements(elements))
    return
  }

  const bounds = getElementsBoundsInZoomLayer(elements, zoomLayer)
  const viewBox = svg.viewBox.baseVal
  if (!bounds || viewBox.width <= 0 || viewBox.height <= 0) return

  // Fit the complete non-muted selection, rather than zooming to only its
  // entry/target block. A small margin keeps boundary edges readable.
  const targetWidth = Math.max(bounds.width, 1)
  const targetHeight = Math.max(bounds.height, 1)
  const desiredScale = Math.min(
    viewBox.width * 0.86 / targetWidth,
    viewBox.height * 0.86 / targetHeight,
  )
  const scale = Math.min(5, Math.max(1, desiredScale))
  const centerX = bounds.x + bounds.width / 2
  const centerY = bounds.y + bounds.height / 2
  const transform = zoomIdentity
    .translate(
      viewBox.x + viewBox.width / 2 - centerX * scale,
      viewBox.y + viewBox.height / 2 - centerY * scale,
    )
    .scale(scale)

  select(svg)
    .transition()
    .duration(280)
    .call(zoomBehavior.transform, transform)
}

function getElementsBoundsInZoomLayer(
  elements: SVGGraphicsElement[],
  zoomLayer: SVGGElement,
): { x: number; y: number; width: number; height: number } | null {
  const zoomMatrix = zoomLayer.getCTM()
  if (!zoomMatrix || elements.length === 0) return null

  let inverseZoomMatrix: DOMMatrix
  try {
    inverseZoomMatrix = zoomMatrix.inverse()
  } catch {
    return null
  }

  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY

  elements.forEach((element) => {
    const elementMatrix = element.getCTM()
    if (!elementMatrix) return
    const matrix = inverseZoomMatrix.multiply(elementMatrix)
    const bbox = element.getBBox()
    const corners = [
      [bbox.x, bbox.y],
      [bbox.x + bbox.width, bbox.y],
      [bbox.x, bbox.y + bbox.height],
      [bbox.x + bbox.width, bbox.y + bbox.height],
    ]

    corners.forEach(([x, y]) => {
      const transformedX = matrix.a * x! + matrix.c * y! + matrix.e
      const transformedY = matrix.b * x! + matrix.d * y! + matrix.f
      minX = Math.min(minX, transformedX)
      minY = Math.min(minY, transformedY)
      maxX = Math.max(maxX, transformedX)
      maxY = Math.max(maxY, transformedY)
    })
  })

  if (![minX, minY, maxX, maxY].every(Number.isFinite)) return null
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY }
}

function scrollPlainCfgToElements(elements: SVGGraphicsElement[]) {
  const container = graphContainer.value
  if (!container || cfgMode.value !== 'plain' || container.scrollWidth <= container.clientWidth) return

  const rects = elements.map(element => element.getBoundingClientRect())
  if (rects.length === 0) return
  const containerRect = container.getBoundingClientRect()
  const contentLeft = container.scrollLeft
    + Math.min(...rects.map(rect => rect.left))
    - containerRect.left
  const contentRight = container.scrollLeft
    + Math.max(...rects.map(rect => rect.right))
    - containerRect.left
  const targetCenter = (contentLeft + contentRight) / 2
  const maxScrollLeft = Math.max(0, container.scrollWidth - container.clientWidth)
  container.scrollTo({
    left: Math.min(maxScrollLeft, Math.max(0, targetCenter - container.clientWidth / 2)),
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
  })
}

function followPlainPlaybackBlock(blockId: BlockId | null, cutoffStep: number | null) {
  if (cfgMode.value !== 'plain') return
  const directNode = blockId === null ? null : `node_${String(blockId)}`
  const blocks = buildPlainStepBlocks()
  const containingNode = cutoffStep === null ? null : findPlainNodeByStep(cutoffStep, blocks)
  const latestNode = (directNode && nodeNameToEl.value.has(directNode) ? directNode : null) ?? containingNode ?? blocks
    .filter(block => cutoffStep !== null && block.startStep <= cutoffStep)
    .sort((left, right) => right.startStep - left.startStep)[0]?.nodeName
  if (!latestNode) return

  const element = nodeNameToEl.value.get(latestNode)
  if (!(element instanceof SVGGraphicsElement)) return
  scrollPlainCfgToElements([element])
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
    const isReached = isPlaybackNodeVisible(nodeName)
    node.classList.toggle('playback-hidden', !isReached)
    node.setAttribute('aria-hidden', String(!isReached))
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

  const hidePlaybackClusters = props.playbackCutoffStep !== null && currentInformationReady.value
  svg.querySelectorAll('.cluster').forEach((cluster) => {
    cluster.classList.toggle('playback-hidden', hidePlaybackClusters)
    cluster.setAttribute('aria-hidden', String(hidePlaybackClusters))
  })

  svg.querySelectorAll('.edge').forEach((edge) => {
    const title = getEdgeTitle(edge)
    const isReached = isPlaybackEdgeVisible(title)
    edge.classList.toggle('playback-hidden', !isReached)
    edge.setAttribute('aria-hidden', String(!isReached))
    const hasVisibleSelection = visibleNodes.value.size > 0
    const matchesVisibleSelection =
      !hasVisibleSelection || visibleEdges.value.has(title)
    const matchesEdgeType =
      activeEdgeType.value === null || getEdgeType(edge) === activeEdgeType.value

    if (matchesVisibleSelection && matchesEdgeType) {
      edge.classList.remove('filtered-out')
    } else {
      edge.classList.add('filtered-out')
    }
  })

  updateSwapHighlightOverlayVisibility()
}

function resetZoom() {
  fitGraphToViewport({ animate: true })
}

function resetFilter() {
  activeEdgeType.value = null
  emit('cfg-navigate', null)
  if (!props.filteredEdgeIds?.length && !props.selectedStepRange && !props.highlightedBlockId?.length) {
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

function extractQuotedField(instr: string, field: string): string | null {
  const single = instr.match(new RegExp(`'${field}'\\s*:\\s*'([^']*)'`))
  if (single?.[1] !== undefined) return single[1]
  const double = instr.match(new RegExp(`"${field}"\\s*:\\s*"([^"]*)"`))
  if (double?.[1] !== undefined) return double[1]
  return null
}

function extractScalarField(instr: string, field: string): string | null {
  const quoted = extractQuotedField(instr, field)
  if (quoted !== null) return quoted
  const single = instr.match(new RegExp(`'${field}'\\s*:\\s*([^,}\\s]+)`))
  if (single?.[1] !== undefined) return single[1]
  const double = instr.match(new RegExp(`"${field}"\\s*:\\s*([^,}\\s]+)`))
  if (double?.[1] !== undefined) return double[1]
  return null
}

function isBoundaryInstruction(instr: string): boolean {
  return /['"]is_boundary['"]\s*:\s*(True|true)/.test(instr)
}

function formatInstruction(instr: string): string {
  const tupleMatch = instr.match(/^\(\s*([^,]+)\s*,\s*'([^']+)'\s*\)$/)
  if (tupleMatch?.[1] !== undefined && tupleMatch[2] !== undefined) {
    const pc = tupleMatch[1].trim().replace(/^['"]|['"]$/g, '')
    return `${pc}  ${tupleMatch[2]}`
  }

  const dictOpcode = extractQuotedField(instr, 'opcode')
  const dictPc = extractScalarField(instr, 'pc')
  if (dictOpcode && dictPc) return `${dictPc}  ${dictOpcode}`

  return instr
}

function addrToName(addr: string): string {
  if (!addr) return addr
  const name = addressNameMap.value.get(addr.toLowerCase())
  if (name) return name
  if (addr.length < 12) return addr
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

function formatUnits(value: string, decimals = 18): string {
  if (!value) return '0'
  try {
    const raw = BigInt(value)
    const negative = raw < 0n
    const abs = negative ? -raw : raw
    const safeDecimals = Number.isFinite(decimals) ? Math.max(0, Math.trunc(decimals)) : 18
    const base = 10n ** BigInt(safeDecimals)
    const whole = abs / base
    const fraction = abs % base

    if (fraction === 0n) return addThousandsSeparators(`${negative ? '-' : ''}${whole.toString()}`)

    let fractionText = fraction.toString().padStart(safeDecimals, '0').replace(/0+$/, '')
    if (fractionText.length > 6) {
      fractionText = fractionText.slice(0, 6).replace(/0+$/, '')
    }
    if (!fractionText) return addThousandsSeparators(`${negative ? '-' : ''}${whole.toString()}.000000`)

    return addThousandsSeparators(`${negative ? '-' : ''}${whole.toString()}.${fractionText}`)
  } catch {
    return value
  }
}

function addThousandsSeparators(value: string): string {
  const negative = value.startsWith('-')
  const unsigned = negative ? value.slice(1) : value
  const [whole, fraction] = unsigned.split('.')
  if (whole === undefined) return value
  const withSeparators = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${negative ? '-' : ''}${withSeparators}${fraction !== undefined ? `.${fraction}` : ''}`
}

function isPositiveButBelowDisplay(value: string, decimals = 18, precision = 6): boolean {
  try {
    const raw = BigInt(value)
    if (raw <= 0n) return false
    const safeDecimals = Number.isFinite(decimals) ? Math.max(0, Math.trunc(decimals)) : 18
    if (safeDecimals <= precision) return false
    const threshold = 10n ** BigInt(safeDecimals - precision)
    return raw < threshold
  } catch {
    return false
  }
}

function formatTokenAmount(value: string, decimals = 18, token?: string): string {
  if (isPositiveButBelowDisplay(value, decimals)) {
    return token ? `<0.000001 ${token}` : '<0.000001'
  }

  const formatted = formatUnits(value, decimals)
  if (formatted === '0') return token ? `0 ${token}` : '0'
  if (formatted === value) return token ? `${value} ${token}` : value

  return token ? `${formatted} ${token}` : formatted
}

function hexToEth(hex: string): string {
  return formatTokenAmount(hex, 18, 'ETH')
}

function buildActionDisplays(action: BlockAction, index: number): ActionDisplay[] {
  const prefix = `Action ${index + 1}`

  if (action.action_type === 'eth_transfer' && action.eth_event) {
    return [{
      key: `${index}-eth`,
      title: `${prefix} · Send ETH`,
      tone: 'send',
      details: [
        { label: 'From', value: addrToName(action.eth_event.from) },
        { label: 'To', value: addrToName(action.eth_event.to) },
        { label: 'Amount', value: hexToEth(action.eth_event.amount) },
      ],
    }]
  }

  if (action.erc20_events && action.erc20_events.length > 0) {
    return action.erc20_events.map((ev, i) => {
      const isRead = ev.type === 'read'
      const token = ev.tokenname || 'ERC20'
      const amount = formatTokenAmount(ev.balance, ev.decimals ?? 18)
      const suffix = action.erc20_events.length > 1 ? `.${i + 1}` : ''
      return {
        key: `${index}-erc20-${i}`,
        title: `${prefix}${suffix} · ${isRead ? 'Read balance' : 'Write balance'}`,
        tone: isRead ? 'read' : 'write',
        details: [
          { label: 'Token', value: token },
          { label: 'Account', value: addrToName(ev.user || '') },
          { label: isRead ? 'Balance read' : 'Balance written', value: `${amount} ${token}` },
        ],
      }
    })
  }

  return [{
    key: `${index}-other`,
    title: `${prefix} · ${action.action_type}`,
    tone: 'other',
    details: [],
  }]
}

function formatGas(gas: number | null | undefined): string {
  if (gas === null || gas === undefined) return 'Unknown'
  return Number(gas).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function formatFoldedStepRange(stepRange: StepRange): string {
  return `${stepRange.start_step}–${stepRange.end_step}`
}

function hasStepRange(block: BlockInformation): boolean {
  return block.start_step !== undefined || block.end_step !== undefined
}

function formatStepValue(step: number | null | undefined): string {
  if (step === null || step === undefined || Number.isNaN(Number(step))) return 'Unknown'
  return String(step)
}

function formatStepRange(startStep: number | null | undefined, endStep: number | null | undefined): string {
  return `${formatStepValue(startStep)} - ${formatStepValue(endStep)}`
}

const edgeTypes: Array<{ type: CfgEdgeType, color: string, desc: string, aliases: string[] }> = [
  { type: 'NORMAL', color: CFG_EDGE_COLORS.NORMAL, desc: 'Non-terminating opcodes', aliases: [CFG_EDGE_COLORS.NORMAL, '#939393', '#607d8b', '#c2cad7'] },
  { type: 'JUMP', color: CFG_EDGE_COLORS.JUMP, desc: 'JUMP, JUMPI', aliases: [CFG_EDGE_COLORS.JUMP, '#242424', '#5b4747', '#d8d2ca'] },
  { type: 'CALL', color: CFG_EDGE_COLORS.CALL, desc: 'CALL, CALLCODE, STATICCALL', aliases: [CFG_EDGE_COLORS.CALL, '#1f6800', '#a9c7ae'] },
  { type: 'DELEGATECALL', color: CFG_EDGE_COLORS.DELEGATECALL, desc: 'DELEGATECALL', aliases: [CFG_EDGE_COLORS.DELEGATECALL, '#009dff', '#abc0d9'] },
  { type: 'TERMINATE', color: CFG_EDGE_COLORS.TERMINATE, desc: 'RETURN, STOP, REVERT, INVALID, SELFDESTRUCT', aliases: [CFG_EDGE_COLORS.TERMINATE, '#c14a00', '#dabaae'] },
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
  if (focusFrameId !== null) {
    window.cancelAnimationFrame(focusFrameId)
    focusFrameId = null
  }
})
</script>

<template>
  <div class="cfg-panel">
    <span class="panel-label">
      (E) Control Flow Graph
      <span class="mode-badge" :class="cfgMode">{{ cfgModeBadge }}</span>
      <button
        class="cfg-mode-toggle"
        :disabled="cfgMode === 'folded' && (!plainReady || plainLoading)"
        @click="toggleCfgMode"
      >
        {{ plainLoading && cfgMode === 'folded' ? 'Loading Plain CFG…' : toggleButtonLabel }}
      </button>
      <span
        class="edge-info-icon"
        :class="{ active: showEdgeTypes || !!activeEdgeType }"
        @mouseenter="openEdgeTypesTooltip"
        @mouseleave="queueCloseEdgeTypesTooltip"
      >
        <svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.2" />
          <text x="7" y="11.1" text-anchor="middle" font-size="9" font-weight="600" fill="currentColor">?</text>
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
    <div class="graph-actions">
      <GraphFitButton
        graph-name="control flow graph"
        @fit="resetZoom"
      />
      <GraphExpandButton
        graph-name="control flow graph"
        :expanded="props.expanded"
        @toggle="emit('toggle-expanded')"
      />
    </div>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading control flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="cfg-container">
      <div class="graph-stage">
        <div
          v-if="!currentInformationReady"
          class="information-pending"
          :class="{ error: !!currentInformationError }"
        >
          {{ currentInformationError || 'CFG topology is ready · block information is still being computed' }}
        </div>
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
        <div
          v-if="hasSelection"
          class="side-panel"
          :class="{ 'side-panel-folded': cfgMode === 'folded', 'side-panel-plain': cfgMode === 'plain' }"
        >
          <div class="panel-section-header">information</div>
          <div class="information-content">
            <template v-if="selectedBlockInfo">
              <div :id="`block-${selectedBlockInfo.block_id}`" class="information-stack">
                <div class="info-card info-card-overview">
                  <div class="info-card-header">
                    <span class="info-card-title">Overview</span>
                  </div>
                  <div class="info-row">
                    <span class="info-label">ID</span>
                    <span class="info-value">{{ selectedBlockInfo.block_id }}</span>
                  </div>
                  <div class="info-row">
                    <span class="info-label">Contract</span>
                    <span class="info-value" :title="selectedBlockInfo.address">{{ addrToName(selectedBlockInfo.address) }}</span>
                  </div>
                  <div v-if="cfgMode === 'folded'" class="info-row info-row-steps">
                    <span class="info-label">Steps</span>
                    <div class="step-ranges">
                      <span v-if="selectedFoldedStepRanges.length === 0" class="info-value">Unknown</span>
                      <span
                        v-for="(stepRange, index) in visibleFoldedStepRanges"
                        :key="`${stepRange.start_step}-${stepRange.end_step}-${index}`"
                        class="info-value step-range"
                      >{{ formatFoldedStepRange(stepRange) }}</span>
                      <button
                        v-if="selectedFoldedStepRanges.length > 1"
                        type="button"
                        class="step-ranges-toggle"
                        :aria-expanded="stepRangesExpanded"
                        :aria-label="stepRangesExpanded ? 'Collapse step ranges' : 'Expand step ranges'"
                        :title="stepRangesExpanded ? 'Collapse step ranges' : 'Expand step ranges'"
                        @click="stepRangesExpanded = !stepRangesExpanded"
                      >
                        <svg viewBox="-7 -7 14 14" aria-hidden="true">
                          <circle cx="0" cy="0" r="6" />
                          <path :d="stepRangesExpanded ? 'M -3.5 -2 L 0 3 L 3.5 -2 Z' : 'M -2.25 -3.5 L 3 0 L -2.25 3.5 Z'" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  <div v-if="cfgMode === 'plain' && hasStepRange(selectedBlockInfo)" class="info-row">
                    <span class="info-label">Step</span>
                    <span class="info-value">{{ formatStepRange(selectedBlockInfo.start_step, selectedBlockInfo.end_step) }}</span>
                  </div>
                  <div class="info-row">
                    <span class="info-label">Gas</span>
                    <span class="info-value">{{ formatGas(selectedBlockInfo.gas) }}</span>
                  </div>
                </div>

                <div v-if="selectedActionDisplays.length > 0" class="info-card">
                  <div class="info-card-header">
                    <span class="info-card-title">Actions</span>
                    <span class="info-card-badge">{{ selectedActionDisplays.length }}</span>
                  </div>
                  <div class="actions-list">
                    <div
                      v-for="action in selectedActionDisplays"
                      :key="action.key"
                      class="action-card"
                      :class="`tone-${action.tone}`"
                    >
                      <div class="action-title">{{ action.title }}</div>
                      <div
                        v-for="detail in action.details"
                        :key="`${action.key}-${detail.label}`"
                        class="action-detail-row"
                      >
                        <span class="action-detail-label">{{ detail.label }}</span>
                        <span class="action-detail-value">{{ detail.value }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <div v-if="cfgMode === 'plain'" class="info-card llm-analysis-group">
                  <div class="info-card-header llm-header-row">
                    <span class="info-card-title">LLM Analysis</span>
                    <span v-if="llmAnalysisResponse?.source === 'cache'" class="llm-cache-badge">cached</span>
                  </div>
                  <div v-if="llmAnalysisState === 'loading'" class="llm-status">
                    Analyzing block intent...
                  </div>
                  <div v-else-if="llmAnalysisState === 'error'" class="llm-status error">
                    {{ llmAnalysisError }}
                  </div>
                  <div
                    v-else-if="llmAnalysisState === 'success' && llmAnalysisResponse"
                    class="llm-analysis-content"
                  >
                    <div class="llm-title">{{ llmAnalysisResponse.analysis.title }}</div>
                    <div class="llm-description">{{ llmAnalysisResponse.analysis.description }}</div>
                  </div>
                </div>

                <div v-if="(selectedBlockInfo.instructions?.length ?? 0) > 0" class="info-card">
                  <div class="info-card-header">
                    <span class="info-card-title">Instructions</span>
                  </div>
                  <div class="block-section selected">
                    <div class="block-instructions">
                      <div
                        v-for="(instr, idx) in (selectedBlockInfo.instructions || []).filter(i => !isBoundaryInstruction(i))"
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
  color: #000000;
  font-weight: 700;
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

.cfg-mode-toggle:disabled {
  cursor: wait;
  opacity: 0.58;
}

.graph-actions {
  position: absolute;
  top: 4px;
  right: 8px;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 8px;
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

.cfg-panel.graph-panel-expanded .cfg-container {
  padding-right: calc(var(--expanded-legend-width) + 6px);
}

.cfg-panel.graph-panel-expanded .graph-actions {
  right: calc(var(--expanded-legend-width) + 14px);
}

.cfg-panel.graph-panel-expanded .side-panel {
  position: absolute;
  top: calc(50% + 3px);
  right: 0;
  bottom: 0;
  width: var(--expanded-legend-width);
  border-top: 1px solid rgba(123, 143, 173, 0.24);
  z-index: 20;
}

.graph-stage {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}

.information-pending {
  position: absolute;
  top: 36px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 12;
  padding: 5px 10px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: 999px;
  background: rgba(255, 251, 235, 0.94);
  color: #92400e;
  font-size: 10px;
  white-space: nowrap;
  pointer-events: none;
}

.information-pending.error {
  border-color: rgba(220, 38, 38, 0.35);
  background: rgba(254, 242, 242, 0.96);
  color: #991b1b;
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

.graph-viewport :deep(.node.action-node ellipse),
.graph-viewport :deep(.node.action-node polygon),
.graph-viewport :deep(.node.action-node path),
.graph-viewport :deep(.node.action-node rect) {
  filter: drop-shadow(0 0 12px rgba(239, 68, 68, 0.82)) drop-shadow(0 0 28px rgba(248, 113, 113, 0.48)) drop-shadow(0 0 52px rgba(252, 165, 165, 0.28));
}

.graph-viewport :deep(.call-signature-label) {
  fill: #000000;
  font-size: 28px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-weight: 800;
  paint-order: stroke;
  stroke: rgba(255, 255, 255, 0.85);
  stroke-width: 4px;
  stroke-linejoin: round;
  letter-spacing: 0.2px;
  pointer-events: none;
}

.graph-viewport :deep(.node.filtered-out),
.graph-viewport :deep(.edge.filtered-out) {
  opacity: 0.08;
  pointer-events: none;
}

.graph-viewport :deep(.node),
.graph-viewport :deep(.edge) {
  transition: none;
}

.graph-viewport :deep(.node.playback-hidden),
.graph-viewport :deep(.edge.playback-hidden),
.graph-viewport :deep(.cluster.playback-hidden) {
  opacity: 0 !important;
  pointer-events: none !important;
}

@media (prefers-reduced-motion: reduce) {
  .graph-viewport :deep(.node),
  .graph-viewport :deep(.edge) {
    transition: none;
  }
}

.graph-viewport :deep(.node.highlighted) {
  filter: drop-shadow(0 0 9px rgba(121, 136, 160, 0.38));
}

.graph-viewport :deep(.node.highlighted ellipse),
.graph-viewport :deep(.node.highlighted polygon),
.graph-viewport :deep(.node.highlighted path),
.graph-viewport :deep(.node.highlighted rect) {
  stroke-width: 3;
  filter: brightness(0.96);
}

.graph-viewport :deep(.node.selected) {
  filter: drop-shadow(0 0 12px rgba(145, 164, 194, 0.55));
}

.graph-viewport :deep(.node.selected ellipse),
.graph-viewport :deep(.node.selected polygon),
.graph-viewport :deep(.node.selected path),
.graph-viewport :deep(.node.selected rect) {
  stroke: #7B8FAD;
  stroke-width: 3.4;
}

.graph-viewport :deep(#swap-highlight-layer .swap-highlight-box) {
  fill: rgba(255, 59, 48, 0.12);
  stroke: #b91c1c;
  stroke-width: 2.8;
  filter: drop-shadow(0 0 5px rgba(220, 38, 38, 0.22));
}

.graph-viewport :deep(#swap-highlight-layer .swap-highlight-box.hidden) {
  display: none;
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
  background: linear-gradient(180deg, rgba(248, 250, 252, 0.96), rgba(241, 245, 249, 0.96));
  border-left: 1px solid rgba(123, 143, 173, 0.24);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  min-height: 0;
  width: 324px;
  box-sizing: border-box;
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
  padding: 10px 12px 8px;
  font-size: 10px;
  color: #1E2130;
  font-weight: 700;
  letter-spacing: 0.6px;
  border-bottom: 1px solid rgba(123, 143, 173, 0.2);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.78), rgba(255, 255, 255, 0.4));
  flex-shrink: 0;
  text-transform: uppercase;
}

.information-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 10px;
  min-height: 0;
}

.information-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.info-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.9);
  box-shadow: 0 8px 18px rgba(15, 23, 42, 0.06);
}

.info-card-overview {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(248, 250, 252, 0.92));
}

.info-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.info-card-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.45px;
  color: #1e293b;
  text-transform: uppercase;
}

.info-card-badge {
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: rgba(123, 143, 173, 0.14);
  color: #51637d;
  font-size: 9px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.info-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
  font-size: 10px;
  align-items: flex-start;
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

.info-row-steps {
  align-items: flex-start;
}

.step-ranges {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
  min-width: 0;
}

.step-range {
  display: block;
  white-space: nowrap;
}

.step-ranges-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
}

.step-ranges-toggle svg {
  display: block;
  width: 14px;
  height: 14px;
}

.step-ranges-toggle circle {
  fill: rgba(255, 255, 255, 0.9);
  stroke: #7b8fad;
  stroke-width: 1;
}

.step-ranges-toggle path {
  fill: #475569;
}

.step-ranges-toggle:hover circle {
  fill: #eef2ff;
  stroke: var(--accent);
}

.step-ranges-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.llm-analysis-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.llm-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.llm-cache-badge {
  border-radius: 999px;
  background: rgba(34, 197, 94, 0.15);
  color: #166534;
  font-size: 9px;
  line-height: 1;
  padding: 2px 6px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.llm-status {
  font-size: 10px;
  color: #64748b;
  line-height: 1.45;
}

.llm-status.error {
  color: #b91c1c;
}

.llm-analysis-content {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.llm-title {
  font-size: 11px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.35;
}

.llm-description {
  font-size: 10px;
  color: #475569;
  line-height: 1.55;
  white-space: pre-wrap;
}

.summary-line {
  margin-top: 4px;
  font-size: 10px;
  line-height: 1.45;
  color: #475569;
  white-space: pre-wrap;
  word-break: break-word;
}

.action-line,
.instruction-line {
  font-family: 'Consolas', 'Monaco', monospace;
}

.actions-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.action-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 9px 10px;
  border-radius: 10px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  background: #f8fafc;
}

.action-card.tone-read {
  background: linear-gradient(180deg, rgba(239, 246, 255, 0.92), rgba(248, 250, 252, 0.94));
  border-color: rgba(96, 165, 250, 0.24);
}

.action-card.tone-write {
  background: linear-gradient(180deg, rgba(250, 245, 255, 0.92), rgba(248, 250, 252, 0.95));
  border-color: rgba(148, 163, 184, 0.28);
}

.action-card.tone-send {
  background: linear-gradient(180deg, rgba(236, 253, 245, 0.94), rgba(248, 250, 252, 0.95));
  border-color: rgba(52, 211, 153, 0.24);
}

.action-title {
  font-size: 10px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.4;
}

.action-detail-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.action-detail-label {
  flex-shrink: 0;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.2px;
  text-transform: uppercase;
  color: #64748b;
}

.action-detail-value {
  font-size: 10px;
  color: #334155;
  text-align: right;
  word-break: break-word;
  font-family: 'Consolas', 'Monaco', monospace;
}

.block-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.block-section.selected {
  padding: 2px 0 0;
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

.instruction-line.instruction-boundary {
  color: #8698B2;
  font-weight: 600;
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
  transform: translateY(3px);
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
  background: rgba(134, 152, 178, 0.10);
  color: #6B7D98;
  font-size: 10px;
  cursor: pointer;
}
</style>
