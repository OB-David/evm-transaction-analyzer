"""FastAPI server wrapping the EVM transaction analysis pipeline."""
import asyncio
import mimetypes
import json, os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator
from typing import Any, Literal
from utils.block_exploration import (
    fetch_block_gas_data,
    get_latest_block_number,
    get_transaction_block_number,
    fetch_blocks_gas_summary,
    start_prefetch,
)
from utils.analysis_paths import ANALYSIS_ROOT, analysis_directory
from labels.dune.sync import DUNE_QUERY_ID
from labels.geth.sync import start_background_sync
from labels.coordinator import (
    HISTORY_START_BLOCK,
    LabelCoordinator,
    MAX_API_BLOCK_RANGE,
)
from utils.plain_cfg_llm import (
    PlainCfgLlmServiceError,
    analyze_plain_cfg_block,
    clear_plain_cfg_runtime_cache,
)

load_dotenv()

app = FastAPI(title="EVM Transaction Analyzer")
label_coordinator = LabelCoordinator()


@app.on_event("startup")
def startup_prefetch():
    """Start block prefetch and the isolated local-index updater."""
    start_prefetch()
    label_coordinator.initialize()
    start_background_sync(get_latest_block_number, store=label_coordinator.geth)

# Thread pool for waiting on analysis subprocesses and serving blocking helpers.
executor = ThreadPoolExecutor(max_workers=4)


@dataclass
class AnalysisJob:
    tx_hash: str
    process: subprocess.Popen[str]
    future: Future[subprocess.CompletedProcess[str]] | None = None
    cancel_requested: bool = False
    stage: str = "queued"
    error: str | None = None


analysis_jobs: dict[str, AnalysisJob] = {}
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
        return v.lower()


class AnalyzeResponse(BaseModel):
    status: str
    stage: str = "analyzing"
    result_dir: str
    files: list[str]
    error: str | None = None


class CancelAnalysisResponse(BaseModel):
    status: Literal["cancelled", "not_running"]
    tx_hash: str
    cleaned: bool


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


class ArbitrageTransactionInfo(BaseModel):
    tx_hash: str
    block_number: int


class ArbitrageTransactionsResponse(BaseModel):
    transactions: list[ArbitrageTransactionInfo]
    history_start_block: int
    max_arbitrage_block: int | None
    initial_sync_complete: bool
    coverage_complete: bool


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
    description: str = ""


class PlainBlockAnalysisResponse(BaseModel):
    status: Literal["success"]
    source: Literal["cache", "llm"]
    analysis: LlmAnalysisResult
    context_meta: dict[str, Any]


def _analysis_result_dir(tx_hash: str) -> str:
    return str(analysis_directory(tx_hash))


def _cleanup_incomplete_analysis(tx_hash: str) -> bool:
    """Remove only the validated result directory belonging to one transaction."""
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise ValueError("Invalid transaction hash")

    result_root = os.path.realpath(ANALYSIS_ROOT)
    result_dir = os.path.realpath(_analysis_result_dir(tx_hash))
    expected_parent = result_root + os.sep
    if not result_dir.startswith(expected_parent) or os.path.dirname(result_dir) != result_root:
        raise RuntimeError("Refusing to clean an unexpected result path")
    if not os.path.exists(result_dir):
        return False
    if os.path.islink(result_dir) or not os.path.isdir(result_dir):
        raise RuntimeError("Refusing to clean a non-directory analysis result")

    shutil.rmtree(result_dir)
    return True


def _completed_analysis_response(tx_hash: str) -> AnalyzeResponse | None:
    result_dir = _analysis_result_dir(tx_hash)
    required_files = {
        "analysis_timing.json",
        "arbitrage.json",
        "call_tree.json",
        "edge_id-step.json",
        "folded_blocks_information.json",
        "folded_cfg.dot",
        "link.json",
        "plain_blocks_information.json",
        "plain_cfg.dot",
        "plain_semantics.json.gz",
        "swap_in_fcfg.json",
        "swap_in_pcfg.json",
    }
    if not os.path.isdir(result_dir):
        return None
    files = sorted(
        name for name in os.listdir(result_dir)
        if os.path.isfile(os.path.join(result_dir, name))
    )
    if not required_files.issubset(files):
        return None
    try:
        with open(os.path.join(result_dir, "link.json"), "r", encoding="utf-8") as handle:
            link_artifact = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    edge_links = link_artifact.get("edge_links", {})
    if (
        link_artifact.get("schema_version") != 1
        or not isinstance(edge_links, dict)
        or not all(isinstance(edge_links.get(name), list) for name in ("folded", "plain", "call_tree"))
    ):
        # Older cached analyses must be regenerated so a TFG selection can
        # drive both CFG modes and the contract call tree.
        return None
    return AnalyzeResponse(
        status="success",
        stage="complete",
        result_dir=os.path.abspath(result_dir),
        files=files,
    )


def _job_response(job: AnalysisJob) -> AnalyzeResponse:
    result_dir = _analysis_result_dir(job.tx_hash)
    files = sorted(
        name for name in os.listdir(result_dir)
        if os.path.isfile(os.path.join(result_dir, name))
    ) if os.path.isdir(result_dir) else []
    if job.future is None or not job.future.done():
        return AnalyzeResponse(
            status="processing",
            stage=job.stage,
            result_dir=os.path.abspath(result_dir),
            files=files,
        )
    if job.cancel_requested:
        return AnalyzeResponse(
            status="error",
            stage="error",
            result_dir=os.path.abspath(result_dir),
            files=[],
            error="Analysis cancelled",
        )
    if job.error:
        return AnalyzeResponse(
            status="error",
            stage="error",
            result_dir=os.path.abspath(result_dir),
            files=[],
            error=job.error,
        )
    return _completed_analysis_response(job.tx_hash) or AnalyzeResponse(
        status="error",
        stage="error",
        result_dir=os.path.abspath(result_dir),
        files=files,
        error="Analysis completed without required artifacts",
    )


def _wait_for_analysis(job: AnalysisJob) -> subprocess.CompletedProcess[str]:
    output_lines: list[str] = []
    if job.process.stdout is not None:
        for line in job.process.stdout:
            output_lines.append(line)
            stripped = line.strip()
            if stripped.startswith("ANALYSIS_STAGE="):
                stage = stripped.removeprefix("ANALYSIS_STAGE=")
                with analysis_jobs_lock:
                    if analysis_jobs.get(job.tx_hash) is job:
                        job.stage = stage
    job.process.wait()
    stdout = "".join(output_lines)
    completed = subprocess.CompletedProcess(
        job.process.args,
        job.process.returncode,
        stdout,
        "",
    )

    if job.cancel_requested:
        _cleanup_incomplete_analysis(job.tx_hash)
    elif completed.returncode != 0:
        job.error = extract_analysis_error(completed.stdout, completed.stderr) or "Analysis failed."
        job.stage = "error"
        _cleanup_incomplete_analysis(job.tx_hash)
    else:
        job.stage = "complete"
    return completed


def _cleanup_analysis_job(tx_hash: str, job: AnalysisJob) -> None:
    with analysis_jobs_lock:
        if analysis_jobs.get(tx_hash) is job:
            analysis_jobs.pop(tx_hash, None)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=5)


def _cancel_analysis_job(tx_hash: str) -> CancelAnalysisResponse:
    with analysis_jobs_lock:
        job = analysis_jobs.get(tx_hash)
        if job is None:
            return CancelAnalysisResponse(status="not_running", tx_hash=tx_hash, cleaned=False)
        if job.process.poll() is not None:
            analysis_jobs.pop(tx_hash, None)
            return CancelAnalysisResponse(status="not_running", tx_hash=tx_hash, cleaned=False)
        job.cancel_requested = True

    result_existed = os.path.isdir(_analysis_result_dir(tx_hash))
    _terminate_process_tree(job.process)
    if job.future is not None:
        try:
            job.future.result(timeout=10)
        except Exception:
            # Cleanup below is still safe because the process tree has exited.
            pass

    _cleanup_incomplete_analysis(tx_hash)
    cleaned = result_existed and not os.path.exists(_analysis_result_dir(tx_hash))
    _cleanup_analysis_job(tx_hash, job)
    return CancelAnalysisResponse(status="cancelled", tx_hash=tx_hash, cleaned=cleaned)


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    clear_plain_cfg_runtime_cache(req.tx_hash)

    with analysis_jobs_lock:
        job = analysis_jobs.get(req.tx_hash)
        if job is not None:
            response = _job_response(job)
            if response.status != "processing":
                analysis_jobs.pop(req.tx_hash, None)
            return response
        else:
            completed_response = _completed_analysis_response(req.tx_hash)
            if completed_response is not None:
                return completed_response

            # A failed server or cancelled client may have left an incomplete directory.
            _cleanup_incomplete_analysis(req.tx_hash)
            popen_kwargs: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    ["uv", "run", "python", "main_api.py", req.tx_hash],
                    **popen_kwargs,
                )
            except Exception as exc:
                _cleanup_incomplete_analysis(req.tx_hash)
                raise HTTPException(status_code=500, detail=f"Failed to start analysis: {exc}") from exc
            job = AnalysisJob(tx_hash=req.tx_hash, process=process)
            analysis_jobs[req.tx_hash] = job
            job.future = executor.submit(_wait_for_analysis, job)

    return _job_response(job)


@app.delete("/api/analyze/{tx_hash}", response_model=CancelAnalysisResponse)
async def cancel_analysis(tx_hash: str):
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")
    tx_hash = tx_hash.lower()
    return await asyncio.to_thread(_cancel_analysis_job, tx_hash)


@app.get("/api/analyze/{tx_hash}/progress", response_model=AnalyzeResponse)
async def analyze_progress(tx_hash: str):
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")
    tx_hash = tx_hash.lower()
    with analysis_jobs_lock:
        job = analysis_jobs.get(tx_hash)
        if job is not None:
            response = _job_response(job)
            if response.status != "processing":
                analysis_jobs.pop(tx_hash, None)
            return response
    completed = _completed_analysis_response(tx_hash)
    if completed is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return completed


@app.get("/api/files/{tx_hash}/{filename}")
async def get_file(tx_hash: str, filename: str):
    """Serve files from the transaction analysis directory."""
    # Validate tx_hash format
    if not TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")

    # Prevent directory traversal attacks
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Whitelist allowed file extensions
    allowed_extensions = {".dot", ".json"}
    if not any(filename.endswith(ext) for ext in allowed_extensions):
        raise HTTPException(status_code=400, detail="File type not allowed")

    # Build file path
    file_path = os.path.join(_analysis_result_dir(tx_hash), filename)

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


@app.get("/api/cfg/{tx_hash}/{mode}.svg")
async def render_cfg_svg(tx_hash: str, mode: str):
    """Render a persisted CFG DOT source without writing an SVG artifact."""
    if not TX_HASH_RE.match(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")
    if mode not in {"folded", "plain"}:
        raise HTTPException(status_code=400, detail="Invalid CFG mode")

    dot_path = os.path.join(_analysis_result_dir(tx_hash), f"{mode}_cfg.dot")
    if not os.path.isfile(dot_path):
        raise HTTPException(status_code=404, detail="CFG DOT source not found")

    def run_graphviz() -> bytes:
        completed = subprocess.run(
            ["dot", "-Tsvg", dot_path],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return completed.stdout

    try:
        svg = await asyncio.to_thread(run_graphviz)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="CFG rendering timed out") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(status_code=500, detail="CFG rendering failed") from exc

    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


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
    """Compatibility endpoint: return the newest local SQLite records."""
    return {
        "transactions": label_coordinator.recent_transactions(500),
        "fetched_at": None,
        "source": "local_sqlite",
        "query_id": DUNE_QUERY_ID,
    }


@app.post("/api/arbitrage-hashes/refresh")
async def refresh_arbitrage_hashes():
    """Compatibility endpoint; synchronization is managed in the background."""
    return {"status": "managed by Geth local-index sync"}


@app.get("/api/arbitrage-transactions", response_model=ArbitrageTransactionsResponse)
async def get_arbitrage_transactions(from_block: int, to_block: int):
    """Read local arbitrage markers for one bounded block-explorer page."""
    if from_block < 0 or to_block < 0:
        raise HTTPException(status_code=400, detail="Block numbers must be non-negative")
    if from_block > to_block:
        raise HTTPException(status_code=400, detail="from_block must not exceed to_block")
    if to_block - from_block + 1 > MAX_API_BLOCK_RANGE:
        raise HTTPException(
            status_code=400,
            detail=f"Block range may contain at most {MAX_API_BLOCK_RANGE} blocks",
        )
    return ArbitrageTransactionsResponse(
        transactions=label_coordinator.query_transactions(from_block, to_block),
        history_start_block=HISTORY_START_BLOCK,
        max_arbitrage_block=label_coordinator.max_arbitrage_block(),
        initial_sync_complete=label_coordinator.dune.initial_sync_complete(),
        coverage_complete=label_coordinator.coverage_complete(from_block, to_block),
    )


@app.get("/api/arbitrage/{tx_hash}")
async def get_arbitrage(tx_hash: str):
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise HTTPException(status_code=400, detail="Invalid tx_hash format")
    path = os.path.join(_analysis_result_dir(tx_hash), "arbitrage.json")
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
