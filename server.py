"""FastAPI server wrapping the EVM transaction analysis pipeline."""
import asyncio
import os
import re
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

app = FastAPI(title="EVM Transaction Analyzer")

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
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "python", "main_api.py", req.tx_hash,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return AnalyzeResponse(
            status="error",
            result_dir="",
            files=[],
            error=stderr.decode().strip() or stdout.decode().strip(),
        )

    # Parse result directory from stdout
    output = stdout.decode()
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
