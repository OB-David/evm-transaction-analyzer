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
