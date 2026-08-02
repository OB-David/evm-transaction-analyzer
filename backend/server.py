"""FastAPI server wrapping the EVM transaction analysis pipeline."""
import asyncio
import mimetypes
import json, os
import re
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator
from typing import Any, Literal
from utils.block_exploration import fetch_block_gas_data, get_transaction_block_number, fetch_blocks_gas_summary, start_prefetch
from utils.arbitrage_crawler import fetch_arbitrage_hashes, get_cached_hashes
from utils.plain_cfg_llm import (
    PlainCfgLlmServiceError,
    analyze_plain_cfg_block,
    clear_plain_cfg_runtime_cache,
)

load_dotenv()

app = FastAPI(title="EVM Transaction Analyzer")


@app.on_event("startup")
def startup_prefetch():
    """Start background block prefetch and arbitrage crawl on server startup."""
    start_prefetch()
    threading.Thread(target=fetch_arbitrage_hashes, daemon=True).start()

# Thread pool for running subprocesses on Windows
executor = ThreadPoolExecutor(max_workers=4)
analysis_jobs: dict[str, Future[subprocess.CompletedProcess[str]]] = {}
analysis_jobs_lock = threading.Lock()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def extract_analysis_error(stdout: str, stderr: str) -> str | None:
    """Extract a user-facing error line from subprocess output."""
    lines: list[str] = []
    for text in (stderr, stdout):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

    if not lines:
        return None

    # Prefer explicit "not supported" reasons.
    for line in reversed(lines):
        if "not supported" in line.lower():
            return line

    # Fallback to the last meaningful output line.
    for line in reversed(lines):
        if not line.startswith("RESULT_DIR="):
            return line

    return None


class AnalyzeRequest(BaseModel):
    tx_hash: str

    @field_validator("tx_hash")
    @classmethod
    def validate_tx_hash(cls, v: str) -> str:
        if not TX_HASH_RE.match(v):
            raise ValueError("tx_hash must be 0x followed by 64 hex characters")
        return v


class AnalyzeResponse(BaseModel):
    status: str
    stage: str = "analyzing"
    result_dir: str
    files: list[str]
    error: str | None = None
    updated_at: str | None = None


class BlockRequest(BaseModel):
    block_number: int


class TransactionGasInfo(BaseModel):
    index: int
    hash: str
    gas: int
    log_gas: float
    gas_price_gwei: float
    from_addr: str
    to_addr: str | None
    x: int
    y: int


class BlockGasResponse(BaseModel):
    status: str
    block_number: int
    miner: str
    transaction_count: int
    transactions: list[TransactionGasInfo]
    error: str | None = None


class BlockNumberResponse(BaseModel):
    block_number: int


class BlockSummaryInfo(BaseModel):
    block_number: int
    avg_gas: float
    base_fee: float
    tx_count: int
    x: int
    y: int


class BlocksHeatmapResponse(BaseModel):
    status: str
    latest_block: int
    latest_block_timestamp: int = 0
    page_timestamp: int
    blocks: list[BlockSummaryInfo]
    error: str | None = None


class PlainBlockAnalysisRequest(BaseModel):
    tx_hash: str
    block_id: int | str
    force_refresh: bool = False

    @field_validator("tx_hash")
    @classmethod
    def validate_tx_hash(cls, v: str) -> str:
        if not TX_HASH_RE.match(v):
            raise ValueError("tx_hash must be 0x followed by 64 hex characters")
        return v

    @field_validator("block_id")
    @classmethod
    def validate_block_id(cls, v: int | str) -> int | str:
        if isinstance(v, str) and not v.strip():
            raise ValueError("block_id cannot be empty")
        return v


class LlmAnalysisResult(BaseModel):
    title: str
    description: str


class PlainBlockAnalysisResponse(BaseModel):
    status: Literal["success"]
    source: Literal["cache", "llm"]
    analysis: LlmAnalysisResult
    context_meta: dict[str, Any]


def _analysis_result_dir(tx_hash: str) -> str:
    return os.path.join("Result", tx_hash.lstrip("0x"))


def _analysis_status_path(tx_hash: str) -> str:
    return os.path.join(_analysis_result_dir(tx_hash), "analysis_status.json")


def _write_server_analysis_status(tx_hash: str, stage: str, error: str | None = None) -> None:
    result_dir = _analysis_result_dir(tx_hash)
    os.makedirs(result_dir, exist_ok=True)
    payload = {
        "status": "error" if error else "processing",
        "stage": stage,
        "result_dir": os.path.abspath(result_dir),
        "files": sorted(os.listdir(result_dir)),
        "error": error,
    }
    status_path = _analysis_status_path(tx_hash)
    temp_path = f"{status_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, status_path)


def _read_analysis_status(tx_hash: str) -> AnalyzeResponse | None:
    status_path = _analysis_status_path(tx_hash)
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return AnalyzeResponse(**payload)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _run_analysis_subprocess(tx_hash: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["uv", "run", "python", "main_api.py", tx_hash],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        current = _read_analysis_status(tx_hash)
        if current is None or current.status != "error":
            _write_server_analysis_status(
                tx_hash,
                "error",
                extract_analysis_error(proc.stdout, proc.stderr) or "Analysis failed.",
            )
    return proc


def _cleanup_analysis_job(tx_hash: str, completed: Future[subprocess.CompletedProcess[str]]) -> None:
    with analysis_jobs_lock:
        if analysis_jobs.get(tx_hash) is completed:
            analysis_jobs.pop(tx_hash, None)


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    clear_plain_cfg_runtime_cache(req.tx_hash)

    with analysis_jobs_lock:
        existing_job = analysis_jobs.get(req.tx_hash)
        current = _read_analysis_status(req.tx_hash)
        if existing_job is not None and not existing_job.done():
            return current or AnalyzeResponse(status="processing", stage="queued", result_dir="", files=[])
        if current is not None and current.status == "success":
            return current

        _write_server_analysis_status(req.tx_hash, "queued")
        submitted_job = executor.submit(_run_analysis_subprocess, req.tx_hash)
        analysis_jobs[req.tx_hash] = submitted_job

    submitted_job.add_done_callback(
        lambda completed, tx_hash=req.tx_hash: _cleanup_analysis_job(tx_hash, completed)
    )

    return _read_analysis_status(req.tx_hash) or AnalyzeResponse(
        status="processing", stage="queued", result_dir="", files=[]
    )


@app.get("/api/analyze/{tx_hash}/status", response_model=AnalyzeResponse)
async def analyze_status(tx_hash: str):
    if not TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")
    current = _read_analysis_status(tx_hash)
    if current is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return current


@app.get("/api/files/{tx_hash}/{filename}")
async def get_file(tx_hash: str, filename: str):
    """Serve files from Result directory."""
    # Validate tx_hash format
    if not TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")

    # Prevent directory traversal attacks
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Whitelist allowed file extensions
    allowed_extensions = {".dot", ".json", ".svg"}
    if not any(filename.endswith(ext) for ext in allowed_extensions):
        raise HTTPException(status_code=400, detail="File type not allowed")

    # Build file path
    tx_dir_name = tx_hash.lstrip("0x")
    file_path = os.path.join("Result", tx_dir_name, filename)

    # Check file exists
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    # Determine content type and return file
    if filename.endswith(".dot"):
        return FileResponse(path=file_path, media_type="text/vnd.graphviz")
    elif filename.endswith(".json"):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return PlainTextResponse(content=content, media_type="application/json")
    else:
        content_type, _ = mimetypes.guess_type(filename)
        return FileResponse(path=file_path, media_type=content_type)


@app.post("/api/block", response_model=BlockGasResponse)
async def get_block_gas_data(req: BlockRequest):
    """Get block gas data for heatmap visualization."""
    result = fetch_block_gas_data(req.block_number)
    return BlockGasResponse(**result)


@app.get("/api/blocks", response_model=BlocksHeatmapResponse)
async def get_blocks_heatmap(offset: int = 0, count: int = 160):
    """Get block-level gas summary data for the blocks heatmap."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, fetch_blocks_gas_summary, offset, count)
    return BlocksHeatmapResponse(**result)


@app.get("/api/transaction/{tx_hash}/block", response_model=BlockNumberResponse)
async def get_transaction_block(tx_hash: str):
    """Get the block number for a given transaction hash."""
    # Validate tx_hash format
    if not TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")

    block_number = get_transaction_block_number(tx_hash)
    if block_number is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return BlockNumberResponse(block_number=block_number)

@app.get("/api/arbitrage-hashes")
async def get_arbitrage_hashes():
    """Return the cached list of arbitrage tx hashes fetched from Dune."""
    return get_cached_hashes()


@app.post("/api/arbitrage-hashes/refresh")
async def refresh_arbitrage_hashes():
    """Trigger a fresh Dune query execution in the background."""
    threading.Thread(target=fetch_arbitrage_hashes, daemon=True).start()
    return {"status": "refresh started"}


@app.get("/api/arbitrage/{tx_hash}")
async def get_arbitrage(tx_hash: str):
    path = os.path.join("Result", tx_hash.lstrip("0x"), "arbitrage.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/llm/plain-block-analysis", response_model=PlainBlockAnalysisResponse)
async def plain_block_analysis(req: PlainBlockAnalysisRequest):
    loop = asyncio.get_event_loop()

    try:
        result = await loop.run_in_executor(
            executor,
            lambda: analyze_plain_cfg_block(
                tx_hash=req.tx_hash,
                block_id=req.block_id,
                force_refresh=req.force_refresh,
            ),
        )
    except PlainCfgLlmServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return PlainBlockAnalysisResponse(**result)
