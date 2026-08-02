<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import TitleBar from './components/TitleBar.vue'
import InputPanel from './components/InputPanel.vue'
import CfgPanel from './components/CfgPanel.vue'
import AfgPanel from './components/AfgPanel.vue'
import SequencePanel from './components/SequencePanel.vue'
import BlockPanel from './components/BlockPanel.vue'
import LegendPanel from './components/LegendPanel.vue'
import {
  analyzeTransaction,
  analysisStageReached,
  fetchEdgeStepMap,
  fetchArbitrageHashes,
  normalizeAnalyzeError,
  triggerArbitrageRefresh,
  type BlockId,
  type AnalysisStage,
  type AnalyzeResult,
  type CfgMode,
  type EdgeStepMap
} from './api/analyze'

const currentTxHash = ref<string | null>(null)
const activeAnalysisTxHash = ref<string | null>(null)
const analysisStage = ref<AnalysisStage>('queued')
const currentBlockNumber = ref<number | null>(null)
const highlightedBlockId = ref<BlockId[] | null>(null)
const inputPanelRef = ref<InstanceType<typeof InputPanel> | null>(null)
const isAnalyzing = ref(false)
const currentCfgMode = ref<CfgMode>('folded')
const exportInProgress = ref(false)

// Arbitrage hashes from Dune — stored as Sets for O(1) lookup in BlockPanel
const arbitrageTxHashes = ref<Set<string>>(new Set())
const arbitrageBlockNumbers = ref<Set<number>>(new Set())

function applyArbitrageData(data: Awaited<ReturnType<typeof fetchArbitrageHashes>>) {
  arbitrageTxHashes.value = new Set(data.transactions.map(t => t.tx_hash))
  arbitrageBlockNumbers.value = new Set(
    data.transactions.map(t => t.block_number).filter((n): n is number => n !== null)
  )
}

onMounted(async () => {
  try {
    applyArbitrageData(await fetchArbitrageHashes())
  } catch (e) {
    console.warn('Failed to load arbitrage hashes:', e)
  }
})

// Sequence diagram state
const sequenceStepRange = ref<{ entryStep: number; exitStep: number } | null>(null)
const edgeStepMap = ref<EdgeStepMap | null>(null)

// Load edge step map when txHash changes
watch([currentTxHash, currentCfgMode, analysisStage], async ([newHash, cfgMode, stage]) => {
  edgeStepMap.value = null
  if (newHash && analysisStageReached(stage, 'folded_cfg')) {
    try {
      edgeStepMap.value = await fetchEdgeStepMap(newHash, cfgMode)
    } catch (e) {
      console.warn('Failed to load edge step map:', e)
    }
  }
})

// Compute filtered edge IDs from sequence step range
const filteredEdgeIds = computed<string[] | null>(() => {
  if (!sequenceStepRange.value || !edgeStepMap.value) return null
  const { entryStep, exitStep } = sequenceStepRange.value
  const matched = Object.values(edgeStepMap.value)
    .filter(e => e.edge_step >= entryStep && e.edge_step <= exitStep)
    .map(e => e.edge_id)
  return matched.length > 0 ? matched : null
})

function handleAnalysisProgress(txHash: string, result: AnalyzeResult) {
  if (activeAnalysisTxHash.value !== txHash) {
    activeAnalysisTxHash.value = txHash
    currentTxHash.value = null
    currentCfgMode.value = 'folded'
    highlightedBlockId.value = null
    sequenceStepRange.value = null
  }

  if (result.stage !== 'error') {
    analysisStage.value = result.stage
  }
  isAnalyzing.value = result.status === 'processing'
  if (analysisStageReached(result.stage, 'afg')) {
    currentTxHash.value = txHash
  }
}

function isStageReady(stage: AnalysisStage): boolean {
  return analysisStageReached(analysisStage.value, stage)
}

function handleAnalysisComplete(txHash: string) {
  currentTxHash.value = txHash
  analysisStage.value = 'complete'
  isAnalyzing.value = false
  console.log('Analysis complete for:', txHash)
}

function handleBlockNumberChanged(blockNum: number) {
  currentBlockNumber.value = blockNum
  console.log('Block number changed:', blockNum)
}

function handleBlockSelected(blockNum: number) {
  currentBlockNumber.value = blockNum
  if (inputPanelRef.value) {
    inputPanelRef.value.updateBlockNumber(blockNum)
  }
  console.log('Block selected from heatmap:', blockNum)
}

function handleLatestBlock(blockNum: number) {
  if (!currentBlockNumber.value) {
    if (inputPanelRef.value) {
      inputPanelRef.value.updateBlockNumber(blockNum)
    }
  }
}

function handleLatestBlocksRefreshed() {
  triggerArbitrageRefresh().then(() => {
    setTimeout(async () => {
      try {
        applyArbitrageData(await fetchArbitrageHashes())
      } catch (e) {
        console.warn('Failed to refresh arbitrage hashes:', e)
      }
    }, 5_000)
  }).catch(() => {})
}

async function handleTransactionSelected(txHash: string) {
  console.log('Transaction selected from heatmap:', txHash)

  // Sync input and immediately switch status to processing.
  inputPanelRef.value?.setAnalyzing(txHash)

  // Show loading immediately
  isAnalyzing.value = true

  // First complete the analysis, THEN update the hash to trigger panel loading
  try {
    const res = await analyzeTransaction(txHash, progress => handleAnalysisProgress(txHash, progress))
    if (res.status === 'success') {
      console.log('Analysis complete for selected transaction')
      currentTxHash.value = txHash
      analysisStage.value = 'complete'
      inputPanelRef.value?.setAnalyzeSuccess()
    } else {
      inputPanelRef.value?.setAnalyzeError(normalizeAnalyzeError(res.error))
    }
  } catch (e) {
    console.error('Failed to analyze selected transaction:', e)
    const message = e instanceof Error ? e.message : 'Network error'
    inputPanelRef.value?.setAnalyzeError(normalizeAnalyzeError(message))
  } finally {
    isAnalyzing.value = false
  }
}

function handleCfgNavigate(blockIds: BlockId[] | null) {
  // Clear sequence selection when AFG navigates
  sequenceStepRange.value = null
  highlightedBlockId.value = blockIds
  console.log('Navigate to CFG blocks:', blockIds)
}

function handleSequenceSelect(stepRange: { entryStep: number; exitStep: number } | null) {
  // Clear AFG highlight when sequence diagram selects
  highlightedBlockId.value = null
  sequenceStepRange.value = stepRange
  console.log('Sequence diagram selection:', stepRange)
}

function handleCfgModeChange(mode: CfgMode) {
  currentCfgMode.value = mode
  highlightedBlockId.value = null
}

const SVG_EXPORT_STYLE_PROPERTIES = [
  'display',
  'visibility',
  'opacity',
  'fill',
  'fill-opacity',
  'stroke',
  'stroke-opacity',
  'stroke-width',
  'stroke-linecap',
  'stroke-linejoin',
  'stroke-dasharray',
  'stroke-dashoffset',
  'vector-effect',
  'paint-order',
  'filter',
  'font-family',
  'font-size',
  'font-style',
  'font-weight',
  'letter-spacing',
  'word-spacing',
  'text-anchor',
  'dominant-baseline',
  'shape-rendering',
  'marker-start',
  'marker-mid',
  'marker-end',
]

const SVG_PRESENTATION_ATTRIBUTE_MAP = [
  ['fill', 'fill'],
  ['fill-opacity', 'fill-opacity'],
  ['stroke', 'stroke'],
  ['stroke-opacity', 'stroke-opacity'],
  ['stroke-width', 'stroke-width'],
  ['stroke-linecap', 'stroke-linecap'],
  ['stroke-linejoin', 'stroke-linejoin'],
  ['stroke-dasharray', 'stroke-dasharray'],
  ['stroke-dashoffset', 'stroke-dashoffset'],
  ['opacity', 'opacity'],
  ['font-family', 'font-family'],
  ['font-size', 'font-size'],
  ['font-style', 'font-style'],
  ['font-weight', 'font-weight'],
  ['letter-spacing', 'letter-spacing'],
  ['word-spacing', 'word-spacing'],
  ['text-anchor', 'text-anchor'],
  ['dominant-baseline', 'dominant-baseline'],
  ['vector-effect', 'vector-effect'],
  ['filter', 'filter'],
] as const

type SvgInlineOptions = {
  strokeScale?: number
  stripVectorEffect?: boolean
  bakeStrokeWidth?: boolean
}

function toSvgPoint(svg: SVGSVGElement, x: number, y: number, inverseScreenMatrix: DOMMatrix | SVGMatrix) {
  const point = svg.createSVGPoint()
  point.x = x
  point.y = y
  return point.matrixTransform(inverseScreenMatrix)
}

function formatViewBoxNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  return Number(value.toFixed(4)).toString()
}

function inlineComputedSvgStyles(sourceEl: Element, targetEl: Element, options: SvgInlineOptions = {}): void {
  if (sourceEl instanceof SVGElement && targetEl instanceof SVGElement) {
    const computed = window.getComputedStyle(sourceEl)
    const styleChunks = SVG_EXPORT_STYLE_PROPERTIES
      .map((property) => {
        if (options.stripVectorEffect && property === 'vector-effect') return ''
        const val = computed.getPropertyValue(property)
        return val ? `${property}:${val};` : ''
      })
      .filter(Boolean)
    if (styleChunks.length > 0) {
      targetEl.setAttribute('style', styleChunks.join(''))
    }

    SVG_PRESENTATION_ATTRIBUTE_MAP.forEach(([cssProperty, attributeName]) => {
      if (options.stripVectorEffect && cssProperty === 'vector-effect') return
      const value = computed.getPropertyValue(cssProperty).trim()
      if (!value) return
      if (value === 'normal' && (cssProperty === 'letter-spacing' || cssProperty === 'word-spacing')) return
      targetEl.setAttribute(attributeName, value)
    })

    if (options.stripVectorEffect) {
      targetEl.removeAttribute('vector-effect')
      targetEl.style.vectorEffect = 'none'
    }

    if (options.bakeStrokeWidth) {
      const strokeWidthPx = parsePx(computed.getPropertyValue('stroke-width'), 0)
      const scale = options.strokeScale && options.strokeScale > 0 ? options.strokeScale : 1
      if (strokeWidthPx > 0 && Number.isFinite(strokeWidthPx)) {
        const targetStrokeWidth = strokeWidthPx / scale
        targetEl.setAttribute('stroke-width', toFiniteSvgNumber(targetStrokeWidth))
        targetEl.style.strokeWidth = `${toFiniteSvgNumber(targetStrokeWidth)}`
      }
    }
  }

  const sourceChildren = sourceEl.children
  const targetChildren = targetEl.children
  const childCount = Math.min(sourceChildren.length, targetChildren.length)
  for (let i = 0; i < childCount; i += 1) {
    inlineComputedSvgStyles(sourceChildren[i], targetChildren[i], options)
  }
}

function stripCommentNodes(root: Node): void {
  const children = Array.from(root.childNodes)
  children.forEach((child) => {
    if (child.nodeType === Node.COMMENT_NODE) {
      root.removeChild(child)
      return
    }
    stripCommentNodes(child)
  })
}

function downloadSvgText(svgText: string, filename: string): void {
  const prefixedSvgText = `<?xml version="1.0" encoding="UTF-8"?>\n${svgText}`
  const blob = new Blob([prefixedSvgText], { type: 'image/svg+xml;charset=utf-8' })
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

type RectSnapshot = {
  left: number
  top: number
  right: number
  bottom: number
  width: number
  height: number
}

const SVG_NS = 'http://www.w3.org/2000/svg'
const XLINK_NS = 'http://www.w3.org/1999/xlink'
const FULL_UI_EXPORT_PANEL_SELECTORS = [
  '.title-bar',
  '.input-panel',
  '.block-panel',
  '.afg-panel',
  '.sequence-panel',
  '.legend-panel',
  '.cfg-panel',
] as const
const MAX_FULL_UI_TEXT_NODES = 900
const FULL_UI_EXPORT_CONTROL_SELECTORS = [
  'input',
  'button',
  '.view-mode-pill',
  '.mode-badge',
  '.cfg-mode-toggle',
  '.live-badge',
  '.hash-input',
  '.side-panel',
  '.panel-section-header',
  '.information-content',
  '.info-card',
  '.info-card-overview',
  '.action-card',
  '.llm-analysis-group',
  '.llm-cache-badge',
  '.info-card-badge',
] as const

function rectFromDomRect(rect: DOMRect | DOMRectReadOnly): RectSnapshot {
  return {
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
  }
}

function toFiniteSvgNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  return Number(value.toFixed(3)).toString()
}

function hasDrawableArea(rect: RectSnapshot): boolean {
  return rect.width > 0.5 && rect.height > 0.5
}

function intersectRects(a: RectSnapshot, b: RectSnapshot): RectSnapshot | null {
  const left = Math.max(a.left, b.left)
  const top = Math.max(a.top, b.top)
  const right = Math.min(a.right, b.right)
  const bottom = Math.min(a.bottom, b.bottom)
  const width = right - left
  const height = bottom - top
  if (width <= 0 || height <= 0) return null
  return { left, top, right, bottom, width, height }
}

function createSvgElement<T extends keyof SVGElementTagNameMap>(tag: T): SVGElementTagNameMap[T] {
  return document.createElementNS(SVG_NS, tag)
}

function parsePx(value: string | null | undefined, fallback = 0): number {
  if (!value) return fallback
  const parsed = Number.parseFloat(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function resolveBorder(style: CSSStyleDeclaration): { width: number, color: string } {
  const sides = [
    { width: parsePx(style.borderTopWidth, 0), color: style.borderTopColor },
    { width: parsePx(style.borderRightWidth, 0), color: style.borderRightColor },
    { width: parsePx(style.borderBottomWidth, 0), color: style.borderBottomColor },
    { width: parsePx(style.borderLeftWidth, 0), color: style.borderLeftColor },
  ]
  const visibleSides = sides.filter((side) => side.width > 0.1 && !isTransparentColor(side.color))
  if (visibleSides.length === 0) {
    return { width: 0, color: 'rgba(0, 0, 0, 0.08)' }
  }

  const dominant = visibleSides.reduce((max, side) => side.width > max.width ? side : max, visibleSides[0])
  return {
    width: dominant.width,
    color: dominant.color,
  }
}

function isTransparentColor(color: string | null | undefined): boolean {
  if (!color) return true
  const normalized = color.trim().toLowerCase()
  if (!normalized || normalized === 'transparent') return true
  const rgbaMatch = normalized.match(/^rgba?\((.+)\)$/)
  if (!rgbaMatch) return false
  const parts = rgbaMatch[1].split(',').map((part) => part.trim())
  if (parts.length < 4) return false
  const alpha = Number.parseFloat(parts[3])
  return Number.isFinite(alpha) && alpha <= 0
}

function getVisibleTextRect(textNode: Text): RectSnapshot | null {
  const range = document.createRange()
  range.selectNodeContents(textNode)
  const rect = rectFromDomRect(range.getBoundingClientRect())
  if (!hasDrawableArea(rect)) return null
  return rect
}

function applyTextTransform(text: string, textTransform: string): string {
  if (textTransform === 'uppercase') return text.toUpperCase()
  if (textTransform === 'lowercase') return text.toLowerCase()
  if (textTransform === 'capitalize') {
    return text.replace(/\b([a-z])/g, (ch) => ch.toUpperCase())
  }
  return text
}

function appendPanelFrame(outputSvg: SVGSVGElement, panelEl: HTMLElement, rootRect: RectSnapshot): RectSnapshot | null {
  const panelRect = rectFromDomRect(panelEl.getBoundingClientRect())
  if (!hasDrawableArea(panelRect)) return null

  const style = window.getComputedStyle(panelEl)
  const frameRect = createSvgElement('rect')
  frameRect.setAttribute('x', toFiniteSvgNumber(panelRect.left - rootRect.left))
  frameRect.setAttribute('y', toFiniteSvgNumber(panelRect.top - rootRect.top))
  frameRect.setAttribute('width', toFiniteSvgNumber(panelRect.width))
  frameRect.setAttribute('height', toFiniteSvgNumber(panelRect.height))

  const radius = parsePx(style.borderTopLeftRadius || style.borderRadius, 0)
  if (radius > 0) {
    frameRect.setAttribute('rx', toFiniteSvgNumber(Math.min(radius, panelRect.width * 0.5, panelRect.height * 0.5)))
  }

  const fillColor = !isTransparentColor(style.backgroundColor) ? style.backgroundColor : '#FFFFFF'
  frameRect.setAttribute('fill', fillColor)

  const border = resolveBorder(style)
  const borderWidth = Math.max(0.8, border.width)
  frameRect.setAttribute('stroke', border.color)
  frameRect.setAttribute('stroke-width', toFiniteSvgNumber(borderWidth))
  outputSvg.appendChild(frameRect)

  return panelRect
}

function appendPanelControlLayers(
  outputSvg: SVGSVGElement,
  panelEl: HTMLElement,
  panelRect: RectSnapshot,
  rootRect: RectSnapshot,
): void {
  const selector = FULL_UI_EXPORT_CONTROL_SELECTORS.join(', ')
  const candidateElements = Array.from(panelEl.querySelectorAll(selector)) as HTMLElement[]
  const visited = new Set<HTMLElement>()

  candidateElements.forEach((el) => {
    if (visited.has(el)) return
    visited.add(el)
    if (el.closest('svg')) return
    if (el.closest('.title-bar .actions')) return

    const style = window.getComputedStyle(el)
    if (style.display === 'none' || style.visibility === 'hidden' || Number.parseFloat(style.opacity) <= 0) return

    const rawRect = rectFromDomRect(el.getBoundingClientRect())
    const rect = intersectRects(rawRect, panelRect)
    if (!rect || !hasDrawableArea(rect)) return

    const backgroundColor = style.backgroundColor
    const border = resolveBorder(style)
    const borderWidth = border.width
    const borderColor = border.color
    const hasFill = !isTransparentColor(backgroundColor)
    const hasBorder = borderWidth > 0.1 && !isTransparentColor(borderColor)
    if (!hasFill && !hasBorder) return

    const shape = createSvgElement('rect')
    shape.setAttribute('x', toFiniteSvgNumber(rect.left - rootRect.left))
    shape.setAttribute('y', toFiniteSvgNumber(rect.top - rootRect.top))
    shape.setAttribute('width', toFiniteSvgNumber(rect.width))
    shape.setAttribute('height', toFiniteSvgNumber(rect.height))

    const radius = parsePx(style.borderTopLeftRadius || style.borderRadius, 0)
    if (radius > 0) {
      shape.setAttribute('rx', toFiniteSvgNumber(Math.min(radius, rect.width * 0.5, rect.height * 0.5)))
    }

    shape.setAttribute('fill', hasFill ? backgroundColor : 'none')
    if (hasBorder) {
      shape.setAttribute('stroke', borderColor)
      shape.setAttribute('stroke-width', toFiniteSvgNumber(Math.max(0.8, borderWidth)))
    } else {
      shape.setAttribute('stroke', 'none')
    }

    outputSvg.appendChild(shape)
  })
}

function buildSvgSnapshotFromScreenRect(
  sourceSvg: SVGSVGElement,
  screenRect: RectSnapshot,
  options: { bakeNonScalingStroke?: boolean } = {},
): SVGSVGElement | null {
  const screenMatrix = sourceSvg.getScreenCTM()
  if (!screenMatrix) return null

  const widthPx = Math.max(1, Math.round(screenRect.width))
  const heightPx = Math.max(1, Math.round(screenRect.height))

  const inverseMatrix = screenMatrix.inverse()
  const topLeft = toSvgPoint(sourceSvg, screenRect.left, screenRect.top, inverseMatrix)
  const topRight = toSvgPoint(sourceSvg, screenRect.right, screenRect.top, inverseMatrix)
  const bottomLeft = toSvgPoint(sourceSvg, screenRect.left, screenRect.bottom, inverseMatrix)
  const bottomRight = toSvgPoint(sourceSvg, screenRect.right, screenRect.bottom, inverseMatrix)

  const minX = Math.min(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x)
  const maxX = Math.max(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x)
  const minY = Math.min(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y)
  const maxY = Math.max(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y)
  const viewBoxWidth = Math.max(1, maxX - minX)
  const viewBoxHeight = Math.max(1, maxY - minY)
  const clonedSvg = sourceSvg.cloneNode(true) as SVGSVGElement
  stripCommentNodes(clonedSvg)
  if (options.bakeNonScalingStroke) {
    const strokeScale = Math.max(0.0001, Math.min(widthPx / viewBoxWidth, heightPx / viewBoxHeight))
    inlineComputedSvgStyles(sourceSvg, clonedSvg, {
      strokeScale,
      stripVectorEffect: true,
      bakeStrokeWidth: true,
    })
  } else {
    inlineComputedSvgStyles(sourceSvg, clonedSvg)
  }
  clonedSvg.setAttribute('xmlns', SVG_NS)
  clonedSvg.setAttribute('xmlns:xlink', XLINK_NS)
  clonedSvg.setAttribute(
    'viewBox',
    `${formatViewBoxNumber(minX)} ${formatViewBoxNumber(minY)} ${formatViewBoxNumber(viewBoxWidth)} ${formatViewBoxNumber(viewBoxHeight)}`,
  )
  clonedSvg.setAttribute('width', String(widthPx))
  clonedSvg.setAttribute('height', String(heightPx))
  clonedSvg.style.width = `${widthPx}px`
  clonedSvg.style.height = `${heightPx}px`
  clonedSvg.setAttribute('x', '0')
  clonedSvg.setAttribute('y', '0')

  return clonedSvg
}

function applyPanelSpecificFigmaFallbacks(
  panelEl: HTMLElement,
  sourceSvg: SVGSVGElement,
  clonedSvg: SVGSVGElement,
): void {
  if (panelEl.classList.contains('afg-panel')) {
    const sourceEdgeTexts = Array.from(sourceSvg.querySelectorAll<SVGTextElement>('.edge text'))
    const clonedEdgeTexts = Array.from(clonedSvg.querySelectorAll<SVGTextElement>('.edge text'))
    const edgeTextCount = Math.min(sourceEdgeTexts.length, clonedEdgeTexts.length)
    for (let i = 0; i < edgeTextCount; i += 1) {
      const sourceText = sourceEdgeTexts[i]
      const clonedText = clonedEdgeTexts[i]
      const computed = window.getComputedStyle(sourceText)
      const fill = computed.getPropertyValue('fill').trim()
      if (fill) {
        clonedText.setAttribute('fill', fill)
        clonedText.style.fill = fill
      }
    }
  }

  if (panelEl.classList.contains('cfg-panel')) {
    clonedSvg.querySelectorAll<SVGElement>('.node.selected ellipse, .node.selected polygon, .node.selected path, .node.selected rect')
      .forEach((shape) => {
        const width = Number.parseFloat(shape.getAttribute('stroke-width') || '0')
        if (!Number.isFinite(width) || width < 3.2) {
          shape.setAttribute('stroke-width', '3.4')
        }
        if (!shape.getAttribute('stroke') || isTransparentColor(shape.getAttribute('stroke'))) {
          shape.setAttribute('stroke', '#7B8FAD')
        }
      })

    clonedSvg.querySelectorAll<SVGElement>('.node.highlighted ellipse, .node.highlighted polygon, .node.highlighted path, .node.highlighted rect')
      .forEach((shape) => {
        const width = Number.parseFloat(shape.getAttribute('stroke-width') || '0')
        if (!Number.isFinite(width) || width < 2.6) {
          shape.setAttribute('stroke-width', '3')
        }
      })

    clonedSvg.querySelectorAll<SVGRectElement>('#swap-highlight-layer .swap-highlight-box')
      .forEach((rect) => {
        rect.setAttribute('fill', 'rgba(255, 59, 48, 0.12)')
        rect.setAttribute('stroke', '#b91c1c')
        rect.setAttribute('stroke-width', '2.8')
      })
  }
}

function appendPanelSvgLayers(
  outputSvg: SVGSVGElement,
  panelEl: HTMLElement,
  panelRect: RectSnapshot,
  rootRect: RectSnapshot,
): void {
  const bakeNonScalingStroke = panelEl.classList.contains('cfg-panel')
  const sourceSvgs = Array.from(panelEl.querySelectorAll('svg')) as SVGSVGElement[]
  for (const sourceSvg of sourceSvgs) {
    if (sourceSvg.parentElement?.closest('svg')) continue
    const sourceStyle = window.getComputedStyle(sourceSvg)
    if (sourceStyle.display === 'none' || sourceStyle.visibility === 'hidden' || Number.parseFloat(sourceStyle.opacity) <= 0) {
      continue
    }

    const sourceRect = rectFromDomRect(sourceSvg.getBoundingClientRect())
    if (!hasDrawableArea(sourceRect)) continue

    const clipContainer = sourceSvg.closest('.graph-viewport, .plot-area') as HTMLElement | null
    const clipRect = clipContainer ? rectFromDomRect(clipContainer.getBoundingClientRect()) : sourceRect
    const panelClip = intersectRects(clipRect, panelRect)
    const visibleRect = panelClip ? intersectRects(panelClip, sourceRect) : null
    if (!visibleRect) continue

    const snapshotSvg = buildSvgSnapshotFromScreenRect(sourceSvg, visibleRect, { bakeNonScalingStroke })
    if (!snapshotSvg) continue
    applyPanelSpecificFigmaFallbacks(panelEl, sourceSvg, snapshotSvg)

    const layerGroup = createSvgElement('g')
    layerGroup.setAttribute(
      'transform',
      `translate(${toFiniteSvgNumber(visibleRect.left - rootRect.left)} ${toFiniteSvgNumber(visibleRect.top - rootRect.top)})`,
    )
    layerGroup.appendChild(snapshotSvg)
    outputSvg.appendChild(layerGroup)
  }
}

function appendPanelTextLayers(
  outputSvg: SVGSVGElement,
  panelEl: HTMLElement,
  panelRect: RectSnapshot,
  rootRect: RectSnapshot,
): void {
  const walker = document.createTreeWalker(
    panelEl,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node: Node) => {
        const textNode = node as Text
        const text = (textNode.textContent || '').replace(/\s+/g, ' ').trim()
        if (!text) return NodeFilter.FILTER_REJECT

        const parent = textNode.parentElement
        if (!parent) return NodeFilter.FILTER_REJECT
        if (parent.closest('svg')) return NodeFilter.FILTER_REJECT
        if (parent.closest('.title-bar .actions')) return NodeFilter.FILTER_REJECT

        const style = window.getComputedStyle(parent)
        if (style.display === 'none' || style.visibility === 'hidden' || Number.parseFloat(style.opacity) <= 0) {
          return NodeFilter.FILTER_REJECT
        }
        return NodeFilter.FILTER_ACCEPT
      },
    },
  )

  let count = 0
  let current = walker.nextNode()
  while (current && count < MAX_FULL_UI_TEXT_NODES) {
    const textNode = current as Text
    const parent = textNode.parentElement
    if (parent) {
      const textRect = getVisibleTextRect(textNode)
      if (textRect) {
        const clippedRect = intersectRects(textRect, panelRect)
        if (clippedRect && hasDrawableArea(clippedRect)) {
          const style = window.getComputedStyle(parent)
          const rawText = (textNode.textContent || '').replace(/\s+/g, ' ').trim()
          const text = applyTextTransform(rawText, style.textTransform)
          const fontSize = Math.max(8, parsePx(style.fontSize, 12))
          const x = clippedRect.left - rootRect.left
          const y = clippedRect.top - rootRect.top + fontSize * 0.82

          const textEl = createSvgElement('text')
          textEl.textContent = text
          textEl.setAttribute('x', toFiniteSvgNumber(x))
          textEl.setAttribute('y', toFiniteSvgNumber(y))
          textEl.setAttribute('fill', style.color || '#000000')
          textEl.setAttribute('font-family', style.fontFamily || 'sans-serif')
          textEl.setAttribute('font-size', toFiniteSvgNumber(fontSize))
          textEl.setAttribute('font-weight', style.fontWeight || '400')
          if (style.fontStyle && style.fontStyle !== 'normal') {
            textEl.setAttribute('font-style', style.fontStyle)
          }
          if (style.letterSpacing && style.letterSpacing !== 'normal') {
            textEl.setAttribute('letter-spacing', style.letterSpacing)
          }
          outputSvg.appendChild(textEl)
          count += 1
        }
      }
    }
    current = walker.nextNode()
  }
}

function exportDomElementAsSvg(targetEl: HTMLElement, outputName: string): { ok: true } | { ok: false, reason: string } {
  try {
    const rootRect = rectFromDomRect(targetEl.getBoundingClientRect())
    const widthPx = Math.max(1, Math.round(rootRect.width))
    const heightPx = Math.max(1, Math.round(rootRect.height))

    const svg = createSvgElement('svg')
    svg.setAttribute('xmlns', SVG_NS)
    svg.setAttribute('xmlns:xlink', XLINK_NS)
    svg.setAttribute('width', String(widthPx))
    svg.setAttribute('height', String(heightPx))
    svg.setAttribute('viewBox', `0 0 ${widthPx} ${heightPx}`)

    const appStyle = window.getComputedStyle(targetEl)
    const bgRect = createSvgElement('rect')
    bgRect.setAttribute('x', '0')
    bgRect.setAttribute('y', '0')
    bgRect.setAttribute('width', String(widthPx))
    bgRect.setAttribute('height', String(heightPx))
    bgRect.setAttribute('fill', !isTransparentColor(appStyle.backgroundColor) ? appStyle.backgroundColor : '#FAFAFA')
    svg.appendChild(bgRect)

    const panelElements: HTMLElement[] = []
    const seen = new Set<HTMLElement>()
    for (const selector of FULL_UI_EXPORT_PANEL_SELECTORS) {
      const panel = targetEl.querySelector(selector) as HTMLElement | null
      if (!panel || seen.has(panel)) continue
      seen.add(panel)
      panelElements.push(panel)
    }

    const panelRects = new Map<HTMLElement, RectSnapshot>()
    panelElements.forEach((panel) => {
      const panelRect = appendPanelFrame(svg, panel, rootRect)
      if (panelRect) panelRects.set(panel, panelRect)
    })

    panelElements.forEach((panel) => {
      const panelRect = panelRects.get(panel)
      if (!panelRect) return
      appendPanelControlLayers(svg, panel, panelRect, rootRect)
    })

    panelElements.forEach((panel) => {
      const panelRect = panelRects.get(panel)
      if (!panelRect) return
      appendPanelSvgLayers(svg, panel, panelRect, rootRect)
    })

    panelElements.forEach((panel) => {
      const panelRect = panelRects.get(panel)
      if (!panelRect) return
      appendPanelTextLayers(svg, panel, panelRect, rootRect)
    })

    const serialized = new XMLSerializer().serializeToString(svg)
    downloadSvgText(serialized, outputName)
    return { ok: true }
  } catch (error: any) {
    return { ok: false, reason: error?.message || 'full UI export failed' }
  }
}

function exportVisibleViewportSvg(options: {
  panelName: string
  panelSelector: string
  viewportSelector: string
  outputName: string
}): { ok: true } | { ok: false, reason: string } {
  try {
    const panelEl = document.querySelector(options.panelSelector) as HTMLElement | null
    if (!panelEl) {
      return { ok: false, reason: `${options.panelName}: panel not found` }
    }

    const viewportEl = panelEl.querySelector(options.viewportSelector) as HTMLElement | null
    if (!viewportEl) {
      return { ok: false, reason: `${options.panelName}: viewport not found` }
    }

    const sourceSvg = viewportEl.querySelector('svg') as SVGSVGElement | null
    if (!sourceSvg) {
      return { ok: false, reason: `${options.panelName}: no SVG rendered` }
    }

    const screenMatrix = sourceSvg.getScreenCTM()
    if (!screenMatrix) {
      return { ok: false, reason: `${options.panelName}: failed to read SVG transform matrix` }
    }
    const inverseMatrix = screenMatrix.inverse()

    const viewportRect = viewportEl.getBoundingClientRect()
    const topLeft = toSvgPoint(sourceSvg, viewportRect.left, viewportRect.top, inverseMatrix)
    const topRight = toSvgPoint(sourceSvg, viewportRect.right, viewportRect.top, inverseMatrix)
    const bottomLeft = toSvgPoint(sourceSvg, viewportRect.left, viewportRect.bottom, inverseMatrix)
    const bottomRight = toSvgPoint(sourceSvg, viewportRect.right, viewportRect.bottom, inverseMatrix)

    const minX = Math.min(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x)
    const maxX = Math.max(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x)
    const minY = Math.min(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y)
    const maxY = Math.max(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y)

    const viewBoxWidth = Math.max(1, maxX - minX)
    const viewBoxHeight = Math.max(1, maxY - minY)
    const widthPx = Math.max(1, Math.round(viewportRect.width))
    const heightPx = Math.max(1, Math.round(viewportRect.height))
    const shouldBakeCfgStroke = panelEl.classList.contains('cfg-panel')

    const clonedSvg = sourceSvg.cloneNode(true) as SVGSVGElement
    stripCommentNodes(clonedSvg)
    if (shouldBakeCfgStroke) {
      const strokeScale = Math.max(0.0001, Math.min(widthPx / viewBoxWidth, heightPx / viewBoxHeight))
      inlineComputedSvgStyles(sourceSvg, clonedSvg, {
        strokeScale,
        stripVectorEffect: true,
        bakeStrokeWidth: true,
      })
    } else {
      inlineComputedSvgStyles(sourceSvg, clonedSvg)
    }
    applyPanelSpecificFigmaFallbacks(panelEl, sourceSvg, clonedSvg)

    clonedSvg.setAttribute('xmlns', SVG_NS)
    clonedSvg.setAttribute('xmlns:xlink', XLINK_NS)
    clonedSvg.setAttribute(
      'viewBox',
      `${formatViewBoxNumber(minX)} ${formatViewBoxNumber(minY)} ${formatViewBoxNumber(viewBoxWidth)} ${formatViewBoxNumber(viewBoxHeight)}`,
    )
    clonedSvg.setAttribute('width', String(widthPx))
    clonedSvg.setAttribute('height', String(heightPx))
    clonedSvg.style.width = `${widthPx}px`
    clonedSvg.style.height = `${heightPx}px`

    const serialized = new XMLSerializer().serializeToString(clonedSvg)
    downloadSvgText(serialized, options.outputName)
    return { ok: true }
  } catch (error: any) {
    return { ok: false, reason: `${options.panelName}: ${error?.message || 'export failed'}` }
  }
}

function buildExportPrefix() {
  if (!currentTxHash.value) return 'traceweaver-no-tx'
  return `traceweaver-${currentTxHash.value.slice(2, 10)}`
}

function handleExportVisibleSvgs() {
  if (exportInProgress.value) return
  exportInProgress.value = true

  try {
    const prefix = buildExportPrefix()
    const specs = [
      {
        panelName: 'Asset Flow Graph',
        panelSelector: '.afg-panel',
        viewportSelector: '.graph-viewport',
        outputName: `${prefix}-asset-flow-viewport.svg`,
      },
      {
        panelName: 'Control Flow Graph',
        panelSelector: '.cfg-panel',
        viewportSelector: '.graph-viewport',
        outputName: `${prefix}-control-flow-viewport.svg`,
      },
      {
        panelName: 'Sequence Diagram',
        panelSelector: '.sequence-panel',
        viewportSelector: '.graph-viewport',
        outputName: `${prefix}-sequence-viewport.svg`,
      },
    ] as const

    const failures: string[] = []
    specs.forEach((spec) => {
      const result = exportVisibleViewportSvg(spec)
      if (!result.ok) failures.push(result.reason)
    })

    if (failures.length === specs.length) {
      window.alert(`No SVG view is ready to export.\n${failures.join('\n')}`)
      return
    }

    if (failures.length > 0) {
      window.alert(`Some views could not be exported:\n${failures.join('\n')}`)
    }
  } finally {
    exportInProgress.value = false
  }
}

function handleExportFullUiSvg() {
  if (exportInProgress.value) return
  exportInProgress.value = true

  try {
    const appGridEl = document.querySelector('.app-grid') as HTMLElement | null
    if (!appGridEl) {
      window.alert('Full UI export failed: app container not found.')
      return
    }

    const prefix = buildExportPrefix()
    const result = exportDomElementAsSvg(appGridEl, `${prefix}-full-ui.svg`)
    if (!result.ok) {
      window.alert(`Full UI export failed: ${result.reason}`)
    }
  } finally {
    exportInProgress.value = false
  }
}
</script>

<template>
  <div class="app-grid">
    <TitleBar
      class="title-bar"
      :export-disabled="exportInProgress"
      @export-visible-svgs="handleExportVisibleSvgs"
      @export-full-ui-svg="handleExportFullUiSvg"
    />
    <div class="left-col">
      <InputPanel
        ref="inputPanelRef"
        class="input-panel"
        @analysis-progress="handleAnalysisProgress"
        @analysis-complete="handleAnalysisComplete"
        @block-number-changed="handleBlockNumberChanged"
      />
      <BlockPanel
        class="block-panel"
        :block-number="currentBlockNumber"
        :selected-tx-hash="currentTxHash"
        :arbitrage-tx-hashes="arbitrageTxHashes"
        :arbitrage-block-numbers="arbitrageBlockNumbers"
        @transaction-selected="handleTransactionSelected"
        @block-selected="handleBlockSelected"
        @latest-block="handleLatestBlock"
        @latest-blocks-refreshed="handleLatestBlocksRefreshed"
      />
    </div>
    <div class="right-col">
      <div class="top-row">
        <AfgPanel
          class="afg-panel"
          :tx-hash="currentTxHash"
          :cfg-mode="currentCfgMode"
          :highlighted-block-id="highlightedBlockId"
          :is-analyzing="isAnalyzing && !isStageReady('afg')"
          @cfg-navigate="handleCfgNavigate"
        />
        <SequencePanel
          class="sequence-panel"
          :tx-hash="isStageReady('sequence') ? currentTxHash : null"
          :is-analyzing="isAnalyzing && !isStageReady('sequence')"
          :selected-step-range="sequenceStepRange"
          @sequence-select="handleSequenceSelect"
        />
        <LegendPanel
          class="legend-panel"
          :tx-hash="isStageReady('afg') ? currentTxHash : null"
          :is-analyzing="isAnalyzing && !isStageReady('afg')"
        />
      </div>
      <CfgPanel
        class="cfg-panel"
        :tx-hash="isStageReady('folded_cfg') ? currentTxHash : null"
        :highlighted-block-id="highlightedBlockId"
        :filtered-edge-ids="filteredEdgeIds"
        :is-analyzing="isAnalyzing && !isStageReady('folded_cfg')"
        :plain-ready="isStageReady('plain_cfg')"
        :edge-step-map="edgeStepMap"
        :preferred-mode="currentCfgMode"
        @cfg-navigate="handleCfgNavigate"
        @mode-change="handleCfgModeChange"
      />
    </div>
  </div>
</template>

<style scoped>
.app-grid {
  height: 100vh;
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(0, 4fr);
  grid-template-rows: 36px 1fr;
  gap: 6px;
  background: var(--bg);
  overflow: hidden;
  padding: 6px;
  box-sizing: border-box;
}

.title-bar {
  grid-column: 1 / -1;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.10), 0 0 0 1px rgba(0,0,0,0.06);
}

.left-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg);
  overflow: visible;
  min-height: 0;
  min-width: 180px;
}

.input-panel {
  flex-shrink: 0;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
  overflow: hidden;
}

.block-panel {
  flex: 1;
  min-height: 0;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.right-col {
  display: grid;
  grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
  gap: 6px;
  background: var(--bg);
  min-height: 0;
  overflow: visible;
}

.top-row {
  display: grid;
  grid-template-columns: minmax(0, 1.04fr) minmax(0, 1.22fr) minmax(160px, 196px);
  min-height: 0;
  gap: 6px;
}

.afg-panel {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.sequence-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.legend-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

.cfg-panel {
  min-height: 0;
  overflow: hidden;
  border-radius: 3px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.05);
}

@media (max-width: 1180px) {
  .app-grid {
    grid-template-columns: minmax(190px, 1fr) minmax(0, 3.8fr);
  }

  .top-row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(140px, 170px);
  }
}
</style>
