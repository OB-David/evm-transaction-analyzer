export interface AnalyzeResult {
  status: string
  result_dir: string
  files: string[]
  error?: string | null
}

export type BlockId = string | number

export interface EdgeLink {
  edge_id: number
  type: 'ETH_TRANSFER' | 'ERC20_TOKEN_TRANSFER' | 'ERC20_BALANCE_CHANGE'
  matched_blocks: BlockId | BlockId[] | {
    sender: BlockId[]
    receiver: BlockId[]
  }
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

const API_BASE = 'http://localhost:8000'

async function fetchTextFile(filename: string, txHash: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`)
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.text()
}

async function fetchJsonFile<T>(filename: string, txHash: string): Promise<T> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`)
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.json()
}

export async function analyzeTransaction(txHash: string): Promise<AnalyzeResult> {
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tx_hash: txHash }),
  })

  if (!res.ok) {
    throw new Error(`Server error: ${res.status}`)
  }

  return res.json()
}

export async function fetchDotFile(txHash: string): Promise<string> {
  return fetchTextFile('asset_flow.dot', txHash)
}

export async function fetchCfgDotFile(txHash: string): Promise<string> {
  return fetchTextFile('folded_cfg.dot', txHash)
}

export async function fetchCfgSvg(txHash: string): Promise<string> {
  return fetchTextFile('folded_cfg.svg', txHash)
}

export async function fetchEdgeLink(txHash: string, mode: CfgMode = 'folded'): Promise<EdgeLink[]> {
  const filename = mode === 'plain' ? 'TFG_link_PCFG.json' : 'TFG_link_FCFG.json'
  return fetchJsonFile<EdgeLink[]>(filename, txHash)
}

export async function fetchBlockGasData(blockNumber: number): Promise<BlockGasData> {
  const res = await fetch(`${API_BASE}/api/block`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ block_number: blockNumber }),
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

export async function fetchBlocksHeatmap(offset: number = 0, count: number = 160): Promise<BlocksHeatmapData> {
  const res = await fetch(`${API_BASE}/api/blocks?offset=${offset}&count=${count}`)

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

export interface BlockInformation {
  block_id: BlockId
  address: string
  blocks_number: number
  gas: number
  actions: BlockAction[]
  start_step?: number
  end_step?: number
  folded_blocks?: BlockId[]
  instructions?: string[]
  start_pc?: string
  end_pc?: string
}

export interface BlockInformationMap {
  [blockId: string]: BlockInformation
}

export type CfgMode = 'folded' | 'plain'

export interface CfgViewData {
  mode: CfgMode
  svgContent: string
  blockInformation: BlockInformationMap
}

export interface CfgViewBundle {
  initialMode: CfgMode
  folded: CfgViewData
  plain: CfgViewData
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
  const filename = mode === 'plain' ? 'plain_cfg.svg' : 'folded_cfg.svg'
  return fetchTextFile(filename, txHash)
}

export async function fetchCfgViewData(txHash: string, preferredMode: CfgMode = 'folded'): Promise<CfgViewBundle> {
  const [foldedSvgContent, plainSvgContent, foldedBlockInformation, plainBlockInformation] = await Promise.all([
    fetchCfgSvgByMode(txHash, 'folded'),
    fetchCfgSvgByMode(txHash, 'plain'),
    fetchBlockInformation(txHash, 'folded'),
    fetchBlockInformation(txHash, 'plain'),
  ])

  return {
    initialMode: preferredMode === 'plain' ? 'plain' : 'folded',
    folded: {
      mode: 'folded',
      svgContent: foldedSvgContent,
      blockInformation: foldedBlockInformation,
    },
    plain: {
      mode: 'plain',
      svgContent: plainSvgContent,
      blockInformation: plainBlockInformation,
    },
  }
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

export interface SequenceCallEntry {
  call_id: number
  entry_step: number
  exit_step: number
  entry_op: string
  exit_op: string
  from_name: string
  to_name: string
  calldata: string[]
}

export interface SequenceCalldataMapping {
  total_calls: number
  calls: SequenceCallEntry[]
}

export async function fetchSequenceSvg(txHash: string): Promise<string> {
  return fetchTextFile('trace_sequence.svg', txHash)
}

export async function fetchSequenceCalldataMapping(txHash: string): Promise<SequenceCalldataMapping> {
  return fetchJsonFile<SequenceCalldataMapping>('trace_sequence_calldata_mapping.json', txHash)
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
  const res = await fetch(`${API_BASE}/api/files/${txHash}/arbitrage.json`)
  if (!res.ok) {
    if (res.status === 404) {
      return { is_arbitrage: false, cycles: [], arb_edge_orders: [] }
    }
    throw new Error(`Failed to fetch arbitrage result: ${res.status}`)
  }
  return res.json()
}

export async function fetchSwapPatternResult(txHash: string, mode: CfgMode = 'folded'): Promise<SwapPatternResult> {
  const filename = mode === 'plain' ? 'swap_in_pcfg.json' : 'swap_in_fcfg.json'
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`)
  if (!res.ok) {
    if (res.status === 404) {
      return { pattern_1: [], pattern_2: [] }
    }
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.json()
}

export async function fetchAddressBalances(txHash: string): Promise<Record<string, Record<string, number>>> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/address_balances.json`)
  if (!res.ok) {
    if (res.status === 404) return {}
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
