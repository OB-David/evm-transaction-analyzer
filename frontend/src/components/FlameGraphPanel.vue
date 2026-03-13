<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { zoom, zoomIdentity } from 'd3-zoom'
import { select } from 'd3-selection'
import { fetchFlameGraphSvg } from '../api/analyze'

const props = defineProps<{
  txHash: string | null
  isAnalyzing: boolean
}>()

const emit = defineEmits<{
  'flame-select': [stepRange: { entryStep: number; exitStep: number } | null]
}>()

const status = ref<'idle' | 'loading' | 'success' | 'error'>('idle')
const errorMsg = ref('')
const graphContainer = ref<HTMLElement | null>(null)
const selectedElement = ref<SVGGElement | null>(null)

watch(() => props.txHash, (newHash) => {
  selectedElement.value = null
  if (newHash) {
    loadFlameGraph(newHash)
  } else {
    status.value = 'idle'
  }
}, { immediate: true })

async function loadFlameGraph(txHash: string) {
  status.value = 'loading'
  errorMsg.value = ''
  selectedElement.value = null

  try {
    const svgText = await fetchFlameGraphSvg(txHash)
    status.value = 'success'

    await nextTick()
    if (graphContainer.value) {
      graphContainer.value.innerHTML = svgText
      setupSvg()
    }
  } catch (e: any) {
    status.value = 'error'
    errorMsg.value = e.message || 'Failed to load flame graph'
  }
}

function setupSvg() {
  if (!graphContainer.value) return
  const svg = graphContainer.value.querySelector('svg')
  if (!svg) return

  // Remove viewBox and set SVG to fill container; d3-zoom handles all positioning
  svg.removeAttribute('viewBox')
  svg.setAttribute('width', '100%')
  svg.setAttribute('height', '100%')
  svg.style.display = 'block'
  svg.style.overflow = 'hidden'

  // Wrap all children in a <g> for zoom/pan
  const wrapperG = document.createElementNS('http://www.w3.org/2000/svg', 'g')
  wrapperG.setAttribute('class', 'flame-zoom-group')
  while (svg.firstChild) {
    wrapperG.appendChild(svg.firstChild)
  }
  svg.appendChild(wrapperG)

  // Attach d3-zoom
  const svgSelection = select(svg as Element)
  const zoomBehavior = zoom()
    .scaleExtent([0.1, 10])
    .on('zoom', (event) => {
      select(wrapperG).attr('transform', event.transform.toString())
    })
  svgSelection.call(zoomBehavior as any)

  // Click on empty area to deselect
  svg.addEventListener('click', (e) => {
    if (e.target === svg || (e.target as Element).classList.contains('flame-zoom-group')) {
      clearSelection()
    }
  })

  attachInteractivity(wrapperG)

  // Fit and center the flame graph content within the viewport
  nextTick(() => {
    const bbox = wrapperG.getBBox()
    if (bbox.width > 0 && bbox.height > 0) {
      const containerW = svg.clientWidth
      const containerH = svg.clientHeight
      const padding = 0.9
      const scaleX = containerW / bbox.width
      const scaleY = containerH / bbox.height
      const scale = Math.min(scaleX, scaleY) * padding
      const tx = (containerW - bbox.width * scale) / 2 - bbox.x * scale
      const ty = (containerH - bbox.height * scale) / 2 - bbox.y * scale
      const initTransform = zoomIdentity.translate(tx, ty).scale(scale)
      svgSelection.call(zoomBehavior.transform as any, initTransform)
    }
  })
}

function attachInteractivity(container: SVGGElement) {
  // Find all <g> elements that have a <title> with Step Range info
  const groups = container.querySelectorAll('g')
  groups.forEach((g) => {
    const titleEl = g.querySelector('title')
    if (!titleEl) return

    const titleText = titleEl.textContent || ''
    const stepMatch = titleText.match(/Step Range:\s*(\d+)\s*--\s*(\d+)/)
    if (!stepMatch) return

    const entryStep = parseInt(stepMatch[1], 10)
    const exitStep = parseInt(stepMatch[2], 10)

    // Make the shape clickable
    const shape = g.querySelector('rect, ellipse')
    if (!shape) return

    shape.addEventListener('click', (e) => {
      e.stopPropagation()
      handleBlockClick(g, entryStep, exitStep)
    })

    g.addEventListener('mouseenter', () => {
      g.classList.add('flame-hovered')
    })
    g.addEventListener('mouseleave', () => {
      g.classList.remove('flame-hovered')
    })
  })
}

function handleBlockClick(g: SVGGElement, entryStep: number, exitStep: number) {
  // Toggle: if clicking same element, deselect
  if (selectedElement.value === g) {
    clearSelection()
    return
  }

  // Deselect previous
  if (selectedElement.value) {
    selectedElement.value.classList.remove('flame-selected')
  }

  // Select new
  selectedElement.value = g
  g.classList.add('flame-selected')
  emit('flame-select', { entryStep, exitStep })
}

function clearSelection() {
  if (selectedElement.value) {
    selectedElement.value.classList.remove('flame-selected')
    selectedElement.value = null
  }
  emit('flame-select', null)
}
</script>

<template>
  <div class="flame-panel">
    <span class="panel-label">(e) Flame Graph</span>

    <div v-if="isAnalyzing || status === 'loading'" class="status-overlay">
      Loading flame graph...
    </div>

    <div v-else-if="status === 'error'" class="status-overlay error">
      {{ errorMsg }}
    </div>

    <div v-else-if="status === 'success'" class="flame-container">
      <div ref="graphContainer" class="graph-viewport"></div>
    </div>

    <div v-else class="placeholder">
      <span class="placeholder-text">Enter a transaction hash to view flame graph</span>
    </div>
  </div>
</template>

<style scoped>
.flame-panel {
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

.flame-container {
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
  height: 100%;
  overflow: hidden;
}

.graph-viewport :deep(svg) {
  width: 100%;
  height: 100%;
}

/* Hover effect on flame graph blocks */
.graph-viewport :deep(.flame-hovered rect),
.graph-viewport :deep(.flame-hovered ellipse) {
  stroke-width: 2 !important;
  stroke: #000 !important;
  filter: drop-shadow(0 0 5px rgba(255, 165, 0, 0.5));
  cursor: pointer;
}

/* Selected block */
.graph-viewport :deep(.flame-selected rect),
.graph-viewport :deep(.flame-selected ellipse) {
  stroke-width: 3 !important;
  stroke: #3B5998 !important;
  filter: drop-shadow(0 0 8px rgba(59, 89, 152, 0.7));
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
