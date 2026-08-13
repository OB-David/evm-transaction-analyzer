<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  fetchAddressBalances,
  fetchArbitrageResult,
  fetchCallTreeEdgeLink,
  fetchBlockInformation,
  fetchCallTreeData,
  fetchTfgSvg,
  fetchEdgeLink,
  fetchLegendData,
  type AfgNavigationTarget,
  type ArbitrageCycle,
  type BlockId,
  type CallTreeEdgeLink,
  type CallTreeEntry,
  type CfgMode,
  type EdgeLink,
  type BlockInformation,
} from '../api/analyze'
import { THEME_DARK_COLORS, getDarkAccentForColor, getFillColorForColor } from '../visualTheme'
import GraphExpandButton from './GraphExpandButton.vue'
import GraphFitButton from './GraphFitButton.vue'

const props = defineProps<{
  txHash: string | null
  cfgMode: CfgMode
  linkedCallTreeTarget: AfgNavigationTarget | null
  isAnalyzing: boolean
  playbackReady: boolean
  expanded: boolean
}>()

const emit = defineEmits<{
  'graph-navigate': [target: AfgNavigationTarget | null]
  'playback-change': [state: {
    cutoffStep: number | null
    active: boolean
    visibleBlockIds: BlockId[] | null
    visibleCallIds: number[] | null
    currentBlockId: BlockId | null
  }]
  'toggle-expanded': []
}>()

type PlaybackStep = {
  kind: 'cfg' | 'call' | 'transfer'
  cutoffStep: number
  visibleBlockIds: BlockId[]
  visibleCallIds: number[]
  visibleTransferOrder: number
  currentBlockId: BlockId | null
}

const edgeLinks = ref<EdgeLink[]>([])
const callTreeLinks = ref<CallTreeEdgeLink[]>([])
const plainBlocks = ref<BlockInformation[]>([])
const callTreeEntries = ref<CallTreeEntry[]>([])
const selectedEdgeId = ref<number | null>(null)
const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const linksLoading = ref(false)
const mappingNotice = ref('')

const graphContainer = ref<HTMLElement | null>(null)
let disposeSvgViewport: (() => void) | null = null
let resetSvgViewport: (() => void) | null = null
let loadRequestId = 0
let linkRequestId = 0
let playbackDataRequestId = 0
let playbackTimer: number | null = null
let playbackRunId = 0
let playbackTargetIndex: number | null = null
const AUTO_PLAY_INTERVAL_MS = 90

const playbackSteps = ref<PlaybackStep[]>([])
const playbackIndex = ref(0)
const isPlaybackRunning = ref(false)
const isPlaybackActivated = ref(false)
const currentPlaybackStep = computed(() => playbackSteps.value[playbackIndex.value] ?? null)
const transferStepIndexes = computed(() => playbackSteps.value.flatMap(
  (step, index) => step.kind === 'transfer' ? [index] : [],
))
const completedTransferCount = computed(() => (
  transferStepIndexes.value.filter(index => index <= playbackIndex.value).length
))
const previousTransferIndex = computed(() => (
  [...transferStepIndexes.value].reverse().find(index => index < playbackIndex.value) ?? null
))
const nextTransferIndex = computed(() => (
  transferStepIndexes.value.find(index => index > playbackIndex.value) ?? null
))
const playbackPhaseLabel = computed(() => {
  if (currentPlaybackStep.value?.kind === 'cfg') return 'CFG'
  if (currentPlaybackStep.value?.kind === 'call') return 'CFG+CALL'
  if (currentPlaybackStep.value?.kind === 'transfer') return 'TFG'
  return ''
})
const playbackPosition = computed(() => playbackSteps.value.length > 0
  ? `${playbackPhaseLabel.value} ${completedTransferCount.value}/${transferStepIndexes.value.length}`
  : '0/0')
const playbackUnavailable = computed(() => (
  !props.playbackReady || status.value !== 'success' || playbackSteps.value.length <= 1
))

// 套利相关状态
const isArbitrage = ref(false)
const arbCycles = ref<number[][]>([])
const detailedArbCycles = ref<ArbitrageCycle[]>([])
const arbOrders = ref<Set<number>>(new Set())
const activeCycleIndex = ref<number | null>(null)
const arbShowOnly = ref(false)
// 套利边经过的节点 title 集合
const arbNodeTitles = ref<Set<string>>(new Set())

// 地址余额数据：地址(小写) -> { token -> 净变化量 }
const addressBalances = ref<Record<string, Record<string, number>>>({})
// display name(小写) -> 地址(小写) 的反向映射，用于节点名匹配
const nameToAddress = ref<Record<string, string>>({})
const nameToColor = ref<Record<string, string>>({})
const tokenAddressToName = ref<Record<string, string>>({})

const cycleCount = computed(() => arbCycles.value.length)
const activeCycleOrders = computed(() => {
  if (activeCycleIndex.value === null) return new Set(arbOrders.value)
  return new Set(arbCycles.value[activeCycleIndex.value] || [])
})
const activeDetailedCycle = computed(() => {
  if (activeCycleIndex.value === null) return null
  return detailedArbCycles.value[activeCycleIndex.value] || null
})
const displayedArbitrageAsset = computed(() => {
  if (cycleCount.value > 1 && activeCycleIndex.value === null) return ''
  const cycle = activeDetailedCycle.value
  if (!cycle) return 'Unknown token'
  const path = cycle.token_address_path || []
  const finalToken = cycle.arbitrage_token_address || path[path.length - 1]
  return finalToken ? displayTokenAddress(finalToken) : 'Unknown token'
})

// tooltip 状态
const tooltipVisible = ref(false)
const tooltipX = ref(0)
const tooltipY = ref(0)
const tooltipName = ref('')
const tooltipBalances = ref<Record<string, number>>({})

watch(() => props.txHash, (newHash) => {
  loadRequestId += 1
  linkRequestId += 1
  playbackDataRequestId += 1
  disposeSvgViewport?.()
  disposeSvgViewport = null
  resetSvgViewport = null
  resetPlayback()
  if (newHash) {
    loadAfgData(newHash, props.cfgMode)
  } else {
    status.value = 'idle'
    edgeLinks.value = []
    callTreeLinks.value = []
    plainBlocks.value = []
    callTreeEntries.value = []
    if (graphContainer.value) graphContainer.value.innerHTML = ''
  }
}, { immediate: true })

watch([() => props.playbackReady, () => props.txHash], ([ready, txHash]) => {
  if (ready && txHash) void loadPlaybackSupportData(txHash)
}, { immediate: true })

watch(() => props.cfgMode, async (newMode) => {
  const txHash = props.txHash
  if (!txHash || status.value !== 'success') return
  const requestId = ++linkRequestId
  linksLoading.value = true
  mappingNotice.value = ''
  try {
    const links = await fetchEdgeLink(txHash, newMode)
    if (requestId === linkRequestId && props.txHash === txHash && props.cfgMode === newMode) {
      edgeLinks.value = links
      linksLoading.value = false
      if (selectedEdgeId.value !== null) {
        navigateToCfgForEdge(selectedEdgeId.value)
      }
    }
  } catch (e) {
    if (requestId === linkRequestId) {
      linksLoading.value = false
      mappingNotice.value = `Failed to load ${newMode} CFG mapping.`
      selectedEdgeId.value = null
      applySelectedEdgeStyle()
      emit('graph-navigate', null)
      console.warn('Failed to update AFG edge mapping:', e)
    }
  }
})

watch(() => props.linkedCallTreeTarget, (target) => {
  if (!target && !linksLoading.value) {
    selectedEdgeId.value = null
    mappingNotice.value = ''
    applySelectedEdgeStyle()
  }
})

async function loadAfgData(txHash: string, cfgMode: CfgMode) {
  const requestId = loadRequestId
  status.value = 'loading'
  errorMsg.value = ''
  linksLoading.value = false
  mappingNotice.value = ''
  selectedEdgeId.value = null
  tooltipVisible.value = false

  isArbitrage.value = false
  arbCycles.value = []
  detailedArbCycles.value = []
  arbOrders.value = new Set()
  activeCycleIndex.value = null
  arbShowOnly.value = false
  arbNodeTitles.value = new Set()
  addressBalances.value = {}
  nameToAddress.value = {}   // ← 重置
  nameToColor.value = {}
  tokenAddressToName.value = {}

  try {
    const [svg, links, callLinks, arb, balances, legend] = await Promise.all([
      fetchTfgSvg(txHash),
      fetchEdgeLink(txHash, cfgMode),
      fetchCallTreeEdgeLink(txHash),
      fetchArbitrageResult(txHash),
      fetchAddressBalances(txHash),
      fetchLegendData(txHash),
    ])
    if (requestId !== loadRequestId || props.txHash !== txHash) return

    edgeLinks.value = props.cfgMode === cfgMode
      ? links
      : await fetchEdgeLink(txHash, props.cfgMode)
    callTreeLinks.value = callLinks
    if (requestId !== loadRequestId || props.txHash !== txHash) return

    isArbitrage.value = arb.is_arbitrage
    detailedArbCycles.value = arb.selected_cycles || []
    arbCycles.value = detailedArbCycles.value.length > 0
      ? detailedArbCycles.value.map(cycle => cycle.transfer_edge_orders)
      : arb.cycles
    arbOrders.value = new Set(
      arb.arb_edge_orders.length > 0 ? arb.arb_edge_orders : arbCycles.value.flat(),
    )
    activeCycleIndex.value = arbCycles.value.length === 1 ? 0 : null

    // 地址统一转小写
    addressBalances.value = Object.fromEntries(
      Object.entries(balances).map(([addr, tokens]) => [addr.toLowerCase(), tokens])
    )

    // ← 建立 displayName -> address 反向映射
    const nameMap: Record<string, string> = {}
    const colorMap: Record<string, string> = {}
    const tokenNameMap: Record<string, string> = { eth: 'ETH' }
    const allEntries = [
      ...legend.user_addresses,
      ...legend.erc20_tokens,
      ...legend.normal_contracts,
    ]
    for (const entry of allEntries) {
      if (entry.name && entry.address) {
        nameMap[entry.name.toLowerCase()] = entry.address.toLowerCase()
        if (entry.color) {
          colorMap[entry.name.toLowerCase()] = entry.color
        }
      }
    }
    nameToAddress.value = nameMap
    nameToColor.value = colorMap
    for (const entry of legend.erc20_tokens) {
      if (entry.address && entry.name) tokenNameMap[entry.address.toLowerCase()] = entry.name
    }
    tokenAddressToName.value = tokenNameMap

    await nextTick()
    await renderGraph(svg, requestId)
    if (requestId === loadRequestId) status.value = 'success'
  } catch (e: any) {
    if (requestId !== loadRequestId) return
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load AFG data'
  }
}

async function loadPlaybackSupportData(txHash: string) {
  const requestId = ++playbackDataRequestId
  try {
    const [blockInformation, callTree] = await Promise.all([
      fetchBlockInformation(txHash, 'plain'),
      fetchCallTreeData(txHash),
    ])
    if (requestId !== playbackDataRequestId || props.txHash !== txHash) return
    plainBlocks.value = Object.values(blockInformation)
    callTreeEntries.value = callTree.calls
    if (status.value === 'success') initializePlayback()
  } catch (e) {
    if (requestId === playbackDataRequestId) {
      console.warn('Failed to load AFG playback support data:', e)
    }
  }
}

async function renderGraph(svgSource: string, requestId: number) {
  if (!graphContainer.value || !svgSource) return

  try {
    const container = graphContainer.value
    disposeSvgViewport?.()
    container.innerHTML = svgSource
    const svg = container.querySelector<SVGSVGElement>('svg')
    if (!svg) throw new Error('TFG SVG is missing its root element')
    const viewport = installSvgViewport(svg)
    disposeSvgViewport = viewport.dispose
    resetSvgViewport = viewport.reset
    if (requestId === loadRequestId) attachInteractivity()
  } catch (e) {
    console.error('Graphviz render error:', e)
    errorMsg.value = 'Failed to render graph'
    status.value = 'error'
  }
}

function installSvgViewport(svg: SVGSVGElement) {
  const values = (svg.getAttribute('viewBox') || '').split(/\s+/).map(Number)
  const base = values.length === 4 && values.every(Number.isFinite)
    ? values as [number, number, number, number]
    : [0, 0, Number(svg.getAttribute('width')) || 1, Number(svg.getAttribute('height')) || 1] as [number, number, number, number]
  let current: [number, number, number, number] = [...base]
  let drag: { x: number; y: number; viewBox: [number, number, number, number] } | null = null

  const apply = () => svg.setAttribute('viewBox', current.join(' '))
  const reset = () => {
    current = [...base]
    apply()
  }
  const onWheel = (event: WheelEvent) => {
    event.preventDefault()
    const rect = svg.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    const scale = Math.exp(event.deltaY * 0.001)
    const x = current[0] + (event.clientX - rect.left) / rect.width * current[2]
    const y = current[1] + (event.clientY - rect.top) / rect.height * current[3]
    const width = Math.min(base[2] * 8, Math.max(base[2] * 0.08, current[2] * scale))
    const height = width * base[3] / base[2]
    const ratioX = (x - current[0]) / current[2]
    const ratioY = (y - current[1]) / current[3]
    current = [x - ratioX * width, y - ratioY * height, width, height]
    apply()
  }
  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0) return
    if ((event.target as Element).closest('.edge, .node')) return
    drag = { x: event.clientX, y: event.clientY, viewBox: [...current] }
    svg.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: PointerEvent) => {
    if (!drag) return
    const rect = svg.getBoundingClientRect()
    current = [
      drag.viewBox[0] - (event.clientX - drag.x) / rect.width * drag.viewBox[2],
      drag.viewBox[1] - (event.clientY - drag.y) / rect.height * drag.viewBox[3],
      drag.viewBox[2],
      drag.viewBox[3],
    ]
    apply()
  }
  const onPointerUp = () => { drag = null }
  svg.addEventListener('wheel', onWheel, { passive: false })
  svg.addEventListener('pointerdown', onPointerDown)
  svg.addEventListener('pointermove', onPointerMove)
  svg.addEventListener('pointerup', onPointerUp)
  svg.addEventListener('pointercancel', onPointerUp)
  reset()
  return {
    reset,
    dispose: () => {
      svg.removeEventListener('wheel', onWheel)
      svg.removeEventListener('pointerdown', onPointerDown)
      svg.removeEventListener('pointermove', onPointerMove)
      svg.removeEventListener('pointerup', onPointerUp)
      svg.removeEventListener('pointercancel', onPointerUp)
    },
  }
}

function attachInteractivity() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  const nodes = svg.querySelectorAll('.node')
  nodes.forEach((node) => {
    const nodeEl = node as SVGElement
    harmonizeNodeAppearance(nodeEl)

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
    harmonizeEdgeAppearance(edgeEl)

    const textEls = edge.querySelectorAll('text')
    const machineOrder = Number(edgeEl.dataset.edgeOrder)
    let edgeId: number | null = Number.isFinite(machineOrder) ? machineOrder : null
    if (edgeId === null) {
      textEls.forEach(t => {
        if (edgeId !== null) return
        const m = (t.textContent || '').match(/\((\d+)\)/)
        if (m) edgeId = parseInt(m[1]!, 10)
      })
    }

    if (edgeId !== null) {
      const capturedId = edgeId
      edgeEl.setAttribute('data-edge-order', String(edgeId))

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
        if (el.getAttribute('pointer-events') === 'none') {
          el.setAttribute('fill', 'transparent')
          el.setAttribute('pointer-events', 'all')
        }
        el.addEventListener('click', (e) => {
          e.stopPropagation()
          handleEdgeClick(capturedId)
        })
        el.addEventListener('mouseenter', () => edgeEl.classList.add('hovered'))
        el.addEventListener('mouseleave', () => edgeEl.classList.remove('hovered'))
      })
    }
  })

  applyArbCycleStyles()
  initializePlayback()
}

function getEdgeOrder(edge: Element): number | null {
  const machineOrder = Number((edge as HTMLElement).dataset.edgeOrder)
  if (Number.isFinite(machineOrder)) return machineOrder
  const label = Array.from(edge.querySelectorAll('text'))
    .map(text => text.textContent || '')
    .join(' ')
  const match = label.match(/\((\d+)\)/)
  if (!match?.[1]) return null
  const order = Number(match[1])
  return Number.isFinite(order) ? order : null
}

function getTransferCutoff(order: number, previousCutoff: number): number {
  if (order === 0) return 0
  const link = callTreeLinks.value.find(item => item.edge_id === order)
    ?? edgeLinks.value.find(item => item.edge_id === order)
  const evidenceSteps = (link?.evidence || [])
    .map(item => Number(item.source_step))
    .filter(Number.isFinite)
  return evidenceSteps.length > 0 ? Math.max(previousCutoff, ...evidenceSteps) : previousCutoff
}

function finiteStep(value: unknown, fallback = 0): number {
  const step = Number(value)
  return Number.isFinite(step) ? step : fallback
}

function getCallRevealStep(call: CallTreeEntry, blocks: BlockInformation[]): number {
  const entryStep = finiteStep(call.entry_step)
  const exitStep = finiteStep(call.exit_step, entryStep)
  const firstChildEntry = callTreeEntries.value
    .filter(candidate => candidate.parent_call_id === call.call_id)
    .reduce(
      (earliest, child) => Math.min(earliest, finiteStep(child.entry_step, exitStep)),
      exitStep,
    )
  const initialContractSegmentEnd = Math.min(exitStep, firstChildEntry)
  const targetAddress = call.to_address.toLowerCase()
  const blocksInCall = blocks.filter(block => {
    const startStep = finiteStep(block.start_step, Number.POSITIVE_INFINITY)
    return startStep >= entryStep
      && startStep <= initialContractSegmentEnd
      && block.address.toLowerCase() === targetAddress
  })
  const fallbackBlocks = blocks.filter(block => {
    const startStep = finiteStep(block.start_step, Number.POSITIVE_INFINITY)
    return startStep >= entryStep && startStep <= initialContractSegmentEnd
  })
  const candidates = blocksInCall.length > 0 ? blocksInCall : fallbackBlocks
  const matchingBlock = candidates[0]
  return matchingBlock ? finiteStep(matchingBlock.start_step, entryStep) : entryStep
}

function buildPlaybackTimeline(orders: number[]): PlaybackStep[] {
  const blocks = [...plainBlocks.value]
    .filter(block => Number.isFinite(Number(block.start_step)))
    .sort((left, right) => (
      finiteStep(left.start_step) - finiteStep(right.start_step)
      || String(left.block_id).localeCompare(String(right.block_id), undefined, { numeric: true })
    ))
  const calls = [...callTreeEntries.value]
    .map(call => ({ call, revealStep: getCallRevealStep(call, blocks) }))
    .sort((left, right) => left.revealStep - right.revealStep || left.call.call_id - right.call.call_id)

  const visibleBlocks = new Set<BlockId>()
  const visibleCalls = new Set<number>()
  const timeline: PlaybackStep[] = []
  let visibleTransferOrder = -1
  let previousCutoff = -1
  let currentBlockId: BlockId | null = null

  const appendStep = (kind: PlaybackStep['kind'], cutoffStep: number) => {
    timeline.push({
      kind,
      cutoffStep,
      visibleBlockIds: [...visibleBlocks],
      visibleCallIds: [...visibleCalls],
      visibleTransferOrder,
      currentBlockId,
    })
  }

  const transactionEndStep = Math.max(
    0,
    ...blocks.map(block => Math.max(
      finiteStep(block.start_step),
      finiteStep(block.end_step, finiteStep(block.start_step)),
    )),
    ...callTreeEntries.value.map(call => finiteStep(call.exit_step)),
  )

  orders.forEach((order, orderIndex) => {
    const transferCutoff = getTransferCutoff(order, Math.max(0, previousCutoff))
    const callLink = callTreeLinks.value.find(link => link.edge_id === order)
    const matchedCallIds = new Set(
      (callLink?.matched_calls || [])
        .map(match => match.call_id)
        .filter((callId): callId is number => callId !== null),
    )
    const matchedCallExit = callTreeEntries.value
      .filter(call => matchedCallIds.has(call.call_id))
      .reduce((latest, call) => Math.max(latest, finiteStep(call.exit_step)), transferCutoff)
    const linkedCompletionStep = Math.max(transferCutoff, matchedCallExit)
    const completionStep = orderIndex === orders.length - 1
      ? Math.max(linkedCompletionStep, transactionEndStep)
      : linkedCompletionStep

    const actions: Array<{
      kind: 'cfg' | 'call'
      step: number
      block?: BlockInformation
      call?: CallTreeEntry
    }> = []
    blocks.forEach(block => {
      const step = finiteStep(block.start_step)
      if (!visibleBlocks.has(block.block_id) && step <= completionStep) {
        actions.push({ kind: 'cfg', step, block })
      }
    })
    calls.forEach(({ call, revealStep }) => {
      if (!visibleCalls.has(call.call_id) && revealStep <= completionStep) {
        actions.push({ kind: 'call', step: revealStep, call })
      }
    })
    actions.sort((left, right) => (
      left.step - right.step
      || (left.kind === right.kind ? 0 : left.kind === 'cfg' ? -1 : 1)
    ))

    for (let actionIndex = 0; actionIndex < actions.length;) {
      const step = actions[actionIndex]!.step
      const actionsAtStep: typeof actions = []
      while (actionIndex < actions.length && actions[actionIndex]!.step === step) {
        actionsAtStep.push(actions[actionIndex]!)
        actionIndex += 1
      }

      let revealsCall = false
      actionsAtStep.forEach(action => {
        if (action.kind === 'cfg' && action.block) {
          visibleBlocks.add(action.block.block_id)
          currentBlockId = action.block.block_id
        } else if (action.call) {
          visibleCalls.add(action.call.call_id)
          revealsCall = true
        }
      })
      appendStep(revealsCall ? 'call' : 'cfg', step)
    }

    visibleTransferOrder = order
    appendStep('transfer', completionStep)
    previousCutoff = completionStep
  })

  return timeline
}

function initializePlayback() {
  const svg = graphContainer.value?.querySelector('svg')
  if (!svg) return

  const orders = [...new Set(
    Array.from(svg.querySelectorAll('.edge'))
      .map(edge => {
        const order = getEdgeOrder(edge)
        if (order !== null) (edge as HTMLElement).dataset.playbackOrder = String(order)
        return order
      })
      .filter((order): order is number => order !== null),
  )].sort((left, right) => left - right)

  playbackSteps.value = buildPlaybackTimeline(orders)
  playbackIndex.value = Math.max(0, playbackSteps.value.length - 1)
  applyPlaybackVisibility()
  emitPlaybackChange()
}

function resetPlayback() {
  stopPlayback()
  playbackSteps.value = []
  playbackIndex.value = 0
  isPlaybackActivated.value = false
  emit('playback-change', {
    cutoffStep: null,
    active: false,
    visibleBlockIds: null,
    visibleCallIds: null,
    currentBlockId: null,
  })
}

function emitPlaybackChange() {
  emit('playback-change', {
    cutoffStep: currentPlaybackStep.value?.cutoffStep ?? null,
    active: isPlaybackActivated.value,
    visibleBlockIds: isPlaybackActivated.value ? currentPlaybackStep.value?.visibleBlockIds ?? [] : null,
    visibleCallIds: isPlaybackActivated.value ? currentPlaybackStep.value?.visibleCallIds ?? [] : null,
    currentBlockId: isPlaybackActivated.value ? currentPlaybackStep.value?.currentBlockId ?? null : null,
  })
}

function clearLinkedSelectionForPlayback() {
  if (selectedEdgeId.value === null && !props.linkedCallTreeTarget) return
  selectedEdgeId.value = null
  mappingNotice.value = ''
  applySelectedEdgeStyle()
  emit('graph-navigate', null)
}

function setPlaybackIndex(nextIndex: number, stopAutomatic = false) {
  if (playbackSteps.value.length === 0) return
  if (stopAutomatic) stopPlayback()
  const boundedIndex = Math.min(playbackSteps.value.length - 1, Math.max(0, nextIndex))
  isPlaybackActivated.value = true
  playbackIndex.value = boundedIndex
  clearLinkedSelectionForPlayback()
  applyPlaybackVisibility()
  emitPlaybackChange()
  if (currentPlaybackStep.value?.kind === 'transfer') {
    nextTick(() => fitVisibleGraphToViewport())
  }
}

function stepBackward() {
  if (previousTransferIndex.value !== null) {
    setPlaybackIndex(previousTransferIndex.value, true)
  }
}

function stepForward() {
  if (isPlaybackRunning.value && playbackTargetIndex !== null) {
    const targetIndex = playbackTargetIndex
    setPlaybackIndex(targetIndex, true)
    return
  }
  if (nextTransferIndex.value !== null) {
    animatePlaybackTo(nextTransferIndex.value)
  }
}

function animatePlaybackTo(targetIndex: number) {
  stopPlayback()
  const direction = Math.sign(targetIndex - playbackIndex.value)
  if (direction === 0) return

  playbackTargetIndex = targetIndex
  const runId = playbackRunId
  isPlaybackRunning.value = true
  const scheduleNextFrame = () => {
    playbackTimer = window.setTimeout(() => {
      playbackTimer = null
      if (runId !== playbackRunId) return

      const nextIndex = playbackIndex.value + direction
      setPlaybackIndex(nextIndex)
      if (nextIndex === targetIndex) {
        stopPlayback()
        return
      }

      nextTick(() => {
        window.requestAnimationFrame(() => {
          if (runId === playbackRunId) scheduleNextFrame()
        })
      })
    }, AUTO_PLAY_INTERVAL_MS)
  }
  scheduleNextFrame()
}

function togglePlayback() {
  if (playbackUnavailable.value) return
  if (isPlaybackRunning.value) {
    stopPlayback()
    return
  }
  if (playbackIndex.value >= playbackSteps.value.length - 1) {
    setPlaybackIndex(0)
  }
  animatePlaybackTo(playbackSteps.value.length - 1)
}

function stopPlayback() {
  playbackRunId += 1
  playbackTargetIndex = null
  if (playbackTimer !== null) {
    window.clearTimeout(playbackTimer)
    playbackTimer = null
  }
  isPlaybackRunning.value = false
}

function applyPlaybackVisibility() {
  const svg = graphContainer.value?.querySelector('svg')
  const currentOrder = currentPlaybackStep.value?.visibleTransferOrder
  if (!svg || currentOrder === undefined) return

  const visibleNodeTitles = new Set<string>()
  svg.querySelectorAll('.edge').forEach(edge => {
    const order = Number((edge as HTMLElement).dataset.playbackOrder)
    const isVisible = Number.isFinite(order) && order <= currentOrder
    edge.classList.toggle('playback-hidden', !isVisible)
    edge.setAttribute('aria-hidden', String(!isVisible))
    if (!isVisible) return
    const title = edge.querySelector('title')?.textContent?.trim() || ''
    title.split('->').forEach(nodeTitle => {
      const normalized = nodeTitle.trim()
      if (normalized) visibleNodeTitles.add(normalized)
    })
  })

  svg.querySelectorAll('.node').forEach(node => {
    const title = node.querySelector('title')?.textContent?.trim() || ''
    const isVisible = visibleNodeTitles.has(title)
    node.classList.toggle('playback-hidden', !isVisible)
    node.setAttribute('aria-hidden', String(!isVisible))
  })
  hideTooltip()
}

function fitVisibleGraphToViewport() {
  fitGraphToViewport()
}

function harmonizeNodeAppearance(nodeEl: SVGElement) {
  const shape = nodeEl.querySelector<SVGElement>('ellipse, polygon, rect, path')
  if (!shape) return

  const fill = getFillColorForColor(shape.getAttribute('fill'))
  const stroke = getDarkAccentForColor(shape.getAttribute('fill'))

  shape.setAttribute('fill', fill)
  shape.setAttribute('stroke', stroke)
  shape.setAttribute('stroke-width', '1.6')

  nodeEl.querySelectorAll<SVGTextElement>('text').forEach((textEl) => {
    textEl.setAttribute('fill', '#000000')
    textEl.style.fill = '#000000'
  })
}

function harmonizeEdgeAppearance(edgeEl: SVGElement) {
  const edgeText = Array.from(edgeEl.querySelectorAll<SVGTextElement>('text'))
  const label = edgeText.map(el => el.textContent || '').join(' ').trim()
  const tokenName = inferTokenNameFromLabel(edgeText, label)
  const accent = getDarkAccentForName(tokenName)
  const textColor = getThemeDarkTextColor(tokenName)

  edgeEl.querySelectorAll<SVGPathElement>('path:not(.hit-area)').forEach((path) => {
    if (path.getAttribute('fill') === 'none') {
      path.setAttribute('stroke', accent)
      path.style.stroke = accent
      path.style.strokeWidth = '1.8px'
    }
  })

  edgeEl.querySelectorAll<SVGPolygonElement>('polygon:not([pointer-events="none"])').forEach((polygon) => {
    polygon.setAttribute('stroke', accent)
    polygon.setAttribute('fill', accent)
    polygon.style.stroke = accent
    polygon.style.fill = accent
  })

  edgeText.forEach((textEl) => {
    textEl.setAttribute('fill', textColor)
    textEl.style.fill = textColor
  })
}

function inferTokenNameFromLabel(texts: SVGTextElement[], label: string): string | null {
  const firstLine = texts[0]?.textContent?.trim() || ''
  const firstLineMatch = firstLine.match(/^\(\d+\)\s+(.+)$/)
  if (firstLineMatch?.[1]) return firstLineMatch[1].trim()
  const match = label.match(/\)\s+(.+?)(?:\(|:)/)
  return match?.[1]?.trim() || null
}

function getDarkAccentForName(name: string | null) {
  if (!name) return '#6B7280'
  const lower = name.toLowerCase()
  if (lower === 'eth') return '#000000'
  return getDarkAccentForColor(nameToColor.value[lower], '#6B7280')
}

function getThemeDarkTextColor(name: string | null) {
  if (!name) return THEME_DARK_COLORS[THEME_DARK_COLORS.length - 1] ?? '#6B7280'

  const lower = name.toLowerCase()
  if (lower === 'eth') return '#000000'
  const mappedColor = nameToColor.value[lower]
  const resolved = getDarkAccentForColor(mappedColor, '')
  if (resolved) return resolved

  let hash = 0
  for (let i = 0; i < lower.length; i += 1) {
    hash = ((hash << 5) - hash + lower.charCodeAt(i)) | 0
  }

  return THEME_DARK_COLORS[Math.abs(hash) % THEME_DARK_COLORS.length] ?? '#6B7280'
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

function displayTokenAddress(address: string): string {
  const normalized = address.toLowerCase()
  const name = tokenAddressToName.value[normalized]
  if (name) return name
  if (normalized === 'eth') return 'ETH'
  return address.length > 14 ? `${address.slice(0, 8)}…${address.slice(-4)}` : address
}

function selectCycle(index: number | null) {
  activeCycleIndex.value = index
  selectedEdgeId.value = null
  mappingNotice.value = ''
  applySelectedEdgeStyle()
  emit('graph-navigate', null)
  applyArbCycleStyles()
  nextTick(() => fitGraphToViewport())
}

function previousCycle() {
  if (cycleCount.value === 0) return
  const current = activeCycleIndex.value ?? 0
  selectCycle((current - 1 + cycleCount.value) % cycleCount.value)
}

function nextCycle() {
  if (cycleCount.value === 0) return
  const current = activeCycleIndex.value ?? -1
  selectCycle((current + 1) % cycleCount.value)
}

function toggleArbShowOnly() {
  arbShowOnly.value = !arbShowOnly.value
  activeCycleIndex.value = cycleCount.value === 1 ? 0 : null
  applyArbCycleStyles()
}

function applyArbCycleStyles() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  const showOnly = arbShowOnly.value && isArbitrage.value
  const highlightedOrders = activeCycleOrders.value
  const highlightedNodes = new Set<string>()

  svg.querySelectorAll('.edge').forEach((edgeEl) => {
    const order = Number((edgeEl as HTMLElement).dataset.edgeOrder)
    const keep = Number.isFinite(order) && highlightedOrders.has(order)
    edgeEl.classList.toggle('arb-highlight', keep)
    edgeEl.classList.toggle('arb-muted', showOnly && !keep)
    if (!keep) return
    const edgeTitle = edgeEl.querySelector('title')?.textContent?.trim() || ''
    edgeTitle.split('->').forEach(part => {
      const title = part.trim()
      if (title) highlightedNodes.add(title)
    })
  })

  arbNodeTitles.value = highlightedNodes

  svg.querySelectorAll('.node').forEach(nodeEl => {
    const titleEl = nodeEl.querySelector('title')
    const nodeTitle = titleEl?.textContent?.trim() || ''
    const keep = highlightedNodes.has(nodeTitle)
    nodeEl.classList.toggle('arb-muted', showOnly && !keep)
  })
}

function handleEdgeClick(edgeId: number) {
  if (linksLoading.value) return

  if (selectedEdgeId.value === edgeId) {
    selectedEdgeId.value = null
    mappingNotice.value = ''
    applySelectedEdgeStyle()
    emit('graph-navigate', null)
    return
  }

  selectedEdgeId.value = edgeId
  applySelectedEdgeStyle()
  navigateToCfgForEdge(edgeId)
}

function navigateToCfgForEdge(edgeId: number) {
  const link = edgeLinks.value.find(l => l.edge_id === edgeId)
  const callTreeLink = callTreeLinks.value.find(l => l.edge_id === edgeId)
  const notices: string[] = []
  if (!link) notices.push(`No ${props.cfgMode} CFG mapping is available for edge ${edgeId}.`)
  else if (link.mapping_status === 'partial') notices.push('Only part of this transfer evidence could be mapped to the CFG.')
  else if (link.mapping_status === 'ambiguous') notices.push('This transfer matched multiple CFG nodes at the same step.')
  else if (link.mapping_status === 'unmatched') notices.push('This transfer could not be mapped to a CFG execution step.')

  if (!callTreeLink) notices.push(`No Call Tree mapping is available for edge ${edgeId}.`)
  else if (callTreeLink.mapping_status === 'partial') notices.push('Only part of this transfer evidence could be mapped to the Call Tree.')
  else if (callTreeLink.mapping_status === 'ambiguous') notices.push('This transfer matched ambiguous Call Tree frames.')
  else if (callTreeLink.mapping_status === 'unmatched') notices.push('This transfer could not be mapped to a Call Tree contract.')
  mappingNotice.value = notices.join(' ')

  const blockIds: BlockId[] = []

  if (typeof link?.matched_blocks === 'number' || typeof link?.matched_blocks === 'string') {
    blockIds.push(link.matched_blocks)
  } else if (Array.isArray(link?.matched_blocks)) {
    blockIds.push(...link.matched_blocks)
  } else if (link && typeof link.matched_blocks === 'object') {
    if (link.matched_blocks.sender) blockIds.push(...link.matched_blocks.sender)
    if (link.matched_blocks.receiver) blockIds.push(...link.matched_blocks.receiver)
  }

  const uniqueBlockIds = [...new Set(blockIds)]
  const callIds = [...new Set(
    (callTreeLink?.matched_calls || [])
      .map(match => match.call_id)
      .filter((callId): callId is number => callId !== null),
  )]
  const contractAddresses = [...new Set(
    (callTreeLink?.matched_contracts || []).map(address => address.toLowerCase()),
  )]
  const includesRootCall = (callTreeLink?.matched_calls || []).some(match => match.call_id === null)

  emit('graph-navigate', {
    blockIds: uniqueBlockIds,
    callIds,
    contractAddresses,
    includesRootCall,
  })
}

function applySelectedEdgeStyle() {
  const svg = graphContainer.value?.querySelector('svg')
  svg?.querySelectorAll('.edge').forEach((edge) => {
    edge.classList.toggle('selected', getEdgeOrder(edge) === selectedEdgeId.value)
  })
}

function fitGraphToViewport() {
  resetSvgViewport?.()
}

onBeforeUnmount(() => {
  stopPlayback()
  disposeSvgViewport?.()
})
</script>

<template>
  <div class="afg-panel">
    <span class="panel-label">(C) Token Flow Graph</span>
    <div class="graph-actions">
      <div class="playback-controls" role="group" aria-label="Execution playback controls">
        <button
          type="button"
          class="playback-button"
          aria-label="Previous execution step"
          title="Previous execution step"
          :disabled="playbackUnavailable || isPlaybackRunning || previousTransferIndex === null"
          @click="stepBackward"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M7 5v14M18 6l-8 6 8 6z" />
          </svg>
        </button>
        <button
          type="button"
          class="playback-button playback-toggle"
          :class="{ active: isPlaybackRunning }"
          :aria-label="isPlaybackRunning ? 'Pause execution playback' : 'Play execution automatically'"
          :title="isPlaybackRunning ? 'Pause playback' : 'Auto play execution'"
          :disabled="playbackUnavailable"
          @click="togglePlayback"
        >
          <svg v-if="isPlaybackRunning" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 5v14M16 5v14" />
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 5l11 7-11 7z" />
          </svg>
        </button>
        <button
          type="button"
          class="playback-button"
          :aria-label="isPlaybackRunning ? 'Skip to target transfer' : 'Next execution step'"
          :title="isPlaybackRunning ? 'Skip animation' : 'Next execution step'"
          :disabled="playbackUnavailable || (!isPlaybackRunning && nextTransferIndex === null)"
          @click="stepForward"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M17 5v14M6 6l8 6-8 6z" />
          </svg>
        </button>
        <span class="playback-position" aria-live="polite">{{ playbackPosition }}</span>
      </div>
      <GraphFitButton
        graph-name="token flow graph"
        @fit="fitGraphToViewport"
      />
      <GraphExpandButton
        graph-name="token flow graph"
        :expanded="props.expanded"
        @toggle="emit('toggle-expanded')"
      />
    </div>

    <div
      v-if="status === 'success' && isArbitrage"
      class="arb-circle-controls"
      role="group"
      aria-label="Arbitrage cycle controls"
    >
      <button
        type="button"
        class="arb-circle-button"
        :class="{ active: arbShowOnly }"
        :aria-pressed="arbShowOnly"
        @click="toggleArbShowOnly"
      >
        <span class="arb-circle-icon" aria-hidden="true">↻</span>
        Arbitrage circle
      </button>

      <template v-if="arbShowOnly && cycleCount > 1">
        <button
          type="button"
          class="arb-icon-button"
          aria-label="Previous arbitrage cycle"
          title="Previous cycle"
          @click="previousCycle"
        >‹</button>
        <button
          type="button"
          class="arb-icon-button"
          aria-label="Next arbitrage cycle"
          title="Next cycle"
          @click="nextCycle"
        >›</button>
        <span v-if="activeCycleIndex !== null" class="arb-cycle-position" aria-live="polite">
          {{ activeCycleIndex + 1 }}/{{ cycleCount }}
        </span>
      </template>
      <span v-if="displayedArbitrageAsset" class="arb-token" aria-live="polite">
        Token: <strong>{{ displayedArbitrageAsset }}</strong>
      </span>
    </div>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading asset flow graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div
      v-show="status === 'success' || status === 'loading'"
      class="afg-container"
      :class="{ 'links-loading': linksLoading }"
    >
      <div :key="txHash || 'idle'" ref="graphContainer" class="graph-viewport"></div>

      <div v-if="mappingNotice" class="mapping-notice" role="status">
        {{ mappingNotice }}
      </div>

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

    <div v-if="status === 'idle' && !isAnalyzing" class="placeholder">
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
  -webkit-user-select: none;
  user-select: none;
}

.panel-label {
  position: absolute;
  top: 8px;
  left: 12px;
  font-size: 11px;
  color: #000000;
  font-weight: 700;
  letter-spacing: 0.5px;
  z-index: 10;
}

.arb-circle-controls {
  position: absolute;
  top: 36px;
  right: 8px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: calc(100% - 16px);
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

.playback-controls {
  display: flex;
  align-items: center;
  gap: 4px;
}

.playback-button {
  position: relative;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid rgba(148, 163, 184, 0.55);
  border-radius: 4px;
  color: #475569;
  background: rgba(255, 255, 255, 0.94);
  cursor: pointer;
  touch-action: manipulation;
  transition: color 180ms ease, border-color 180ms ease, background 180ms ease;
}

.playback-button::before {
  content: '';
  position: absolute;
  inset: -8px;
}

.playback-button:hover:not(:disabled),
.playback-button.active {
  color: #334155;
  border-color: var(--accent);
  background: #f1f5f9;
}

.playback-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.playback-button:disabled {
  cursor: not-allowed;
  opacity: 0.38;
}

.playback-button svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  pointer-events: none;
}

.playback-position {
  min-width: 28px;
  color: #64748b;
  font-size: 9px;
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.arb-circle-button,
.arb-icon-button {
  height: 28px;
  border: 1px solid rgba(194, 65, 12, 0.38);
  border-radius: 5px;
  color: #9a3412;
  background: rgba(255, 247, 237, 0.96);
  font-size: 10px;
  cursor: pointer;
  transition: background 150ms ease, border-color 150ms ease;
  box-shadow: 0 2px 8px rgba(124, 45, 18, 0.1);
}

.arb-circle-button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  font-weight: 700;
  white-space: nowrap;
}

.arb-circle-icon {
  font-size: 15px;
  line-height: 1;
}

.arb-icon-button {
  width: 28px;
  padding: 0;
  font-size: 18px;
  line-height: 1;
}

.arb-icon-button:hover:not(:disabled),
.arb-circle-button:hover,
.arb-circle-button.active {
  color: #7c2d12;
  background: rgba(255, 237, 213, 0.98);
  border-color: rgba(194, 65, 12, 0.72);
}

.arb-icon-button:focus-visible,
.arb-circle-button:focus-visible {
  outline: 2px solid #c2410c;
  outline-offset: 2px;
}

.arb-icon-button:disabled {
  cursor: not-allowed;
  opacity: 0.38;
}

.arb-cycle-position {
  min-width: 27px;
  color: #7c2d12;
  font-size: 10px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.arb-token {
  max-width: 150px;
  padding: 5px 8px;
  overflow: hidden;
  color: #9a3412;
  background: rgba(255, 247, 237, 0.96);
  border: 1px solid rgba(194, 65, 12, 0.28);
  border-radius: 5px;
  box-shadow: 0 2px 8px rgba(124, 45, 18, 0.1);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.arb-token strong {
  color: #431407;
}

.afg-container {
  position: relative;
  flex: 1;
  min-height: 0;
  padding-top: 28px;
  overflow: hidden;
}

.afg-container.links-loading .graph-viewport {
  cursor: progress;
}

.mapping-notice {
  position: absolute;
  left: 12px;
  bottom: 10px;
  z-index: 25;
  max-width: calc(100% - 24px);
  padding: 6px 9px;
  border: 1px solid #d7a24a;
  border-radius: 4px;
  color: #7c4a03;
  background: rgba(255, 248, 230, 0.96);
  font-size: 10px;
  line-height: 1.35;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.12);
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
  transition: none;
}

.graph-viewport :deep(.node.hovered ellipse),
.graph-viewport :deep(.node.hovered polygon),
.graph-viewport :deep(.node.hovered path),
.graph-viewport :deep(.node.hovered rect) {
  stroke-width: 2;
  filter: brightness(1.05);
}

.graph-viewport :deep(.edge) {
  transition: none;
}

.graph-viewport :deep(.node.playback-hidden),
.graph-viewport :deep(.edge.playback-hidden) {
  opacity: 0 !important;
  pointer-events: none !important;
}

.graph-viewport :deep(.edge.selected path:not(.hit-area)) {
  stroke-width: 4px !important;
  filter: drop-shadow(0 0 2px rgba(59, 89, 152, 0.75));
}

.graph-viewport :deep(.edge.selected polygon) {
  stroke-width: 2.5px !important;
  filter: drop-shadow(0 0 2px rgba(59, 89, 152, 0.75));
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
  stroke-width: 2.6;
  filter: brightness(1.2);
}

.graph-viewport :deep(.edge.arb-highlight path:not(.hit-area)) {
  stroke-width: 2.8px;
}

.graph-viewport :deep(.edge.arb-muted),
.graph-viewport :deep(.node.arb-muted) {
  opacity: 0;
}

.graph-viewport :deep(.edge.arb-muted .hit-area),
.graph-viewport :deep(.edge.arb-muted polygon),
.graph-viewport :deep(.node.arb-muted) {
  pointer-events: none;
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

@media (prefers-reduced-motion: reduce) {
  .playback-button,
  .graph-viewport :deep(.node),
  .graph-viewport :deep(.edge) {
    transition: none;
  }
}
</style>
