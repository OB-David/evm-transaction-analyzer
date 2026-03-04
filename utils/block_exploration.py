"""Block gas data extraction and processing for heatmap visualization."""
import os
import numpy as np
from web3 import Web3
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Web3 connection
PROVIDER_URL = os.environ.get("GETH_API")
if not PROVIDER_URL:
    raise ValueError("GETH_API environment variable not set")

w3 = Web3(Web3.HTTPProvider(PROVIDER_URL))


def fetch_block_gas_data(block_number: int) -> Dict:
    """
    Fetch block data and extract transaction gas information.

    Args:
        block_number: The block number to fetch

    Returns:
        Dictionary containing block info and transaction gas data
    """
    try:
        # Fetch block with full transaction details
        block = w3.eth.get_block(block_number, full_transactions=True)
    except Exception as e:
        return {
            "status": "error",
            "block_number": block_number,
            "miner": "",
            "transaction_count": 0,
            "transactions": [],
            "error": f"Failed to fetch block: {str(e)}"
        }

    miner = block.get('miner', '0x000...')
    txs = block.get('transactions', [])

    if not txs:
        return {
            "status": "success",
            "block_number": block_number,
            "miner": str(miner),
            "transaction_count": 0,
            "transactions": [],
            "error": None
        }

    # Process transactions
    transactions = []
    for i, tx in enumerate(txs):
        # Extract gas value
        gas_val = tx.get('gas', 0)
        log_gas = float(np.log10(gas_val)) if gas_val > 0 else 0.0

        # Extract gas price (prefer effectiveGasPrice for EIP-1559)
        gp_wei = tx.get('effectiveGasPrice') or tx.get('gasPrice') or 0
        gp_gwei = float(w3.from_wei(gp_wei, 'gwei'))

        # Extract transaction hash
        tx_hash = tx.get('hash')
        if hasattr(tx_hash, 'hex'):
            tx_hash = f"0x{tx_hash.hex()}"
        else:
            tx_hash = str(tx_hash)

        # Extract addresses
        from_addr = str(tx.get('from', 'Unknown'))
        to_addr_raw = tx.get('to')
        to_addr = str(to_addr_raw) if to_addr_raw else None

        # Calculate grid position (10 columns)
        x = i % 10
        y = i // 10

        transactions.append({
            "index": i,
            "hash": tx_hash,
            "gas": gas_val,
            "log_gas": log_gas,
            "gas_price_gwei": gp_gwei,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "x": x,
            "y": y
        })

    return {
        "status": "success",
        "block_number": block_number,
        "miner": str(miner),
        "transaction_count": len(transactions),
        "transactions": transactions,
        "error": None
    }


def get_transaction_block_number(tx_hash: str) -> Optional[int]:
    """
    Get the block number for a given transaction hash.

    Args:
        tx_hash: The transaction hash

    Returns:
        Block number or None if not found
    """
    try:
        tx = w3.eth.get_transaction(tx_hash)
        return tx.get('blockNumber')
    except Exception:
        return None
