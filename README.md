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

### Frontend

```bash
cd frontend
npm install
```

## Running

### Start the backend server

```bash
cd backend
uv run uvicorn server:app --port 8001
```

The API server starts at `http://localhost:8001`.

### Start the frontend dev server

```bash
cd frontend
npm run dev
```

The frontend starts at `http://localhost:9006`.

The frontend uses same-origin `/api` requests. During development, Vite proxies
these requests to `http://127.0.0.1:8000`. Set `VITE_API_BASE` when the API is
hosted separately (for example, `VITE_API_BASE=https://api.example.com`).

### Standalone analysis (no server)

```bash
cd backend
uv run python main.py
```

Runs the analysis pipeline on the hardcoded transaction hash in `main.py` and saves results to `data_base/analysis/`.

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
| `utils/extract_token_changes.py` | Token transfer pairing and asset flow |
| `utils/render_cfg.py` | CFG DOT file rendering |
| `utils/render_legend.py` | Legend generation (matplotlib) |
| `utils/block_exploration.py` | Block gas data fetching |
