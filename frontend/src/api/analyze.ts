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

async function fetchOptionalTextFile(filename: string, txHash: string): Promise<string | null> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`)
  if (res.status === 404) return null
  if (!res.ok) {
    throw new Error(`Failed to fetch ${filename}: ${res.status}`)
  }
  return res.text()
}

async function fetchOptionalJsonFile<T>(filename: string, txHash: string): Promise<T | null> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/${filename}`)
  if (res.status === 404) return null
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
  return fetchTextFile('transaction_cfg.dot', txHash)
}

export async function fetchCfgSvg(txHash: string): Promise<string> {
  return fetchTextFile('transaction_cfg.svg', txHash)
}

export async function fetchEdgeLink(txHash: string): Promise<EdgeLink[]> {
  return fetchJsonFile<EdgeLink[]>('edge_link.json', txHash)
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
  start_pc: string
  end_pc: string
  gas: number
  actions: BlockAction[]
  instructions: string[]
}

export interface BlockInformationMap {
  [blockId: string]: BlockInformation
}

export interface SemanticNodeInformation {
  semantic_node_id: string
  label: string
  purpose: string
  confidence: number
  contract_address: string
  contract_name: string
  member_block_ids: BlockId[]
  blocks_number: number
  start_pc: string
  end_pc: string
  gas: number
  entry_conditions: string[]
  exit_effects: string[]
  entry_edge_types: string[]
  exit_edge_types: string[]
  trace_step_range: {
    entry_step: number | null
    exit_step: number | null
  }
  sequence_hint_step?: number | null
  sequence_index?: number
  actions: BlockAction[]
  block_opcode_sequences?: Array<{
    block_id: BlockId
    pc_range: string
    opcodes: string[]
    actions: BlockAction[]
  }>
  decision_signals?: {
    opcode_focus_counts: Record<string, number>
    external_calls: Array<Record<string, unknown>>
    state_reads: Array<Record<string, unknown>>
    state_writes: Array<Record<string, unknown>>
    terminal_behavior: Record<string, boolean>
  }
  trace_state_changes?: Array<{
    trace_index: number
    address: string
    pc: string
    opcode: string
    stack: string[]
    memory: string[]
    stack_change: {
      before: string[]
      after: string[]
    }
    memory_change: {
      before: string[]
      after: string[]
    }
  }>
  neighbor_context?: {
    previous: Array<Record<string, unknown>>
    next: Array<Record<string, unknown>>
  }
  contains_exceptional_terminate?: boolean
  is_routing_noise?: boolean
  member_blocks: BlockInformation[]
}

export type SemanticNodeMap = Record<string, SemanticNodeInformation>

export interface SemanticEdge {
  edge_id: string
  source_node: string
  target_node: string
  edge_types: string[]
  raw_edge_ids: string[]
  edge_steps: number[]
  min_edge_step?: number | null
  is_primary_path?: boolean
}

export interface SemanticCfgData {
  mode: 'semantic'
  model: string
  nodes: SemanticNodeMap
  edges: SemanticEdge[]
  raw_to_semantic: Record<string, string>
  background?: Record<string, unknown>
}

export type CfgMode = 'semantic' | 'folded'

export interface CfgViewData {
  mode: CfgMode
  svgContent: string
  semanticData: SemanticCfgData | null
  blockInformation: BlockInformationMap
}

export interface CfgViewBundle {
  initialMode: CfgMode
  semantic: CfgViewData | null
  folded: CfgViewData
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

export async function fetchBlockInformation(txHash: string): Promise<BlockInformationMap> {
  return fetchJsonFile<BlockInformationMap>('folded_blocks_information.json', txHash)
}

export async function fetchCfgViewData(txHash: string, preferredMode: CfgMode = 'semantic'): Promise<CfgViewBundle> {
  const [foldedSvgContent, foldedBlockInformation] = await Promise.all([
    fetchCfgSvg(txHash),
    fetchBlockInformation(txHash).catch(() => ({} as BlockInformationMap)),
  ])

  const folded: CfgViewData = {
    mode: 'folded',
    svgContent: foldedSvgContent,
    semanticData: null,
    blockInformation: foldedBlockInformation,
  }

  let semantic: CfgViewData | null = null

  try {
    const [semanticSvg, semanticData] = await Promise.all([
      fetchOptionalTextFile('semantic_cfg.svg', txHash),
      fetchOptionalJsonFile<SemanticCfgData>('semantic_cfg.json', txHash),
    ])

    if (semanticSvg && semanticData && semanticData.mode === 'semantic') {
      const blockInformation: BlockInformationMap = {}
      Object.values(semanticData.nodes).forEach((node) => {
        node.member_blocks.forEach((block) => {
          blockInformation[String(block.block_id)] = block
        })
      })

      semantic = {
        mode: 'semantic',
        svgContent: semanticSvg,
        semanticData,
        blockInformation,
      }
    }
  } catch (e) {
    console.warn('Semantic CFG unavailable, falling back to folded CFG.', e)
  }

  return {
    initialMode: preferredMode === 'semantic' && semantic ? 'semantic' : 'folded',
    semantic,
    folded,
  }
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

export async function fetchEdgeStepMap(txHash: string, preferredMode: CfgMode = 'semantic'): Promise<EdgeStepMap> {
  if (preferredMode === 'semantic') {
    try {
      const semanticMap = await fetchOptionalJsonFile<EdgeStepMap>('semantic_edge_id-step.json', txHash)
      if (semanticMap) {
        return semanticMap
      }
    } catch (e) {
      console.warn('Semantic edge-step map unavailable, falling back to folded edge map.', e)
    }
  }

  return fetchJsonFile<EdgeStepMap>('edge_id-step.json', txHash)
}

export interface ArbitrageResult {
  is_arbitrage: boolean
  cycles: number[][]
  arb_edge_orders: number[]
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
