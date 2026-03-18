<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { zoom, zoomIdentity } from 'd3-zoom'
import { select } from 'd3-selection'
import {
  fetchSequenceCalldataMapping,
  fetchSequenceSvg,
  type SequenceCallEntry,
  type SequenceCalldataMapping,
} from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  isAnalyzing: boolean
  selectedStepRange: { entryStep: number; exitStep: number } | null
}>()

const emit = defineEmits<{
  'sequence-select': [stepRange: { entryStep: number; exitStep: number } | null]
}>()

const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const graphContainer = ref<HTMLElement | null>(null)
const viewportRef = ref<HTMLElement | null>(null)
const popupRef = ref<HTMLElement | null>(null)
const selectedLinkElement = ref<SVGAElement | null>(null)
const selectedCall = ref<SequenceCallEntry | null>(null)
const popupPosition = ref({ left: 12, top: 40 })
const popupAnchor = ref<{ x: number; y: number } | null>(null)

let sequenceMapping: SequenceCalldataMapping | null = null
let resizeObserver: ResizeObserver | null = null
let svgElement: SVGSVGElement | null = null
let wrapperGroup: SVGGElement | null = null
let zoomBehavior: any = null
let svgSelection: any = null
let currentTransform = zoomIdentity

const selectedCallJson = computed(() => (
  selectedCall.value ? JSON.stringify(selectedCall.value, null, 2) : ''
))

watch(() => props.txHash, (newHash) => {
  resetPanelState()
  if (newHash) {
    loadSequenceData(newHash)
  } else {
    status.value = 'idle'
  }
}, { immediate: true })

watch(() => props.selectedStepRange, (stepRange) => {
  if (!stepRange && selectedLinkElement.value) {
    clearSelection(false)
  }
})

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
})

async function loadSequenceData(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''

  try {
    const [svgText, mapping] = await Promise.all([
      fetchSequenceSvg(txHash),
      fetchSequenceCalldataMapping(txHash),
    ])

    sequenceMapping = mapping
    status.value = 'success'

    await nextTick()
    if (!graphContainer.value) return

    graphContainer.value.innerHTML = svgText
    setupSvg()
  } catch (error: any) {
    status.value = 'error'
    errorMsg.value = formatLoadError(error)
  }
}

function formatLoadError(error: { message?: string } | null | undefined) {
  const message = error?.message || 'Failed to load sequence diagram'
  if (message.includes('trace_sequence.svg') && message.includes('404')) {
    return 'Sequence SVG is unavailable. Re-run analysis after PlantUML SVG export is configured.'
  }
  if (message.includes('trace_sequence_calldata_mapping.json') && message.includes('404')) {
    return 'Sequence calldata mapping is missing for this transaction.'
  }
  return message
}

function resetPanelState() {
  clearSelection(false)
  errorMsg.value = ''
  sequenceMapping = null
  popupAnchor.value = null
  currentTransform = zoomIdentity
  if (graphContainer.value) {
    graphContainer.value.innerHTML = ''
  }
  svgElement = null
  wrapperGroup = null
  zoomBehavior = null
  svgSelection = null
}

function setupSvg() {
  if (!graphContainer.value) return

  const svg = graphContainer.value.querySelector('svg')
  if (!svg) {
    status.value = 'error'
    errorMsg.value = 'Sequence SVG loaded, but no <svg> element was found.'
    return
  }

  svgElement = svg as SVGSVGElement
  svgElement.setAttribute('width', '100%')
  svgElement.setAttribute('height', '100%')
  svgElement.setAttribute('preserveAspectRatio', 'none')
  svgElement.style.display = 'block'
  svgElement.style.overflow = 'hidden'
  svgElement.style.width = '100%'
  svgElement.style.height = '100%'

  const staticTags = new Set(['defs', 'style', 'title', 'desc', 'metadata'])
  const zoomGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  zoomGroup.setAttribute('class', 'sequence-zoom-group')

  const nodesToMove = Array.from(svgElement.childNodes)
  nodesToMove.forEach((node) => {
    if (
      node.nodeType === Node.ELEMENT_NODE &&
      staticTags.has((node as Element).tagName.toLowerCase())
    ) {
      return
    }
    zoomGroup.appendChild(node)
  })

  svgElement.appendChild(zoomGroup)
  wrapperGroup = zoomGroup

  svgSelection = select(svgElement as Element)
  zoomBehavior = zoom()
    .scaleExtent([0.05, 8])
    .on('zoom', (event) => {
      currentTransform = event.transform
      if (wrapperGroup) {
        select(wrapperGroup).attr('transform', event.transform.toString())
      }
    })

  svgSelection.call(zoomBehavior as any)

  svgElement.addEventListener('click', handleSvgBackgroundClick)

  attachInteractivity()
  installResizeObserver()

  nextTick(() => {
    resetViewport()
  })
}

function installResizeObserver() {
  if (!graphContainer.value) return

  if (resizeObserver) {
    resizeObserver.disconnect()
  }

  resizeObserver = new ResizeObserver(() => {
    if (isIdentityTransform()) {
      resetViewport()
    }
    if (selectedCall.value) {
      updatePopupPosition()
    }
  })
  resizeObserver.observe(graphContainer.value)
}

function handleSvgBackgroundClick(event: MouseEvent) {
  const target = event.target as Element | null
  if (target?.closest('a')) return
  clearSelection()
}

function isIdentityTransform() {
  return currentTransform.x === 0 && currentTransform.y === 0 && currentTransform.k === 1
}

function resetViewport() {
  if (!svgSelection || !zoomBehavior) return
  currentTransform = zoomIdentity
  svgSelection.call(zoomBehavior.transform as any, zoomIdentity)
}

function attachInteractivity() {
  if (!wrapperGroup) return

  const links = Array.from(wrapperGroup.querySelectorAll('a'))
  links.forEach((link) => {
    const href =
      link.getAttribute('href') ||
      link.getAttribute('xlink:href') ||
      link.getAttributeNS('http://www.w3.org/1999/xlink', 'href') ||
      ''

    const match = href.match(/^#call-(\d+)$/)
    if (!match) return

    const callId = parseInt(match[1], 10)
    const anchor = link as SVGAElement
    anchor.classList.add('sequence-call-link')

    anchor.addEventListener('click', (event) => {
      event.preventDefault()
      event.stopPropagation()
      handleCallClick(anchor, callId, event as MouseEvent)
    })

    anchor.addEventListener('mouseenter', () => {
      anchor.classList.add('sequence-hovered')
    })

    anchor.addEventListener('mouseleave', () => {
      anchor.classList.remove('sequence-hovered')
    })
  })
}

function handleCallClick(anchor: SVGAElement, callId: number, event: MouseEvent) {
  const callEntry = sequenceMapping?.calls.find((item) => item.call_id === callId) || null
  if (!callEntry) return

  if (selectedLinkElement.value === anchor) {
    clearSelection()
    return
  }

  if (selectedLinkElement.value) {
    selectedLinkElement.value.classList.remove('sequence-selected')
  }

  selectedLinkElement.value = anchor
  selectedLinkElement.value.classList.add('sequence-selected')
  selectedCall.value = callEntry

  const viewportRect = viewportRef.value?.getBoundingClientRect()
  if (viewportRect) {
    popupAnchor.value = {
      x: event.clientX - viewportRect.left,
      y: event.clientY - viewportRect.top,
    }
  }

  emit('sequence-select', {
    entryStep: callEntry.entry_step,
    exitStep: callEntry.exit_step,
  })

  nextTick(() => {
    updatePopupPosition()
  })
}

function updatePopupPosition() {
  if (!popupAnchor.value || !popupRef.value || !viewportRef.value) return

  const viewportRect = viewportRef.value.getBoundingClientRect()
  const popupWidth = popupRef.value.offsetWidth || 360
  const popupHeight = popupRef.value.offsetHeight || 240
  const margin = 12

  let left = popupAnchor.value.x + 14
  let top = popupAnchor.value.y + 14

  if (left + popupWidth > viewportRect.width - margin) {
    left = Math.max(margin, popupAnchor.value.x - popupWidth - 14)
  }
  if (top + popupHeight > viewportRect.height - margin) {
    top = Math.max(margin, viewportRect.height - popupHeight - margin)
  }

  popupPosition.value = { left, top }
}

function clearSelection(shouldEmit = true) {
  if (selectedLinkElement.value) {
    selectedLinkElement.value.classList.remove('sequence-selected')
    selectedLinkElement.value = null
  }

  selectedCall.value = null
  popupAnchor.value = null

  if (shouldEmit) {
    emit('sequence-select', null)
  }
}
</script>

<template>
  <div class="sequence-panel">
    <span class="panel-label">(d) Sequence Diagram</span>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading sequence diagram...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" ref="viewportRef" class="sequence-container">
      <div ref="graphContainer" class="graph-viewport"></div>

      <div
        v-if="selectedCall"
        ref="popupRef"
        class="call-popup"
        :style="{ left: `${popupPosition.left}px`, top: `${popupPosition.top}px` }"
        @click.stop
      >
        <div class="popup-header">
          <div class="popup-title">{{ selectedCall.from_name }} -> {{ selectedCall.to_name }}</div>
          <button class="close-btn" @click="clearSelection()">x</button>
        </div>
        <div class="popup-meta">
          <span>{{ selectedCall.entry_op }} / {{ selectedCall.exit_op }}</span>
          <span>{{ selectedCall.entry_step }} - {{ selectedCall.exit_step }}</span>
        </div>
        <pre class="popup-json">{{ selectedCallJson }}</pre>
      </div>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Enter a transaction hash to view sequence diagram</span>
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

.panel-label {
  position: absolute;
  top: 8px;
  left: 12px;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.5px;
  z-index: 10;
}

.sequence-container {
  position: relative;
  flex: 1;
  min-height: 0;
  padding-top: 28px;
  display: flex;
  overflow: hidden;
}

.graph-viewport {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.graph-viewport :deep(svg) {
  width: 100%;
  height: 100%;
}

.graph-viewport :deep(.sequence-call-link) {
  cursor: pointer;
}

.graph-viewport :deep(.sequence-call-link text) {
  cursor: pointer;
  user-select: none;
}

.graph-viewport :deep(.sequence-call-link.sequence-hovered line),
.graph-viewport :deep(.sequence-call-link.sequence-hovered path),
.graph-viewport :deep(.sequence-call-link.sequence-hovered polygon),
.graph-viewport :deep(.sequence-call-link.sequence-hovered text) {
  filter: brightness(1.03);
}

.graph-viewport :deep(.sequence-call-link.sequence-hovered line),
.graph-viewport :deep(.sequence-call-link.sequence-hovered path),
.graph-viewport :deep(.sequence-call-link.sequence-hovered polygon) {
  stroke-width: 2.4 !important;
}

.graph-viewport :deep(.sequence-call-link.sequence-selected) {
  filter: drop-shadow(0 0 8px rgba(37, 99, 235, 0.35));
}

.graph-viewport :deep(.sequence-call-link.sequence-selected line),
.graph-viewport :deep(.sequence-call-link.sequence-selected path),
.graph-viewport :deep(.sequence-call-link.sequence-selected polygon) {
  stroke: #1d4ed8 !important;
  stroke-width: 2.8 !important;
}

.graph-viewport :deep(.sequence-call-link.sequence-selected text) {
  fill: #1e3a8a !important;
  font-weight: 700;
}

.call-popup {
  position: absolute;
  width: min(420px, calc(100% - 24px));
  max-height: calc(100% - 40px);
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid rgba(148, 163, 184, 0.65);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.98);
  box-shadow: 0 14px 32px rgba(15, 23, 42, 0.18);
  z-index: 20;
}

.popup-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.popup-title {
  font-size: 12px;
  font-weight: 700;
  color: #0f172a;
  word-break: break-word;
}

.close-btn {
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.16);
  color: #475569;
  cursor: pointer;
  flex-shrink: 0;
}

.close-btn:hover {
  background: rgba(148, 163, 184, 0.3);
}

.popup-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 10px;
  color: #475569;
  font-family: 'Consolas', 'Monaco', monospace;
}

.popup-json {
  margin: 0;
  padding: 10px;
  flex: 1;
  min-height: 0;
  overflow: auto;
  border-radius: 10px;
  background: #f8fafc;
  color: #0f172a;
  font-size: 10px;
  line-height: 1.45;
  font-family: 'Consolas', 'Monaco', monospace;
}

.popup-json::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.popup-json::-webkit-scrollbar-thumb {
  background: rgba(148, 163, 184, 0.7);
  border-radius: 999px;
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
