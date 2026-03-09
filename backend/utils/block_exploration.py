"""Block gas data extraction and processing for heatmap visualization."""
import os
import time
import threading
import numpy as np
from web3 import Web3
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Web3 connection
# Add no-cache headers to avoid HTTP-level caching by proxies and public RPC gateways.
PROVIDER_URL = os.environ.get("GETH_API")
if not PROVIDER_URL:
    raise ValueError("GETH_API environment variable not set")

w3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))

# Block-level cache for heatmap (keyed by block number string)
BLOCK_CACHE: Dict[str, Dict] = {}
# Cache includes on-chain block timestamp so we can display freshness without an extra RPC call.
_latest_block_cache = {"num": 0, "ts": 0.0, "block_ts": 0}
PREFETCH_COUNT = 300


def get_latest_block_info() -> Tuple[int, int]:
    """Return (block_number, block_timestamp) for the chain tip.

    Calls eth_getBlockByNumber('latest') rather than eth_blockNumber because many
    public RPC providers aggressively cache eth_blockNumber (sometimes for minutes),
    while eth_getBlockByNumber('latest') is typically not cached.
    Result is cached locally for 3 seconds.
    """
    now = time.time()
    if now - _latest_block_cache["ts"] < 3 and _latest_block_cache["num"] > 0:
        return _latest_block_cache["num"], _latest_block_cache["block_ts"]
    block = w3.eth.get_block('latest', full_transactions=False)
    num = block['number']
    block_ts = block.get('timestamp', 0)
    _latest_block_cache["num"] = num
    _latest_block_cache["block_ts"] = block_ts
    _latest_block_cache["ts"] = now
    return num, block_ts


def get_latest_block_number() -> int:
    num, _ = get_latest_block_info()
    return num


def _prefetch_worker(start_num: int):
    """Background prefetch: cache PREFETCH_COUNT blocks from start_num downward."""
    print(f"[*] Background prefetch started from block {start_num}, target {PREFETCH_COUNT} blocks.")
    cached = 0
    for i in range(PREFETCH_COUNT):
        target_num = start_num - i
        if target_num < 0:
            break
        cache_key = str(target_num)
        if cache_key in BLOCK_CACHE:
            continue
        try:
            block = w3.eth.get_block(target_num, full_transactions=False)
            tx_count = len(block.get('transactions', []))
            gas_used = block.get('gasUsed', 0)
            avg_gas = gas_used / tx_count if tx_count > 0 else 0
            base_fee = block.get('baseFeePerGas', 0) / 1e9
            BLOCK_CACHE[cache_key] = {
                "block_number": target_num,
                "avg_gas": avg_gas,
                "base_fee": base_fee,
                "tx_count": tx_count,
            }
            cached += 1
            if cached % 50 == 0:
                print(f"  [+] Prefetched {cached} blocks...")
        except Exception:
            continue
    print(f"[*] Background prefetch completed. Cached {cached} new blocks.")


def _new_block_watcher():
    """Background thread: polls eth_getBlockByNumber('latest') every 3 seconds.

    Uses the same RPC method as get_latest_block_info() to avoid eth_blockNumber
    provider-level caching. Caches each new block into BLOCK_CACHE immediately so
    frontend auto-refresh responses are served instantly.
    """
    print("[*] New block watcher started.")
    last_seen = _latest_block_cache["num"]
    while True:
        time.sleep(3)
        try:
            block = w3.eth.get_block('latest', full_transactions=False)
            current = block['number']
            block_ts = block.get('timestamp', 0)
            if current > last_seen:
                for num in range(last_seen + 1, current + 1):
                    cache_key = str(num)
                    if cache_key not in BLOCK_CACHE:
                        b = w3.eth.get_block(num, full_transactions=False)
                        tx_count = len(b.get('transactions', []))
                        gas_used = b.get('gasUsed', 0)
                        avg_gas = gas_used / tx_count if tx_count > 0 else 0
                        base_fee = b.get('baseFeePerGas', 0) / 1e9
                        BLOCK_CACHE[cache_key] = {
                            "block_number": num,
                            "avg_gas": avg_gas,
                            "base_fee": base_fee,
                            "tx_count": tx_count,
                        }
                last_seen = current
                _latest_block_cache["num"] = current
                _latest_block_cache["block_ts"] = block_ts
                _latest_block_cache["ts"] = time.time()
        except Exception:
            pass


def start_prefetch():
    """Start background prefetch thread from the latest block, then watch for new blocks."""
    try:
        block = w3.eth.get_block('latest', full_transactions=False)
        initial_height = block['number']
        _latest_block_cache["num"] = initial_height
        _latest_block_cache["block_ts"] = block.get('timestamp', 0)
        _latest_block_cache["ts"] = time.time()
        threading.Thread(target=_prefetch_worker, args=(initial_height,), daemon=True).start()
        threading.Thread(target=_new_block_watcher, daemon=True).start()
    except Exception as e:
        print(f"Failed to start prefetch thread: {e}")


def fetch_blocks_gas_summary(offset: int = 0, count: int = 160) -> Dict:
    """
    Fetch summary gas data for multiple blocks (for block-level heatmap).

    Args:
        offset: Number of blocks before latest to start from
        count: Number of blocks to fetch

    Returns:
        Dictionary with block summary data including avg_gas per block
    """
    try:
        latest_num, latest_block_ts = get_latest_block_info()
        start_num = latest_num - offset  # newest block on this page

        # When offset==0 we already have the latest block timestamp from get_latest_block_info(),
        # so skip the redundant RPC call.
        if offset == 0:
            page_timestamp = latest_block_ts
        else:
            try:
                top_block = w3.eth.get_block(start_num, full_transactions=False)
                page_timestamp = top_block.get('timestamp', 0)
            except Exception:
                page_timestamp = 0

        # Build ascending list of target block numbers
        target_nums = [start_num - count + 1 + i for i in range(count)]

        blocks = []
        for i, num in enumerate(target_nums):
            cache_key = str(num)
            if cache_key in BLOCK_CACHE:
                entry = BLOCK_CACHE[cache_key].copy()
                entry["x"] = i % 10
                entry["y"] = i // 10
                blocks.append(entry)
                continue

            try:
                block = w3.eth.get_block(num, full_transactions=False)
                tx_count = len(block.get('transactions', []))
                gas_used = block.get('gasUsed', 0)
                avg_gas = gas_used / tx_count if tx_count > 0 else 0
                base_fee = block.get('baseFeePerGas', 0) / 1e9

                entry = {
                    "block_number": num,
                    "avg_gas": avg_gas,
                    "base_fee": base_fee,
                    "tx_count": tx_count,
                }
                BLOCK_CACHE[cache_key] = entry

                grid_entry = entry.copy()
                grid_entry["x"] = i % 10
                grid_entry["y"] = i // 10
                blocks.append(grid_entry)
            except Exception:
                # On error, add a placeholder
                blocks.append({
                    "block_number": num,
                    "avg_gas": 0,
                    "base_fee": 0,
                    "tx_count": 0,
                    "x": i % 10,
                    "y": i // 10,
                })

        return {
            "status": "success",
            "latest_block": latest_num,
            "latest_block_timestamp": latest_block_ts,
            "page_timestamp": page_timestamp,
            "blocks": blocks,
            "error": None,
        }
    except Exception as e:
        return {
            "status": "error",
            "latest_block": 0,
            "latest_block_timestamp": 0,
            "page_timestamp": 0,
            "blocks": [],
            "error": f"Failed to fetch blocks: {str(e)}",
        }


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
