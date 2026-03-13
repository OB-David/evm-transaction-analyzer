export interface AnalyzeResult {
  status: string
  result_dir: string
  files: string[]
  error?: string | null
}

export interface EdgeLink {
  edge_id: number
  type: 'ETH_TRANSFER' | 'ERC20_TOKEN_TRANSFER' | 'ERC20_BALANCE_CHANGE'
  matched_blocks: number | number[] | {
    sender: number[]
    receiver: number[]
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
  const res = await fetch(`${API_BASE}/api/files/${txHash}/asset_flow.dot`)

  if (!res.ok) {
    throw new Error(`Failed to fetch DOT file: ${res.status}`)
  }

  return res.text()
}

export async function fetchCfgDotFile(txHash: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/transaction_cfg.dot`)

  if (!res.ok) {
    throw new Error(`Failed to fetch CFG DOT file: ${res.status}`)
  }

  return res.text()
}

export async function fetchEdgeLink(txHash: string): Promise<EdgeLink[]> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/edge_link.json`)

  if (!res.ok) {
    throw new Error(`Failed to fetch edge link: ${res.status}`)
  }

  return res.json()
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

export interface BlockInformation {
  block_id: number
  address: string
  blocks_number: number
  start_pc: string
  end_pc: string
  gas: number
  actions: string[]
  instructions: string[]
}

export interface BlockInformationMap {
  [blockId: string]: BlockInformation
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
  const res = await fetch(`${API_BASE}/api/files/${txHash}/legend.json`)

  if (!res.ok) {
    throw new Error(`Failed to fetch legend data: ${res.status}`)
  }

  return res.json()
}

export async function fetchBlockInformation(txHash: string): Promise<BlockInformationMap> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/folded_blocks_information.json`)

  if (!res.ok) {
    throw new Error(`Failed to fetch block information: ${res.status}`)
  }

  return res.json()
}

export interface EdgeStepEntry {
  edge_id: string
  edge_step: number
}

export type EdgeStepMap = Record<string, EdgeStepEntry>

export async function fetchFlameGraphSvg(txHash: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/trace_flame_graph.svg`)

  if (!res.ok) {
    throw new Error(`Failed to fetch flame graph: ${res.status}`)
  }

  return res.text()
}

export async function fetchEdgeStepMap(txHash: string): Promise<EdgeStepMap> {
  const res = await fetch(`${API_BASE}/api/files/${txHash}/edge_id-step.json`)

  if (!res.ok) {
    throw new Error(`Failed to fetch edge step map: ${res.status}`)
  }

  return res.json()
}
