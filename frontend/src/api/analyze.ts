export interface AnalyzeResult {
  status: string
  stage: AnalysisStage
  result_dir: string
  files: string[]
  error?: string | null
}

export type AnalysisStage = 'queued' | 'analyzing' | 'afg' | 'sequence' | 'folded_cfg' | 'plain_cfg' | 'folded_info' | 'complete' | 'error'

const ANALYSIS_STAGE_ORDER: Record<AnalysisStage, number> = {
  queued: 0,
  analyzing: 1,
  afg: 2,
  sequence: 3,
  folded_cfg: 4,
  plain_cfg: 5,
  folded_info: 6,
  complete: 7,
  error: -1,
}

export function analysisStageReached(current: AnalysisStage, target: AnalysisStage): boolean {
  return ANALYSIS_STAGE_ORDER[current] >= ANALYSIS_STAGE_ORDER[target]
}

export type BlockId = string | number

export type EdgeLinkMappingStatus = 'complete' | 'partial' | 'unmatched' | 'ambiguous'

export interface EdgeLinkEvidence {
  role: string
  source_step: number | null
  block_id?: BlockId | null
  call_id?: number | null
  contract_address?: string | null
  status: 'matched' | 'unmatched' | 'ambiguous'
}

export interface EdgeLink {
  schema_version?: number
  edge_id: number
  type: 'ETH_TRANSFER' | 'ERC20_TOKEN_TRANSFER' | 'ERC20_BALANCE_CHANGE'
  mapping_status?: EdgeLinkMappingStatus
  evidence?: EdgeLinkEvidence[]
  matched_blocks: BlockId | BlockId[] | {
    sender: BlockId[]
    receiver: BlockId[]
  }
}

export interface CallTreeLinkMatch {
  call_id: number | null
  contract_address: string
}

export interface CallTreeEdgeLink {
  schema_version?: number
  edge_id: number
  type: EdgeLink['type']
  mapping_status?: EdgeLinkMappingStatus
  evidence?: EdgeLinkEvidence[]
  matched_calls: CallTreeLinkMatch[]
  matched_contracts: string[]
}

export interface AfgNavigationTarget {
  blockIds: BlockId[]
  callIds: number[]
  contractAddresses: string[]
  includesRootCall: boolean
}

export interface TransactionGasInfo {
  index: number
  hash: string
  gas: number
  log_gas: number
  gas_price_gwei: number
  from_addr: string
  to_addr: string | null
  x: number
  y: number
}

export interface BlockGasData {
  status: string
  block_number: number
  miner: string
  transaction_count: number
  transactions: TransactionGasInfo[]
  error?: string | null
}

const configuredApiBase = import.meta.env.VITE_API_BASE || ''
const API_BASE = configuredApiBase.endsWith('/') ? configuredApiBase.slice(0, -1) : configuredApiBase

async function fetchTextFile(filename: string, txHash: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.text()
}

async function fetchJsonFile<T>(filename: string, txHash: string): Promise<T> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.json()
}

async function fetchAnalysisProgress(txHash: string, signal?: AbortSignal): Promise<AnalyzeResult> {
  const res = await fetch(`${API_BASE}/api/analyze/${txHash}/progress`, { signal, cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch analysis progress: ${res.status}`)
  }
  return res.json()
}

function waitForPoll(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Analysis cancelled', 'AbortError'))
      return
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, milliseconds)
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('Analysis cancelled', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export async function analyzeTransaction(
  txHash: string,
  onProgress?: (result: AnalyzeResult) => void,
  signal?: AbortSignal,
): Promise<AnalyzeResult> {
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tx_hash: txHash }),
    signal,
  })

  if (!res.ok) {
    throw new Error(`Server error: ${res.status}`)
  }

  let result: AnalyzeResult = await res.json()
  onProgress?.(result)
  while (result.status === 'processing') {
    await waitForPoll(250, signal)
    result = await fetchAnalysisProgress(txHash, signal)
    onProgress?.(result)
  }
  return result
}

export interface CancelAnalysisResult {
  status: 'cancelled' | 'not_running'
  tx_hash: string
  cleaned: boolean
}

export async function cancelAnalysis(txHash: string): Promise<CancelAnalysisResult> {
  const res = await fetch(`${API_BASE}/api/analyze/${txHash}`, { method: 'DELETE' })
  if (!res.ok) {
    throw new Error(`Failed to cancel analysis: ${res.status}`)
  }
  return res.json()
}

export async function fetchDotFile(txHash: string): Promise<string> {
  return fetchTextFile('asset_flow.dot', txHash)
}

export async function fetchCfgSvg(txHash: string): Promise<string> {
  return fetchCfgSvgByMode(txHash, 'folded')
}

export interface LinkArtifact {
  schema_version: number
  edge_links: {
    folded: EdgeLink[]
    plain: EdgeLink[]
    call_tree: CallTreeEdgeLink[]
  }
}

export async function fetchEdgeLink(txHash: string, mode: CfgMode = 'folded'): Promise<EdgeLink[]> {
  const artifact = await fetchJsonFile<LinkArtifact>('link.json', txHash)
  if (artifact.schema_version !== 1 || !Array.isArray(artifact.edge_links?.[mode])) {
    throw new Error('Unsupported link.json schema')
  }
  return artifact.edge_links[mode]
}

export async function fetchCallTreeEdgeLink(txHash: string): Promise<CallTreeEdgeLink[]> {
  const artifact = await fetchJsonFile<LinkArtifact>('link.json', txHash)
  if (artifact.schema_version !== 1 || !Array.isArray(artifact.edge_links?.call_tree)) {
    throw new Error('Unsupported link.json call-tree mapping schema')
  }
  return artifact.edge_links.call_tree
}

export async function fetchBlockGasData(blockNumber: number, signal?: AbortSignal): Promise<BlockGasData> {
  const res = await fetch(`${API_BASE}/api/block`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ block_number: blockNumber }),
    signal,
  })

  if (!res.ok) {
    throw new Error(`Failed to fetch block gas data: ${res.status}`)
  }

  return res.json()
}

export async function fetchTransactionBlock(txHash: string): Promise<number> {
  const res = await fetch(`${API_BASE}/api/transaction/${txHash}/block`)

  if (!res.ok) {
    throw new Error(`Failed to fetch transaction block: ${res.status}`)
  }

  const data = await res.json()
  return data.block_number
}

export interface BlockSummaryInfo {
  block_number: number
  avg_gas: number
  base_fee: number
  tx_count: number
  x: number
  y: number
}

export interface BlocksHeatmapData {
  status: string
  latest_block: number
  latest_block_timestamp: number
  page_timestamp: number
  blocks: BlockSummaryInfo[]
  error?: string | null
}

export async function fetchBlocksHeatmap(
  offset: number = 0,
  count: number = 160,
  signal?: AbortSignal,
): Promise<BlocksHeatmapData> {
  const res = await fetch(`${API_BASE}/api/blocks?offset=${offset}&count=${count}`, { signal })

  if (!res.ok) {
    throw new Error(`Failed to fetch blocks heatmap: ${res.status}`)
  }

  return res.json()
}

export interface Erc20Event {
  tokenname: string
  token_address?: string
  decimals?: number
  type: string
  user: string
  balance: string
}

export interface EthEvent {
  type: string
  from: string
  to: string
  amount: string
}

export interface BlockAction {
  action_type: string
  erc20_events: Erc20Event[]
  send_eth: string
  eth_event: EthEvent | null
}

export interface StepRange {
  start_step: number
  end_step: number
}

export interface BlockInformation {
  schema_version?: number
  block_id: BlockId
  address: string
  blocks_number: number
  gas: number
  actions: BlockAction[]
  start_step?: number
  end_step?: number
  step_ranges?: StepRange[]
  folded_blocks?: BlockId[]
  instructions?: string[]
}

export interface BlockInformationMap {
  [blockId: string]: BlockInformation
}

export type CfgMode = 'folded' | 'plain'

export interface CfgViewData {
  mode: CfgMode
  svgContent: string
  blockInformation?: BlockInformationMap
}

export interface PlainBlockLlmAnalysisRequest {
  tx_hash: string
  block_id: BlockId
  force_refresh?: boolean
}

export interface PlainBlockLlmAnalysisContent {
  title: string
  description: string
}

export interface PlainBlockStepRange {
  block_id: BlockId
  start_step: number
  end_step: number
}

export interface PlainBlockLlmContextMeta {
  target_block_id: BlockId
  prev_block_id: BlockId | null
  next_block_id: BlockId | null
  step_ranges: {
    prev: PlainBlockStepRange | null
    target: PlainBlockStepRange
    next: PlainBlockStepRange | null
  }
}

export interface PlainBlockLlmAnalysisResponse {
  status: 'success'
  source: 'cache' | 'llm'
  analysis: PlainBlockLlmAnalysisContent
  context_meta: PlainBlockLlmContextMeta
}

export interface LegendEntry {
  name: string
  address: string
  color?: string
}

export interface LegendData {
  user_addresses: LegendEntry[]
  erc20_tokens: LegendEntry[]
  normal_contracts: LegendEntry[]
}

export async function fetchLegendData(txHash: string): Promise<LegendData> {
  return fetchJsonFile<LegendData>('legend.json', txHash)
}

export async function fetchBlockInformation(txHash: string, mode: CfgMode = 'folded'): Promise<BlockInformationMap> {
  const filename = mode === 'plain' ? 'plain_blocks_information.json' : 'folded_blocks_information.json'
  return fetchJsonFile<BlockInformationMap>(filename, txHash)
}

async function fetchCfgSvgByMode(txHash: string, mode: CfgMode): Promise<string> {
  const res = await fetch(`${API_BASE}/api/cfg/${txHash}/${mode}.svg`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to render ${mode} CFG: ${res.status}`)
  }
  return res.text()
}

export async function fetchCfgViewData(txHash: string, mode: CfgMode = 'folded'): Promise<CfgViewData> {
  const svgContent = await fetchCfgSvgByMode(txHash, mode)
  return { mode, svgContent }
}

export async function fetchPlainBlockLlmAnalysis(
  txHash: string,
  blockId: BlockId,
  forceRefresh: boolean = false,
): Promise<PlainBlockLlmAnalysisResponse> {
  const payload: PlainBlockLlmAnalysisRequest = {
    tx_hash: txHash,
    block_id: blockId,
    force_refresh: forceRefresh,
  }

  const res = await fetch(`${API_BASE}/api/llm/plain-block-analysis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })

  let body: any = null
  try {
    body = await res.json()
  } catch {
    body = null
  }

  if (!res.ok) {
    const detail = body?.detail || `Failed to fetch plain block LLM analysis: ${res.status}`
    throw new Error(detail)
  }

  return body as PlainBlockLlmAnalysisResponse
}

export interface EdgeStepEntry {
  edge_id: string
  edge_step: number
  source_node: string
  target_node: string
}

export type EdgeStepMap = Record<string, EdgeStepEntry>

export interface CallTreeEntry {
  call_id: number
  parent_call_id: number | null
  depth: number
  entry_step: number
  exit_step: number
  entry_op: string
  exit_op: string
  from_address: string
  to_address: string
  from_name: string
  to_name: string
  selector?: string
  calldata: string[]
  probable_text_signatures?: string[]
}

export interface CallTreePayload {
  schema_version: number
  root: {
    address: string
    name: string
  }
  calls: CallTreeEntry[]
}

export async function fetchCallTreeData(txHash: string): Promise<CallTreePayload> {
  const payload = await fetchJsonFile<CallTreePayload>('call_tree.json', txHash)
  if (payload.schema_version !== 1 || !payload.root || !Array.isArray(payload.calls)) {
    throw new Error('Unsupported call_tree.json schema')
  }
  return payload
}

export async function fetchEdgeStepMap(txHash: string, _preferredMode: CfgMode = 'folded'): Promise<EdgeStepMap> {
  return fetchJsonFile<EdgeStepMap>('edge_id-step.json', txHash)
}

export interface ArbitrageResult {
  is_arbitrage: boolean
  cycles: number[][]
  arb_edge_orders: number[]
}

export interface SwapPatternBlock {
  id: BlockId | string
  address: string
}

export interface SwapPatternResult {
  pattern_1: SwapPatternBlock[]
  pattern_2: SwapPatternBlock[]
}

export async function fetchArbitrageResult(txHash: string): Promise<ArbitrageResult> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/arbitrage.json`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch arbitrage result: ${res.status}`)
  }
  return res.json()
}

export function normalizeAnalyzeError(message?: string | null): string {
  const raw = (message || '').trim()
  if (!raw) return 'Analysis failed'

  const lowered = raw.toLowerCase()
  if (lowered.includes('eth transfer transaction is not supported')) {
    return 'This transaction is a plain ETH transfer, which is not supported by this analyzer.'
  }
  if (lowered.includes('contract creation transaction is not supported')) {
    return 'Contract creation transactions are not supported by this analyzer.'
  }
  if (lowered.includes('pipeline completed but result directory not found')) {
    return 'Analysis did not produce output files. This transaction type may not be supported.'
  }

  return raw
}

export async function fetchSwapPatternResult(txHash: string, mode: CfgMode = 'folded'): Promise<SwapPatternResult> {
  const filename = mode === 'plain' ? 'swap_in_pcfg.json' : 'swap_in_fcfg.json'
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.json()
}

export async function fetchAddressBalances(txHash: string): Promise<Record<string, Record<string, number>>> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/address_balances.json`, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`Failed to fetch address balances: ${res.status}`)
  }
  return res.json()
}

export interface ArbitrageTransaction {
  tx_hash: string
  block_number: number | null
}

export interface ArbitrageHashesData {
  transactions: ArbitrageTransaction[]
  fetched_at: string | null
  source: string
  query_id: number
}

export async function fetchArbitrageHashes(): Promise<ArbitrageHashesData> {
  const res = await fetch(`${API_BASE}/api/arbitrage-hashes`)
  if (!res.ok) throw new Error(`Failed to fetch arbitrage hashes: ${res.status}`)
  return res.json()
}

export async function triggerArbitrageRefresh(): Promise<void> {
  await fetch(`${API_BASE}/api/arbitrage-hashes/refresh`, { method: 'POST' })
}

export interface ArbitrageTransactionsData {
  transactions: Array<{
    tx_hash: string
    block_number: number
  }>
  history_start_block: number
  max_arbitrage_block: number | null
  initial_sync_complete: boolean
  coverage_complete: boolean
}

export async function fetchArbitrageTransactions(
  fromBlock: number,
  toBlock: number,
  signal?: AbortSignal,
): Promise<ArbitrageTransactionsData> {
  const params = new URLSearchParams({
    from_block: String(fromBlock),
    to_block: String(toBlock),
  })
  const res = await fetch(`${API_BASE}/api/arbitrage-transactions?${params}`, { signal })
  if (!res.ok) {
    throw new Error(`Failed to fetch local arbitrage markers: ${res.status}`)
  }
  return res.json()
}
