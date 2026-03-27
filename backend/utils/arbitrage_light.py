"""Lightweight arbitrage detection for a single transaction."""

from __future__ import annotations

import os
from typing import Any

from web3 import Web3

from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.evm_information import TraceFormatter
from utils.extract_token_changes import detect_arbitrage, pair_transactions


def _failed_result(tx_hash: str, reason: str, error: str) -> dict[str, Any]:
    return {
        "tx_hash": tx_hash.lower(),
        "is_arbitrage": False,
        "status": "failed",
        "reason": reason,
        "error": error,
    }


def _skipped_result(tx_hash: str, reason: str) -> dict[str, Any]:
    return {
        "tx_hash": tx_hash.lower(),
        "is_arbitrage": False,
        "status": "skipped",
        "reason": reason,
        "error": None,
    }


def analyze_tx_arbitrage_light(tx_hash: str, provider_url: str | None = None) -> dict[str, Any]:
    """Analyze arbitrage for one tx without rendering/output file generation."""
    normalized_hash = tx_hash.lower()
    provider = provider_url or os.getenv("GETH_API", "")
    if not provider:
        return _failed_result(normalized_hash, "missing_geth_api", "GETH_API is not set")

    try:
        web3 = Web3(Web3.HTTPProvider(provider))
        if not web3.is_connected():
            return _failed_result(normalized_hash, "geth_unreachable", f"Cannot connect to {provider}")

        tx = web3.eth.get_transaction(normalized_hash)
        from_address = tx.get("from")
        to_address = tx.get("to")
        amount = int(tx.get("value", 0) or 0)

        if to_address is None or to_address == "":
            return _skipped_result(normalized_hash, "contract_creation_unsupported")

        contract_code = web3.eth.get_code(to_address)
        if len(contract_code) == 0:
            return _skipped_result(normalized_hash, "eth_transfer_unsupported")

        formatter = TraceFormatter(provider)
        processor = BasicBlockProcessor()

        standardized_trace = formatter.get_standardized_trace(normalized_hash)
        contracts_addresses = standardized_trace.get("contracts_addresses", [])
        slot_map = standardized_trace.get("slot_map", {})
        erc20_token_map = standardized_trace.get("erc20_token_map", {})

        contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)
        all_blocks = processor.process_multiple_contracts(contracts_bytecode)
        token_decimals_map = {addr: formatter.get_token_decimals(addr) for addr in erc20_token_map.keys()}

        cfg_constructor = CFGConstructor(all_blocks, token_decimals_map)
        _, _, _, all_changes, _, _ = cfg_constructor.construct_cfg(
            standardized_trace, slot_map, erc20_token_map
        )

        original_transfer = [
            str(from_address).lower() if from_address else "",
            str(to_address).lower(),
            amount,
        ]
        pairs, _, pending_erc20 = pair_transactions(original_transfer, all_changes, token_decimals_map)
        arb_result = detect_arbitrage(pairs, pending_erc20)
        cycles = arb_result.get("cycles", [])
        is_arbitrage = len(cycles) > 0

        return {
            "tx_hash": normalized_hash,
            "is_arbitrage": is_arbitrage,
            "status": "analyzed",
            "reason": "arbitrage_detected" if is_arbitrage else "no_arbitrage",
            "error": None,
            "cycles": cycles,
            "arb_edge_orders": sorted(list(arb_result.get("arb_edge_orders", set()))),
        }
    except Exception as exc:
        return _failed_result(normalized_hash, "analysis_error", str(exc))
