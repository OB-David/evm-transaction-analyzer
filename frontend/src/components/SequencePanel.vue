<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { select } from 'd3-selection'
import { zoom, zoomIdentity } from 'd3-zoom'
import {
  fetchLegendData,
  fetchCallTreeData,
  type AfgNavigationTarget,
  type LegendData,
} from '../api/analyze'
import {
  buildCallTreeModel,
  collapsedAtDepth,
  isDescendantOf,
  type CallTreeFrame,
  type CallTreeModel,
} from '../utils/callTree'
import { CFG_EDGE_COLORS, getDarkAccentForColor, getFillColorForColor } from '../visualTheme'
import GraphExpandButton from './GraphExpandButton.vue'
import GraphFitButton from './GraphFitButton.vue'

const props = defineProps<{
  txHash: string | null
  isAnalyzing: boolean
  selectedStepRange: { entryStep: number; exitStep: number } | null
  linkedCallTreeTarget: AfgNavigationTarget | null
  playbackCutoffStep: number | null
  playbackActive: boolean
  playbackVisibleCallIds: number[] | null
  expanded: boolean
}>()

const emit = defineEmits<{
  'sequence-select': [stepRange: { entryStep: number; exitStep: number } | null]
  'toggle-expanded': []
}>()

const ROOT_KEY = 'transaction-root'
const COLUMN_WIDTH = 208
const NODE_WIDTH = 180
const NODE_HEIGHT = 44
const ROW_HEIGHT = 52
const PADDING = 22
const TOKEN_CONTENT_LEFT = Math.ceil((NODE_WIDTH - NODE_WIDTH / Math.SQRT2) / 2)
const TOKEN_CONTENT_RIGHT = NODE_WIDTH - TOKEN_CONTENT_LEFT
const TOKEN_CONTENT_TOP = Math.ceil((NODE_HEIGHT - NODE_HEIGHT / Math.SQRT2) / 2)
const TOKEN_CONTENT_BOTTOM = NODE_HEIGHT - TOKEN_CONTENT_TOP

interface TreeNodeLayout {
  key: string
  frame: CallTreeFrame | null
  virtual: 'user' | 'contract' | null
  depth: number
  x: number
  y: number
}

interface TreeEdgeLayout {
  key: string
  parentKey: string
  childKey: string
  frame: CallTreeFrame | null
  kind: 'root' | 'call'
}

const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const model = shallowRef<CallTreeModel | null>(null)
const legendData = shallowRef<LegendData | null>(null)
const collapsedIds = ref<Set<number>>(new Set())
const collapsedToRoot = ref(false)
const selectedCallId = ref<number | null>(null)
const linkedCallIds = computed(() => new Set(props.linkedCallTreeTarget?.callIds || []))
const hasLinkedFilter = computed(() => Boolean(
  props.linkedCallTreeTarget
  && (props.linkedCallTreeTarget.callIds.length > 0 || props.linkedCallTreeTarget.includesRootCall),
))
const linkedContextIds = computed(() => {
  const context = new Set<number>()
  const currentModel = model.value
  if (!currentModel) return context

  linkedCallIds.value.forEach(callId => {
    let frame = currentModel.frameById.get(callId)
    while (frame) {
      context.add(frame.call_id)
      frame = frame.parentCallId === null
        ? undefined
        : currentModel.frameById.get(frame.parentCallId)
    }
  })
  return context
})
const sequenceContainer = ref<HTMLElement | null>(null)
const graphViewport = ref<HTMLElement | null>(null)
const treeSvg = ref<SVGSVGElement | null>(null)
const treeZoomLayer = ref<SVGGElement | null>(null)
const treeViewBox = ref('0 0 720 320')
const popupRef = ref<HTMLElement | null>(null)
const popupPosition = ref({ left: 12, top: 12 })
const popupAnchor = ref<{ x: number; y: number } | null>(null)
const popupVisible = ref(false)
const signaturesExpanded = ref(false)
const calldataExpanded = ref(false)
const showEdgeLegend = ref(false)

const callTreeEdgeTypes = [
  { type: 'CALL', color: CFG_EDGE_COLORS.CALL, desc: 'CALL, CALLCODE, STATICCALL', dashed: false },
  { type: 'DELEGATECALL', color: CFG_EDGE_COLORS.DELEGATECALL, desc: 'DELEGATECALL', dashed: false },
  { type: 'REVERTED', color: CFG_EDGE_COLORS.TERMINATE, desc: 'Call frame exited with REVERT', dashed: true },
]

function isFrameReached(frame: CallTreeFrame): boolean {
  if (props.playbackActive && props.playbackVisibleCallIds !== null) {
    return props.playbackVisibleCallIds.includes(frame.call_id)
  }
  return props.playbackCutoffStep === null || frame.entry_step <= props.playbackCutoffStep
}

function reachedDescendantCount(frame: CallTreeFrame): number {
  const currentModel = model.value
  if (!currentModel) return 0
  return frame.childCallIds.reduce((total, childId) => {
    const child = currentModel.frameById.get(childId)
    if (!child || !isFrameReached(child)) return total
    return total + 1 + reachedDescendantCount(child)
  }, 0)
}

const reachedCallCount = computed(() => (
  model.value?.frames.filter(isFrameReached).length ?? 0
))

let resizeObserver: ResizeObserver | null = null
let zoomBehavior: any = null
let svgSelection: any = null
let fitFrameId: number | null = null
let loadRequestId = 0
let skipNextLayoutFit = false

const selectedFrame = computed(() => {
  if (!model.value || selectedCallId.value === null) return null
  return model.value.frameById.get(selectedCallId.value) || null
})

const legendLookup = computed(() => {
  const entryByAddress = new Map<string, { color?: string }>()
  const tokenAddresses = new Set<string>()
  const legend = legendData.value

  if (legend) {
    for (const entry of [...legend.erc20_tokens, ...legend.normal_contracts, ...legend.user_addresses]) {
      entryByAddress.set(normalizeAddress(entry.address), entry)
    }
    legend.erc20_tokens.forEach(entry => tokenAddresses.add(normalizeAddress(entry.address)))
  }
  return { entryByAddress, tokenAddresses }
})

const layout = computed(() => {
  const currentModel = model.value
  const nodes: TreeNodeLayout[] = []
  const edges: TreeEdgeLayout[] = []
  const nodeByKey = new Map<string, TreeNodeLayout>()
  let nextLeaf = 0
  let maxDepth = 0

  if (!currentModel) {
    return { nodes, edges, nodeByKey, revertedCallIds: new Set<number>(), width: 480, height: 220, visibleCalls: 0 }
  }

  const visit = (callId: number): number => {
    const frame = currentModel.frameById.get(callId)
    if (!frame) return PADDING
    maxDepth = Math.max(maxDepth, frame.depth)
    const visibleChildren = collapsedIds.value.has(callId)
      ? []
      : frame.childCallIds.filter(childId => {
        const child = currentModel.frameById.get(childId)
        return Boolean(child && isFrameReached(child))
      })
    let y: number

    if (visibleChildren.length === 0) {
      y = PADDING + nextLeaf * ROW_HEIGHT
      nextLeaf += 1
    } else {
      const childYs = visibleChildren.map(visit)
      // Keep the earliest execution path at the top of the viewport. Centering a
      // parent across hundreds of descendants leaves a large blank area above it.
      y = childYs[0]!
      visibleChildren.forEach(childId => {
        const child = currentModel.frameById.get(childId)
        if (child) {
          edges.push({
            key: `${callId}-${childId}`,
            parentKey: `call-${callId}`,
            childKey: `call-${childId}`,
            frame: child,
            kind: 'call',
          })
        }
      })
    }

    const node: TreeNodeLayout = {
      key: `call-${callId}`,
      frame,
      virtual: null,
      depth: frame.depth,
      x: PADDING + frame.depth * COLUMN_WIDTH,
      y,
    }
    nodes.push(node)
    nodeByKey.set(node.key, node)
    return y
  }

  const visibleRootCallIds = collapsedToRoot.value
    ? []
    : currentModel.rootCallIds.filter(callId => {
      const frame = currentModel.frameById.get(callId)
      return Boolean(frame && isFrameReached(frame))
    })
  const rootYs = visibleRootCallIds.map(visit)
  const rootY = rootYs[0] ?? PADDING
  const rootNode: TreeNodeLayout = {
    key: ROOT_KEY,
    frame: null,
    virtual: 'contract',
    depth: 0,
    x: PADDING,
    y: rootY,
  }
  nodes.push(rootNode)
  nodeByKey.set(ROOT_KEY, rootNode)

  visibleRootCallIds.forEach(callId => {
    const frame = currentModel.frameById.get(callId)
    if (frame) {
      edges.push({
        key: `${ROOT_KEY}-${callId}`,
        parentKey: ROOT_KEY,
        childKey: `call-${callId}`,
        frame,
        kind: 'call',
      })
    }
  })

  const revertedCallIds = new Set<number>()
  nodes.forEach(node => {
    const frame = node.frame
    if (!frame) return
    let current: CallTreeFrame | undefined = frame
    while (current) {
      if (current.exit_op.toUpperCase() === 'REVERT') {
        revertedCallIds.add(frame.call_id)
        break
      }
      current = current.parentCallId === null
        ? undefined
        : currentModel.frameById.get(current.parentCallId)
    }
  })

  const leafCount = Math.max(1, nextLeaf)
  return {
    nodes,
    edges,
    nodeByKey,
    revertedCallIds,
    width: Math.max(480, PADDING * 2 + maxDepth * COLUMN_WIDTH + NODE_WIDTH),
    height: Math.max(220, PADDING * 2 + (leafCount - 1) * ROW_HEIGHT + NODE_HEIGHT),
    visibleCalls: nodes.reduce((count, node) => count + (node.frame ? 1 : 0), 0),
  }
})

watch(() => props.txHash, (newHash) => {
  loadRequestId += 1
  resetPanelState()
  if (newHash) loadCallTreeData(newHash)
  else status.value = 'idle'
}, { immediate: true })

watch(() => props.selectedStepRange, (stepRange) => {
  if (!stepRange && selectedCallId.value !== null) clearSelection(false)
})

watch(() => props.playbackVisibleCallIds, (nextCallIds, previousCallIds) => {
  const currentModel = model.value
  if (!props.playbackActive || !currentModel || nextCallIds === null) return

  const previousIds = new Set(previousCallIds || [])
  const revealedCallIds = nextCallIds.filter(callId => !previousIds.has(callId))
  if (revealedCallIds.length === 0) return

  collapsedToRoot.value = false
  const nextCollapsedIds = new Set(collapsedIds.value)
  revealedCallIds.forEach(callId => {
    let frame = currentModel.frameById.get(callId)
    while (frame) {
      nextCollapsedIds.delete(frame.call_id)
      frame = frame.parentCallId === null
        ? undefined
        : currentModel.frameById.get(frame.parentCallId)
    }
  })
  collapsedIds.value = nextCollapsedIds
}, { deep: true })

watch(() => props.linkedCallTreeTarget, async (target) => {
  if (!model.value) return
  if (target) applyLinkedTreeFocus(target)
  else restoreDefaultFolding()
  await nextTick()
  queueFitTreeToViewport()
}, { deep: true })

watch(selectedCallId, () => {
  signaturesExpanded.value = false
  calldataExpanded.value = false
})

watch(
  () => [layout.value.width, layout.value.height, layout.value.visibleCalls],
  () => {
    if (skipNextLayoutFit) {
      skipNextLayoutFit = false
      return
    }
    queueFitTreeToViewport()
  },
)

onBeforeUnmount(() => {
  teardownZoom()
  resizeObserver?.disconnect()
  resizeObserver = null
})

async function loadCallTreeData(txHash: string) {
  const requestId = loadRequestId
  status.value = 'loading'
  errorMsg.value = ''

  try {
    const [mapping, legend] = await Promise.all([
      fetchCallTreeData(txHash),
      fetchLegendData(txHash).catch(() => null),
    ])
    if (requestId !== loadRequestId || props.txHash !== txHash) return

    const nextModel = buildCallTreeModel(mapping)
    model.value = nextModel
    legendData.value = legend
    if (props.linkedCallTreeTarget) {
      applyLinkedTreeFocus(props.linkedCallTreeTarget)
    } else restoreDefaultFolding()
    status.value = 'success'

    await nextTick()
    setupZoom()
    installResizeObserver()
    queueFitTreeToViewport()
  } catch (error: any) {
    if (requestId !== loadRequestId) return
    status.value = 'error'
    errorMsg.value = formatLoadError(error)
  }
}

function formatLoadError(error: { message?: string } | null | undefined) {
  const message = error?.message || 'Failed to load call tree'
  if (
    message.includes('404')
    && message.includes('call_tree.json')
  ) {
    return 'Call tree data is missing for this transaction.'
  }
  return message
}

function resetPanelState() {
  clearSelection(false)
  teardownZoom()
  resizeObserver?.disconnect()
  resizeObserver = null
  model.value = null
  legendData.value = null
  collapsedIds.value = new Set()
  collapsedToRoot.value = false
  errorMsg.value = ''
}

function installResizeObserver() {
  resizeObserver?.disconnect()
  if (!graphViewport.value) return
  resizeObserver = new ResizeObserver(() => {
    queueFitTreeToViewport()
  })
  resizeObserver.observe(graphViewport.value)
}

function setupZoom() {
  teardownZoom()
  if (!treeSvg.value || !treeZoomLayer.value) return

  updateTreeViewBox()
  svgSelection = select(treeSvg.value as Element)
  zoomBehavior = zoom()
    .scaleExtent([0.005, 8])
    .on('zoom', (event: any) => {
      if (treeZoomLayer.value) {
        select(treeZoomLayer.value).attr('transform', event.transform.toString())
      }
    })
  svgSelection.call(zoomBehavior as any)
}

function teardownZoom() {
  if (fitFrameId !== null) {
    window.cancelAnimationFrame(fitFrameId)
    fitFrameId = null
  }
  if (svgSelection) svgSelection.on('.zoom', null)
  zoomBehavior = null
  svgSelection = null
}

function updateTreeViewBox() {
  if (!graphViewport.value || !treeSvg.value) return null
  const width = Math.max(1, graphViewport.value.clientWidth)
  const height = Math.max(1, graphViewport.value.clientHeight)
  treeViewBox.value = `0 0 ${width} ${height}`
  treeSvg.value.setAttribute('viewBox', treeViewBox.value)
  return { width, height }
}

function queueFitTreeToViewport() {
  if (fitFrameId !== null) window.cancelAnimationFrame(fitFrameId)
  fitFrameId = window.requestAnimationFrame(() => {
    fitFrameId = null
    fitTreeToViewport()
  })
}

function fitTreeToViewport() {
  if (!treeZoomLayer.value || !svgSelection || !zoomBehavior) return
  const viewport = updateTreeViewBox()
  if (!viewport) return

  const linkedNodes = hasLinkedFilter.value
    ? layout.value.nodes.filter(node => !isTreeNodeMuted(node))
    : []
  const bounds = linkedNodes.length > 0
    ? {
        x: Math.min(...linkedNodes.map(node => node.x)),
        y: Math.min(...linkedNodes.map(node => node.y)),
        width: Math.max(...linkedNodes.map(node => node.x + NODE_WIDTH))
          - Math.min(...linkedNodes.map(node => node.x)),
        height: Math.max(...linkedNodes.map(node => node.y + NODE_HEIGHT))
          - Math.min(...linkedNodes.map(node => node.y)),
      }
    : treeZoomLayer.value.getBBox()
  if (bounds.width <= 0 || bounds.height <= 0) return
  const padding = 18
  const availableWidth = Math.max(1, viewport.width - padding * 2)
  const availableHeight = Math.max(1, viewport.height - padding * 2)
  const fitScale = Math.min(
    availableWidth / bounds.width,
    availableHeight / bounds.height,
  )
  const scale = Math.min(2.5, Math.max(0.005, fitScale))
  const x = (viewport.width - bounds.width * scale) / 2 - bounds.x * scale
  const y = (viewport.height - bounds.height * scale) / 2 - bounds.y * scale
  const transform = zoomIdentity.translate(x, y).scale(scale)
  svgSelection.call(zoomBehavior.transform as any, transform)

  window.requestAnimationFrame(repositionSelectedPopup)
}

function repositionSelectedPopup() {
  if (selectedCallId.value === null || !treeSvg.value || !sequenceContainer.value) return
  const node = treeSvg.value.querySelector<SVGGElement>(`[data-call-id="${selectedCallId.value}"]`)
  if (!node) return
  const nodeRect = node.getBoundingClientRect()
  const containerRect = sequenceContainer.value.getBoundingClientRect()
  popupAnchor.value = {
    x: nodeRect.right - containerRect.left,
    y: nodeRect.top + nodeRect.height / 2 - containerRect.top,
  }
  updatePopupPosition()
}

function toggleCall(callId: number) {
  const currentModel = model.value
  if (!currentModel) return
  skipNextLayoutFit = true
  const next = new Set(collapsedIds.value)

  if (next.has(callId)) {
    next.delete(callId)
  } else {
    next.add(callId)
    if (
      selectedCallId.value !== null
      && isDescendantOf(currentModel, selectedCallId.value, callId)
    ) {
      clearSelection()
    }
  }
  collapsedIds.value = next
}

function expandAll() {
  collapsedToRoot.value = false
  collapsedIds.value = new Set()
}

function restoreDefaultFolding() {
  const currentModel = model.value
  if (!currentModel) return
  collapsedToRoot.value = false
  collapsedIds.value = collapsedAtDepth(currentModel, 2)
}

function applyLinkedTreeFocus(target: AfgNavigationTarget) {
  const currentModel = model.value
  if (!currentModel) return

  if (target.callIds.length === 0) {
    if (!target.includesRootCall) {
      restoreDefaultFolding()
      return
    }
    collapsedToRoot.value = target.includesRootCall
    collapsedIds.value = new Set(
      currentModel.frames
        .filter(frame => frame.childCallIds.length > 0)
        .map(frame => frame.call_id),
    )
    return
  }

  collapsedToRoot.value = false
  const expandedAncestors = new Set<number>()
  target.callIds.forEach(callId => {
    let frame = currentModel.frameById.get(callId)
    while (frame?.parentCallId !== null && frame?.parentCallId !== undefined) {
      expandedAncestors.add(frame.parentCallId)
      frame = currentModel.frameById.get(frame.parentCallId)
    }
  })

  collapsedIds.value = new Set(
    currentModel.frames
      .filter(frame => frame.childCallIds.length > 0 && !expandedAncestors.has(frame.call_id))
      .map(frame => frame.call_id),
  )
}

function isTreeNodeMuted(node: TreeNodeLayout): boolean {
  if (!hasLinkedFilter.value) return false
  if (!node.frame || node.virtual) return false
  return !linkedContextIds.value.has(node.frame.call_id)
}

function isTreeEdgeMuted(edge: TreeEdgeLayout): boolean {
  if (edge.kind === 'root' || !edge.frame) return false
  return hasLinkedFilter.value && !linkedContextIds.value.has(edge.frame.call_id)
}

function collapseAll() {
  if (!model.value) return
  collapsedToRoot.value = true
  collapsedIds.value = new Set()
  if (selectedCallId.value !== null) clearSelection()
}

function toggleRoot() {
  const currentModel = model.value
  if (!currentModel) return

  if (collapsedToRoot.value) {
    // Root expansion reveals only its immediate calls. Each child then
    // controls the next level independently.
    collapsedToRoot.value = false
    collapsedIds.value = collapsedAtDepth(currentModel, 1)
    return
  }

  collapsedToRoot.value = true
  collapsedIds.value = new Set()
  if (selectedCallId.value !== null) clearSelection()
}

function handleRootClick() {
  closePopup()
}

function handleNodeClick(frame: CallTreeFrame, event?: MouseEvent) {
  if (selectedCallId.value === frame.call_id) {
    clearSelection()
    return
  }

  selectedCallId.value = frame.call_id
  popupVisible.value = true
  const containerRect = sequenceContainer.value?.getBoundingClientRect()
  if (containerRect && event) {
    popupAnchor.value = {
      x: event.clientX - containerRect.left,
      y: event.clientY - containerRect.top,
    }
  } else {
    popupAnchor.value = { x: 16, y: 16 }
  }

  emit('sequence-select', {
    entryStep: frame.entry_step,
    exitStep: frame.exit_step,
  })
  nextTick(updatePopupPosition)
}

function updatePopupPosition() {
  if (!popupAnchor.value || !popupRef.value || !sequenceContainer.value) return
  const containerRect = sequenceContainer.value.getBoundingClientRect()
  const popupWidth = popupRef.value.offsetWidth || 200
  const popupHeight = popupRef.value.offsetHeight || 300
  const margin = 12
  let left = popupAnchor.value.x + 14
  let top = popupAnchor.value.y + 14

  if (left + popupWidth > containerRect.width - margin) {
    left = Math.max(margin, popupAnchor.value.x - popupWidth - 14)
  }
  if (top + popupHeight > containerRect.height - margin) {
    top = Math.max(margin, containerRect.height - popupHeight - margin)
  }
  popupPosition.value = { left, top }
}

function clearSelection(shouldEmit = true) {
  selectedCallId.value = null
  popupVisible.value = false
  popupAnchor.value = null
  if (shouldEmit) emit('sequence-select', null)
}

function closePopup() {
  popupVisible.value = false
}

function toggleSignatures() {
  if (!selectedFrame.value?.probable_text_signatures?.length) return
  signaturesExpanded.value = !signaturesExpanded.value
  nextTick(updatePopupPosition)
}

function toggleCalldata() {
  if (!selectedFrame.value?.calldata.length) return
  calldataExpanded.value = !calldataExpanded.value
  nextTick(updatePopupPosition)
}

function normalizeAddress(address: string): string {
  return address.trim().toLowerCase()
}

function isToken(frame: CallTreeFrame): boolean {
  return legendLookup.value.tokenAddresses.has(normalizeAddress(frame.to_address))
}

function colorForAddress(address: string): string | undefined {
  return legendLookup.value.entryByAddress.get(normalizeAddress(address))?.color
}

function fillForAddress(address: string): string {
  return getFillColorForColor(colorForAddress(address), '#E2E2E2')
}

function strokeForFrame(frame: CallTreeFrame): string {
  if (frame.exit_op.toUpperCase() === 'REVERT') return '#C14A00'
  return getDarkAccentForColor(colorForAddress(frame.to_address), '#7B8794')
}

function rootStroke(): string {
  if (!model.value) return '#7B8794'
  return getDarkAccentForColor(colorForAddress(model.value.rootAddress), '#7B8794')
}

function nodeTextLeft(frame: CallTreeFrame): number {
  return isToken(frame) ? TOKEN_CONTENT_LEFT : 22
}

function nodeContractX(frame: CallTreeFrame): number {
  return isToken(frame) ? TOKEN_CONTENT_LEFT + 31 : 47
}

function nodeContractLabel(frame: CallTreeFrame): string {
  return clipped(frame.to_name, isToken(frame) ? 14 : 18)
}

function nodeSignatureLabel(frame: CallTreeFrame): string {
  return clipped(primarySignature(frame), isToken(frame) ? 20 : 24)
}

function foldControlX(frame: CallTreeFrame): number {
  return isToken(frame) ? TOKEN_CONTENT_RIGHT - 10 : NODE_WIDTH - 10
}

function foldControlY(frame: CallTreeFrame): number {
  return isToken(frame) ? TOKEN_CONTENT_BOTTOM - 8 : NODE_HEIGHT - 8
}

function foldedCountX(frame: CallTreeFrame): number {
  return foldControlX(frame) - 9
}

function foldedCountY(frame: CallTreeFrame): number {
  return foldControlY(frame) + 3
}

function connector(edge: TreeEdgeLayout): string {
  const parent = layout.value.nodeByKey.get(edge.parentKey)
  const child = layout.value.nodeByKey.get(edge.childKey)
  if (!parent || !child) return ''
  const startX = parent.x + NODE_WIDTH
  const startY = parent.y + NODE_HEIGHT / 2
  const endX = child.x
  const endY = child.y + NODE_HEIGHT / 2
  const middleX = (startX + endX) / 2
  return `M ${startX} ${startY} C ${middleX} ${startY}, ${middleX} ${endY}, ${endX} ${endY}`
}

function clipped(text: string, max = 26): string {
  return text.length > max ? `${text.slice(0, max - 3)}...` : text
}

function primarySignature(frame: CallTreeFrame): string {
  return frame.probable_text_signatures?.[0] || 'Unknown function'
}

function signatureSummary(frame: CallTreeFrame): string {
  const signatures = frame.probable_text_signatures || []
  if (signatures.length === 0) return 'No probable signature'
  return signatures.length === 1 ? signatures[0]! : `${signatures[0]}  +${signatures.length - 1}`
}

function entryClass(frame: CallTreeFrame): string {
  const op = frame.entry_op.toUpperCase()
  if (op === 'STATICCALL') return 'static'
  if (op === 'DELEGATECALL') return 'delegated'
  if (op === 'CALLCODE') return 'callcode'
  return 'call'
}

function edgeClass(edge: TreeEdgeLayout) {
  if (edge.kind === 'root' || !edge.frame) return { root: true }
  return {
    reverted: edge.frame.exit_op.toUpperCase() === 'REVERT',
    'reverted-subtree': layout.value.revertedCallIds.has(edge.frame.call_id),
  }
}

function edgeColor(edge: TreeEdgeLayout): string {
  if (edge.kind === 'root' || !edge.frame) return '#111827'
  if (edge.frame.exit_op.toUpperCase() === 'REVERT') return CFG_EDGE_COLORS.TERMINATE
  if (edge.frame.entry_op.toUpperCase() === 'DELEGATECALL') return CFG_EDGE_COLORS.DELEGATECALL
  return CFG_EDGE_COLORS.CALL
}
</script>

<template>
  <div class="sequence-panel">
    <div class="panel-header">
      <span class="panel-label">
        (D) Contract Call Tree
        <span class="edge-legend-control">
          <button
            type="button"
            class="edge-info-button"
            aria-label="Explain call tree edge colors"
            :aria-expanded="showEdgeLegend"
            @click.stop="showEdgeLegend = !showEdgeLegend"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.2" />
              <text x="7" y="11.1" text-anchor="middle" font-size="9" font-weight="600" fill="currentColor">?</text>
            </svg>
          </button>
          <div v-if="showEdgeLegend" class="edge-legend-tooltip" @click.stop>
            <div class="edge-legend-title">Call Tree Edge Colors</div>
            <div v-for="edgeType in callTreeEdgeTypes" :key="edgeType.type" class="edge-legend-item">
              <svg width="34" height="12" viewBox="0 0 34 12" aria-hidden="true">
                <line
                  x1="2"
                  y1="6"
                  x2="26"
                  y2="6"
                  :stroke="edgeType.color"
                  stroke-width="2.5"
                  :stroke-dasharray="edgeType.dashed ? '5 3' : undefined"
                />
                <polygon points="26,3 32,6 26,9" :fill="edgeType.color" />
              </svg>
              <span><strong>{{ edgeType.type }}</strong>{{ edgeType.desc }}</span>
            </div>
          </div>
        </span>
      </span>
      <div class="panel-controls">
        <div v-if="status === 'success' && model" class="tree-actions" aria-label="Call tree fold controls">
          <button type="button" @click="expandAll">Expand</button>
          <button type="button" @click="collapseAll">Collapse</button>
        </div>
        <div class="graph-actions">
          <GraphFitButton
            graph-name="contract call tree"
            @fit="fitTreeToViewport"
          />
          <GraphExpandButton
            graph-name="contract call tree"
            :expanded="props.expanded"
            @toggle="emit('toggle-expanded')"
          />
        </div>
      </div>
    </div>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading call tree...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error" role="alert">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success' && model" ref="sequenceContainer" class="sequence-container">
      <div ref="graphViewport" class="graph-viewport" @click.self="closePopup">
        <svg
          ref="treeSvg"
          :class="['call-tree-svg', { 'playback-active': props.playbackActive }]"
          width="100%"
          height="100%"
          :viewBox="treeViewBox"
          aria-label="Foldable contract call tree"
          @click.self="closePopup"
        >
          <defs>
            <marker id="call-tree-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
            </marker>
          </defs>

          <g ref="treeZoomLayer" class="tree-zoom-layer">
          <g class="tree-edges">
            <TransitionGroup name="call-tree-edge">
            <g
              v-for="edge in layout.edges"
              :key="edge.key"
              :class="['tree-edge', edgeClass(edge), { muted: isTreeEdgeMuted(edge) }]"
              :style="{ '--call-tree-edge-color': edgeColor(edge) }"
            >
              <path :d="connector(edge)" marker-end="url(#call-tree-arrow)" />
            </g>
            </TransitionGroup>
          </g>

          <TransitionGroup name="call-tree-node">
          <g
            v-for="node in layout.nodes"
            :key="node.key"
            :data-call-id="node.frame?.call_id"
            :transform="`translate(${node.x} ${node.y})`"
            :class="['tree-node', {
              root: !node.frame,
              token: node.frame && isToken(node.frame),
              selected: node.frame && selectedCallId === node.frame.call_id,
              linked: node.frame && linkedCallIds.has(node.frame.call_id),
              muted: isTreeNodeMuted(node),
              reverted: node.frame?.exit_op.toUpperCase() === 'REVERT',
              'reverted-subtree': node.frame && layout.revertedCallIds.has(node.frame.call_id),
            }]"
            role="button"
            tabindex="0"
            :aria-label="node.frame
              ? `Call ${node.frame.call_id}: ${node.frame.from_name} to ${node.frame.to_name}, ${node.frame.exit_op}${linkedCallIds.has(node.frame.call_id) ? ', linked to selected token transfer' : ''}`
              : `Root contract: ${model.rootName}`"
            :aria-current="node.frame && linkedCallIds.has(node.frame.call_id) ? 'true' : undefined"
            @click="node.frame ? handleNodeClick(node.frame, $event) : handleRootClick()"
            @keydown.enter.prevent="node.frame ? handleNodeClick(node.frame) : handleRootClick()"
            @keydown.space.prevent="node.frame ? handleNodeClick(node.frame) : handleRootClick()"
          >
            <template v-if="node.frame">
              <ellipse
                v-if="selectedCallId === node.frame.call_id && isToken(node.frame)"
                :cx="NODE_WIDTH / 2"
                :cy="NODE_HEIGHT / 2"
                :rx="NODE_WIDTH / 2 + 3"
                :ry="NODE_HEIGHT / 2 + 3"
                class="selection-halo"
              />
              <rect
                v-else-if="selectedCallId === node.frame.call_id"
                x="-3"
                y="-3"
                :width="NODE_WIDTH + 6"
                :height="NODE_HEIGHT + 6"
                rx="5"
                class="selection-halo"
              />
              <ellipse
                v-if="isToken(node.frame)"
                :cx="NODE_WIDTH / 2"
                :cy="NODE_HEIGHT / 2"
                :rx="NODE_WIDTH / 2"
                :ry="NODE_HEIGHT / 2"
                class="node-shape"
                :fill="fillForAddress(node.frame.to_address)"
                :stroke="strokeForFrame(node.frame)"
              />
              <rect
                v-else
                :width="NODE_WIDTH"
                :height="NODE_HEIGHT"
                rx="3"
                class="node-shape"
                :fill="fillForAddress(node.frame.to_address)"
                :stroke="strokeForFrame(node.frame)"
              />
              <title>{{ node.frame.from_name }} → {{ node.frame.to_name }}</title>
              <text :x="nodeTextLeft(node.frame)" y="16" class="node-order">#{{ node.frame.call_id }}</text>
              <text :x="nodeContractX(node.frame)" y="16" class="node-contract">
                {{ nodeContractLabel(node.frame) }}
                <title>{{ node.frame.to_name }}</title>
              </text>
              <text :x="nodeTextLeft(node.frame)" y="34" class="node-signature">
                {{ nodeSignatureLabel(node.frame) }}
                <title>{{ primarySignature(node.frame) }}</title>
              </text>
              <g
                v-if="node.frame.childCallIds.length > 0"
                class="fold-control"
                :transform="`translate(${foldControlX(node.frame)} ${foldControlY(node.frame)})`"
                role="button"
                tabindex="0"
                :aria-label="collapsedIds.has(node.frame.call_id) ? 'Expand nested calls' : 'Collapse nested calls'"
                @click.stop="toggleCall(node.frame.call_id)"
                @keydown.enter.stop.prevent="toggleCall(node.frame.call_id)"
                @keydown.space.stop.prevent="toggleCall(node.frame.call_id)"
              >
                <circle r="6" />
                <path :d="collapsedIds.has(node.frame.call_id) ? 'M -2.25 -3.5 L 3 0 L -2.25 3.5 Z' : 'M -3.5 -2 L 0 3 L 3.5 -2 Z'" />
              </g>
              <text
                v-if="collapsedIds.has(node.frame.call_id)"
                :x="foldedCountX(node.frame)"
                :y="foldedCountY(node.frame)"
                text-anchor="end"
                class="folded-count"
              >
                +{{ reachedDescendantCount(node.frame) }}
              </text>
            </template>

            <template v-else>
              <rect
                :width="NODE_WIDTH"
                :height="NODE_HEIGHT"
                rx="3"
                class="node-shape root-shape"
                :fill="fillForAddress(model.rootAddress)"
                :stroke="rootStroke()"
              />
              <text x="22" y="16" class="node-order">#0</text>
              <text x="47" y="16" class="node-contract">
                {{ clipped(model.rootName, 21) }}
                <title>{{ model.rootName }}</title>
              </text>
              <g
                v-if="model.rootCallIds.length > 0"
                class="fold-control"
                :transform="`translate(${NODE_WIDTH - 10} ${NODE_HEIGHT - 8})`"
                role="button"
                tabindex="0"
                :aria-label="collapsedToRoot ? 'Expand root contract calls' : 'Collapse root contract calls'"
                :aria-expanded="!collapsedToRoot"
                @click.stop="toggleRoot"
                @keydown.enter.stop.prevent="toggleRoot"
                @keydown.space.stop.prevent="toggleRoot"
              >
                <circle r="6" />
                <path :d="collapsedToRoot ? 'M -2.25 -3.5 L 3 0 L -2.25 3.5 Z' : 'M -3.5 -2 L 0 3 L 3.5 -2 Z'" />
              </g>
              <text
                v-if="collapsedToRoot"
                :x="NODE_WIDTH - 19"
                :y="NODE_HEIGHT - 5"
                text-anchor="end"
                class="folded-count"
              >
                +{{ reachedCallCount }}
              </text>
            </template>
          </g>
          </TransitionGroup>
          </g>
        </svg>
      </div>

      <aside
        v-if="selectedFrame && popupVisible"
        ref="popupRef"
        class="call-popup"
        :style="{ left: `${popupPosition.left}px`, top: `${popupPosition.top}px` }"
        aria-label="Selected call details"
        @click.stop
      >
        <div class="popup-header">
          <div>
            <div class="popup-kicker">CALL #{{ selectedFrame.call_id }} · DEPTH {{ selectedFrame.depth }}</div>
            <div class="popup-title">{{ selectedFrame.from_name }} → {{ selectedFrame.to_name }}</div>
          </div>
          <button type="button" class="close-btn" aria-label="Close call details" @click="closePopup">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
          </button>
        </div>

        <div class="popup-content">
          <div class="popup-grid">
            <div><span>Entry</span><strong :class="['entry-value', entryClass(selectedFrame)]">{{ selectedFrame.entry_op }} · {{ selectedFrame.entry_step }}</strong></div>
            <div><span>Exit</span><strong :class="{ error: selectedFrame.exit_op.toUpperCase() === 'REVERT' }">{{ selectedFrame.exit_op }} · {{ selectedFrame.exit_step }}</strong></div>
            <div><span>Function selector</span><strong>{{ selectedFrame.selector || '—' }}</strong></div>
          </div>

          <section class="disclosure-section signature-section">
            <button
              type="button"
              class="disclosure-button"
              :disabled="!selectedFrame.probable_text_signatures?.length"
              :aria-expanded="signaturesExpanded"
              @click="toggleSignatures"
            >
              <span>
                <strong>Probable signatures</strong>
                <small>{{ signatureSummary(selectedFrame) }}</small>
              </span>
              <svg :class="{ open: signaturesExpanded }" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
            </button>
            <div v-if="signaturesExpanded" class="disclosure-content signature-list">
              <code v-for="(signature, index) in selectedFrame.probable_text_signatures" :key="`${signature}-${index}`">
                <span>{{ index + 1 }}</span>{{ signature }}
              </code>
            </div>
          </section>

          <section class="disclosure-section">
            <button
              type="button"
              class="disclosure-button"
              :disabled="selectedFrame.calldata.length === 0"
              :aria-expanded="calldataExpanded"
              @click="toggleCalldata"
            >
              <span>
                <strong>Calldata</strong>
              </span>
              <svg :class="{ open: calldataExpanded }" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
            </button>
            <div v-if="calldataExpanded" class="disclosure-content calldata-list">
              <code v-for="(item, index) in selectedFrame.calldata" :key="`${selectedFrame.call_id}-${index}`">
                <span>{{ index }}</span>{{ item }}
              </code>
            </div>
          </section>
        </div>
      </aside>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Enter a transaction hash to view call tree</span>
    </div>
  </div>
</template>

<style scoped>
.sequence-panel {
  position: relative;
  background: var(--panel-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-header {
  min-height: 31px;
  padding: 5px 8px 5px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid #e2e8f0;
  background: var(--panel-bg);
  z-index: 10;
}

.panel-label {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #000;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
}

.edge-legend-control {
  position: relative;
  display: inline-flex;
}

.edge-info-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  padding: 0;
  border: 0;
  color: #64748b;
  background: transparent;
  cursor: pointer;
}

.edge-info-button svg { display: block; }

.edge-info-button:hover,
.edge-info-button[aria-expanded='true'] {
  color: var(--accent);
}

.edge-legend-tooltip {
  position: absolute;
  top: calc(100% + 7px);
  left: -8px;
  z-index: 40;
  width: 260px;
  padding: 10px 12px;
  border: 1px solid #d7dee8;
  border-radius: 8px;
  color: #334155;
  background: rgba(255, 255, 255, 0.98);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.16);
  white-space: normal;
}

.edge-legend-title {
  margin-bottom: 7px;
  color: #0f172a;
  font-size: 10px;
  font-weight: 800;
}

.edge-legend-item {
  display: grid;
  grid-template-columns: 34px 1fr;
  align-items: center;
  gap: 7px;
  margin: 5px 0;
  font-size: 9px;
  font-weight: 500;
  line-height: 1.3;
}

.edge-legend-item strong {
  display: block;
  color: #1e293b;
  font-size: 9px;
}

.tree-actions {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
}

.panel-controls {
  display: flex;
  align-items: center;
  gap: 24px;
}

.graph-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tree-actions button {
  min-height: 23px;
  padding: 2px 7px;
  border: 1px solid #d0d0d0;
  border-radius: 2px;
  color: #475569;
  background: #f8fafc;
  font-size: 10px;
  cursor: pointer;
}

.tree-actions button:hover {
  border-color: var(--accent);
  background: #f1f5f9;
}

.tree-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.sequence-container {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.graph-viewport {
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.call-tree-svg {
  display: block;
  width: 100%;
  height: 100%;
  cursor: grab;
  touch-action: none;
  background: #fff;
  font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

.call-tree-svg:active { cursor: grabbing; }

.tree-edge path {
  fill: none;
  stroke: var(--call-tree-edge-color, #769B7E);
  stroke-width: 1.35;
  vector-effect: non-scaling-stroke;
}

.tree-edge.reverted path {
  stroke-width: 1.7;
  stroke-dasharray: 5 3;
}

.tree-edge.reverted-subtree,
.tree-node.reverted-subtree {
  opacity: 0.38;
}

.tree-node { cursor: pointer; }
.call-tree-node-enter-active,
.call-tree-edge-enter-active {
  transition: opacity 90ms ease-out;
}

.call-tree-node-enter-from,
.call-tree-edge-enter-from {
  opacity: 0;
}
.tree-node.root { cursor: pointer; }
.tree-node:focus { outline: none; }

.node-shape {
  stroke-width: 1.2;
  vector-effect: non-scaling-stroke;
  transition: filter 150ms ease, stroke-width 150ms ease;
}

.tree-node:hover .node-shape,
.tree-node:focus-visible .node-shape {
  filter: brightness(0.98);
  stroke-width: 2;
}

.tree-node.linked .node-shape {
  stroke-width: 3;
  filter: brightness(0.96) drop-shadow(0 0 9px rgba(121, 136, 160, 0.38));
}

.tree-node.muted,
.tree-edge.muted {
  opacity: 0.08;
}

.tree-node.muted {
  pointer-events: none;
}

.selection-halo {
  fill: none;
  stroke: #1d4ed8;
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
  pointer-events: none;
}

.node-order,
.node-signature,
.folded-count,
.root-contract {
  font-family: Consolas, Monaco, monospace;
}

.node-order {
  fill: #334155;
  font-size: 9.5px;
  font-weight: 700;
}

.node-contract {
  fill: #111827;
  font-size: 12px;
  font-weight: 700;
}

.node-signature {
  fill: #334155;
  font-size: 10px;
}

.folded-count { fill: #8a5a00; font-size: 8.5px; font-weight: 700; }

.fold-control { cursor: pointer; }
.fold-control circle { fill: rgba(255, 255, 255, 0.9); stroke: #7b8fad; stroke-width: 1; vector-effect: non-scaling-stroke; }
.fold-control path { fill: #475569; }
.fold-control:hover circle { fill: #eef2ff; stroke: #1d4ed8; }

.call-popup {
  position: absolute;
  z-index: 30;
  width: min(200px, calc(100% - 24px));
  max-height: min(480px, calc(100% - 24px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(148, 163, 184, 0.7);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.98);
  box-shadow: 0 14px 32px rgba(15, 23, 42, 0.18);
}

.popup-header {
  min-height: 56px;
  padding: 9px 10px 9px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-bottom: 1px solid #e2e8f0;
}

.popup-kicker {
  margin-bottom: 3px;
  color: #64748b;
  font-family: Consolas, Monaco, monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.popup-title {
  color: #0f172a;
  font-size: 12px;
  font-weight: 700;
  overflow-wrap: anywhere;
}

.close-btn {
  width: 24px;
  height: 24px;
  flex: 0 0 auto;
  padding: 5px;
  border: none;
  border-radius: 50%;
  color: #475569;
  background: rgba(148, 163, 184, 0.16);
  cursor: pointer;
}

.close-btn:hover { background: rgba(148, 163, 184, 0.3); }
.close-btn svg { width: 14px; height: 14px; display: block; }
.close-btn path { fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; }

.popup-content {
  min-height: 0;
  padding: 9px;
  overflow: auto;
  background: #f8fafc;
}

.popup-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
}

.popup-grid > div {
  min-width: 0;
  padding: 6px 7px;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  background: #fff;
}

.popup-grid span,
.disclosure-button strong {
  display: block;
  color: #64748b;
  font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.popup-grid span { margin-bottom: 2px; }

.popup-grid strong,
.disclosure-button small {
  display: block;
  overflow-wrap: anywhere;
  color: #1e293b;
  font-family: Consolas, Monaco, monospace;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.4;
}

.popup-grid strong.error { color: #c14a00; }
.popup-grid strong.entry-value.call,
.popup-grid strong.entry-value.static,
.popup-grid strong.entry-value.callcode { color: #769B7E; }
.popup-grid strong.entry-value.delegated { color: #7898BC; }

.disclosure-section {
  margin-top: 7px;
  overflow: hidden;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  background: #fff;
}

.disclosure-button {
  width: 100%;
  min-height: 43px;
  padding: 6px 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: none;
  color: #1e293b;
  background: #fff;
  text-align: left;
  cursor: pointer;
}

.disclosure-button:hover:not(:disabled) { background: #f8fafc; }
.disclosure-button:disabled { cursor: default; opacity: 0.65; }
.disclosure-button > span { min-width: 0; }
.disclosure-button small {
  margin-top: 3px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.disclosure-button svg {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  transition: transform 160ms ease;
}

.disclosure-button svg.open { transform: rotate(180deg); }
.disclosure-button path { fill: none; stroke: #64748b; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }

.disclosure-content {
  padding: 7px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-top: 1px solid #e2e8f0;
  background: #f8fafc;
}

.disclosure-content code {
  padding: 5px 6px;
  display: block;
  overflow-wrap: anywhere;
  border: 1px solid #e2e8f0;
  border-radius: 3px;
  color: #1e293b;
  background: #fff;
  font-family: Consolas, Monaco, monospace;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.4;
}

.disclosure-content code span {
  margin-right: 7px;
  color: #94a3b8;
}

.status-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: var(--accent);
  font-size: 12px;
  text-align: center;
  max-width: min(340px, calc(100% - 32px));
}

.status-overlay.error { color: var(--error); }

.placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 12px;
}

@media (prefers-reduced-motion: reduce) {
  .node-shape,
  .disclosure-button svg { transition: none; }
  .call-tree-node-enter-active,
  .call-tree-edge-enter-active { transition: none; }
}
</style>
