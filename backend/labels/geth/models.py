"""Data models shared by the Geth trace reader and label detector."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Trace:
    block_number: int
    transaction_hash: str
    transaction_position: int
    trace_address: list[int]
    trace_type: str
    call_type: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    gas: int | None = None
    gas_used: int | None = None
    value: int | None = None
    input: str = "0x"
    error: str | None = None
    classification: str = "unknown"
    protocol: str | None = None
    abi_name: str | None = None
    function_signature: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transfer:
    block_number: int
    transaction_hash: str
    trace_address: list[int]
    amount: int
    to_address: str
    from_address: str
    token_address: str


@dataclass(frozen=True)
class Swap:
    abi_name: str
    transaction_hash: str
    transaction_position: int
    block_number: int
    trace_address: list[int]
    contract_address: str
    from_address: str
    to_address: str
    token_in_address: str
    token_in_amount: int
    token_out_address: str
    token_out_amount: int
    protocol: str
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Arbitrage:
    swaps: list[Swap]
    block_number: int
    transaction_hash: str
    account_address: str
    profit_token_address: str
    start_amount: int
    end_amount: int
    profit_amount: int
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
