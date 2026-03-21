# EVM Transaction Analyzer

```
evm-transaction-analyzer/
├── backend/    Python (FastAPI) analysis server
├── frontend/   Vue 3 + TypeScript visualization app
└── README.md
```

- **Backend** — Fetches transaction traces from an Ethereum node, disassembles contract bytecode into basic blocks, constructs transaction-level CFGs, extracts token transfer flows, and serves results via a FastAPI server.
- **Frontend** — Interactive dashboard using D3-Graphviz for CFG/AFG rendering and Plotly for block gas heatmap visualization.

## Prerequisites

- [UV](https://docs.astral.sh/uv/) (Python package manager, v0.4+)
- Python 3.12+
- Node.js 18+
- Access to a Geth JSON-RPC endpoint

## Using UV

UV is the Python package manager used by this project.

### Python version management

- `uv python list` — View available Python versions
- `uv python install python3.x` — Install a Python version
- `uv python uninstall python3.x` — Uninstall a Python version

### Project dependency management

- `uv add <package>` — Add a dependency (updates `pyproject.toml`)
- `uv remove <package>` — Remove a dependency
- `uv sync` — Install/sync all dependencies
- `uv lock` — Create/update the lockfile
- `uv run <command>` — Run a command in the project environment
- `uv tree` — View the dependency tree

## Setup

### Backend

```bash
cd backend
uv sync                    # Install Python dependencies
cp .env.example .env       # Create environment config
# Edit .env with your actual values
```

Environment variables (configured in `backend/.env`):

| Variable | Description |
|----------|-------------|
| `GETH_API` | URL of the Geth JSON-RPC endpoint |
| `POSTGRESQL_HOST` | PostgreSQL server host |
| `OPENAI_API_KEY` | Optional. Enables semantic CFG generation via an OpenAI-compatible API |
| `OPENAI_BASE_URL` | Optional. OpenAI-compatible base URL. If `/v1` is omitted, backend appends it automatically |
| `OPENAI_SEMANTIC_CFG_MODEL` | Optional. Model used for semantic CFG labeling. Default: `gpt-5-mini` |
| `OPENAI_SEMANTIC_CFG_TIMEOUT_SECONDS` | Optional. Semantic CFG request timeout in seconds. Default: `45` |
| `OPENAI_SEMANTIC_CFG_MIN_CONFIDENCE` | Optional. Reject semantic labels below this confidence. Default: `0.45` |
| `OPENAI_SEMANTIC_CFG_MAX_PROMPT_CHARS` | Optional. Max characters per semantic CFG batch before automatic split. Default: `120000` |
| `OPENAI_SEMANTIC_CFG_API_MODE` | Optional. `auto`, `responses`, or `chat_completions`. Use `chat_completions` for providers that do not implement `/v1/responses` |
| `OPENAI_SEMANTIC_CFG_BATCH_SIZE` | Optional. Fixed number of semantic regions per contract batch before request. Default: `20` |
| `OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE` | Optional. Number of fine-grained regions merged into one coarse semantic candidate before LLM labeling. Larger values usually mean fewer semantic nodes. Default: `18` |
| `OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT` | Optional. Target number of semantic nodes after the second compression pass. Lower values produce a more condensed semantic CFG. Default: `28` |
| `OPENAI_SEMANTIC_CFG_REASONING_EFFORT` | Optional. Thinking depth for compatible chat-completions providers. Recommended `minimal` for speed-sensitive semantic CFG generation |

### Frontend

```bash
cd frontend
npm install
```

## Running

### Start the backend server

```bash
cd backend
uv run uvicorn server:app --port 8000
```

The API server starts at `http://localhost:8000`.

### Start the frontend dev server

```bash
cd frontend
npm run dev
```

The frontend starts at `http://localhost:5173`.

### Standalone analysis (no server)

```bash
cd backend
uv run python main.py
```

Runs the analysis pipeline on the hardcoded transaction hash in `main.py` and saves results to `backend/Result/`.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/analyze` | Analyze a transaction by hash |
| GET | `/api/files/{tx_hash}/{filename}` | Retrieve analysis output files (.dot, .json, .svg) |
| POST | `/api/block` | Get block gas data for heatmap |
| GET | `/api/blocks` | Get block-level gas summary (paginated) |
| GET | `/api/transaction/{tx_hash}/block` | Get block number for a transaction |

## Key Backend Modules

| Module | Description |
|--------|-------------|
| `main.py` | Main analysis pipeline entry point |
| `main_api.py` | CLI wrapper accepting tx_hash as argument |
| `server.py` | FastAPI server wrapping the pipeline |
| `utils/evm_information.py` | EVM trace fetching, contract bytecode retrieval |
| `utils/basic_block.py` | Bytecode to basic block conversion |
| `utils/cfg_transaction.py` | Transaction-level CFG construction |
| `utils/semantic_cfg.py` | Rule-constrained semantic CFG grouping and LLM labeling |
| `utils/extract_token_changes.py` | Token transfer pairing and asset flow |
| `utils/render_cfg.py` | CFG DOT file rendering |
| `utils/render_legend.py` | Legend generation (matplotlib) |
| `utils/block_exploration.py` | Block gas data fetching |
