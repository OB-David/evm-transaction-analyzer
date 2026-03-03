"""FastAPI server wrapping the EVM transaction analysis pipeline."""
import asyncio
import mimetypes
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, field_validator

app = FastAPI(title="EVM Transaction Analyzer")

# Thread pool for running subprocesses on Windows
executor = ThreadPoolExecutor(max_workers=4)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


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
    result_dir: str
    files: list[str]
    error: str | None = None


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    def run_analysis():
        """Run analysis in a thread to avoid Windows asyncio subprocess issues."""
        proc = subprocess.run(
            ["uv", "run", "python", "main_api.py", req.tx_hash],
            capture_output=True,
            text=True,
        )
        return proc

    # Run subprocess in thread pool
    loop = asyncio.get_event_loop()
    proc = await loop.run_in_executor(executor, run_analysis)

    if proc.returncode != 0:
        return AnalyzeResponse(
            status="error",
            result_dir="",
            files=[],
            error=proc.stderr.strip() or proc.stdout.strip(),
        )

    # Parse result directory from stdout
    output = proc.stdout
    result_dir = ""
    for line in output.splitlines():
        if line.startswith("RESULT_DIR="):
            result_dir = line.split("=", 1)[1]
            break

    if not result_dir or not os.path.isdir(result_dir):
        return AnalyzeResponse(
            status="error",
            result_dir="",
            files=[],
            error="Pipeline completed but result directory not found.",
        )

    files = os.listdir(result_dir)
    return AnalyzeResponse(
        status="success",
        result_dir=result_dir,
        files=files,
    )


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
