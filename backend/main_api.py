"""CLI wrapper that accepts tx_hash as argument and runs the analysis pipeline."""
import sys
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict
from web3 import Web3

# 处理编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.extract_token_changes import (
    pair_transactions,
    afg_to_fcfg,
    afg_to_pcfg,
    edge_link_to_json,
    detect_arbitrage,
    compute_address_balances,
)
from utils.indentify_swap import filter_to_file
from utils.sequence_diagram import build_refined_hierarchical_trace
from main import create_result_directory, save_graphs

load_dotenv()

ANALYSIS_STATUS_FILENAME = "analysis_status.json"


def _write_analysis_status(result_dir: str, stage: str, error: str | None = None) -> None:
    """Atomically publish pipeline progress for the API and frontend poller."""
    payload = {
        "status": "error" if error else ("success" if stage == "complete" else "processing"),
        "stage": stage,
        "result_dir": os.path.abspath(result_dir),
        "files": sorted(
            name for name in os.listdir(result_dir)
            if name != f".{ANALYSIS_STATUS_FILENAME}.tmp"
        ),
        "error": error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    status_path = os.path.join(result_dir, ANALYSIS_STATUS_FILENAME)
    temp_path = os.path.join(result_dir, f".{ANALYSIS_STATUS_FILENAME}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, status_path)

def _normalize_edge_step(value: Any) -> int:
    if isinstance(value, bool): return 0
    if isinstance(value, int): return value
    try:
        return int(str(value))
    except Exception:
        return 0

def _build_edge_step_information_compat(cfg: Any) -> Dict[str, Dict[str, Any]]:
    """为前端生成 edge_id 与 step 的映射关系"""
    edges = list(getattr(cfg, "edges", []))
    indexed_edges = list(enumerate(edges))
    sorted_edges = sorted(
        indexed_edges,
        key=lambda item: (_normalize_edge_step(getattr(item[1], "edge_step", 0)), item[0]),
    )

    edge_step_map: Dict[str, Dict[str, Any]] = {}
    for rank, (_original_index, edge) in enumerate(sorted_edges, start=1):
        edge_id = f"edge_{rank}"
        setattr(edge, "edge_id", edge_id)
        edge_step = _normalize_edge_step(getattr(edge, "edge_step", 0))

        source_id = getattr(getattr(edge, "source", None), "id", "unknown")
        target_id = getattr(getattr(edge, "target", None), "id", "unknown")

        edge_step_map[edge_id] = {
            "edge_id": edge_id,
            "edge_step": edge_step,
            "source_node": f"node_{source_id}",
            "target_node": f"node_{target_id}",
        }
    return edge_step_map

def _enrich_folded_blocks_information(cfg: Any, folded_blocks_map: Dict[str, Any]) -> Dict[str, Any]:
    """回填缺失的 PC 信息"""
    node_by_id = {str(getattr(node, "id", "")): node for node in getattr(cfg, "nodes", [])}
    for key, info in folded_blocks_map.items():
        if not isinstance(info, dict):
            continue
        block_id = info.get("block_id", key)
        node = node_by_id.get(str(block_id))
        if node is not None:
            if info.get("start_pc") in (None, ""):
                info["start_pc"] = str(getattr(node, "start_pc", "Unknown"))
            if info.get("end_pc") in (None, ""):
                fold_info = getattr(node, "fold_info", {})
                node_end_pc = fold_info.get("end_pc") if isinstance(fold_info, dict) else getattr(node, "end_pc", "Unknown")
                info["end_pc"] = str(node_end_pc)
    return folded_blocks_map

def run(tx_hash: str):
    PROVIDER_URL = os.environ.get("GETH_API")
    result_dir = create_result_directory(tx_hash)
    _write_analysis_status(result_dir, "analyzing")

    web3 = Web3(Web3.HTTPProvider(PROVIDER_URL))
    tx = web3.eth.get_transaction(tx_hash)
    from_address = tx.get('from')
    to_address = tx.get('to')
    amount = tx.get('value', 0)

    # 基础检查
    if to_address is None or to_address == "":
        raise ValueError("This contract creation transaction is not supported.")
    
    contract_code = web3.eth.get_code(to_address)
    if len(contract_code) == 0:
        raise ValueError("This ETH transfer transaction is not supported.")

    formatter = TraceFormatter(PROVIDER_URL)
    processor = BasicBlockProcessor()

    # 1. 获取分析数据
    standardized_trace = formatter.get_standardized_trace(tx_hash)
    contracts_addresses = standardized_trace.get("contracts_addresses", [])
    slot_map = standardized_trace.get("slot_map", {})
    users_addresses = standardized_trace.get("users_addresses", [])
    erc20_token_map = standardized_trace.get("erc20_token_map", {})
    full_address_name_map = standardized_trace.get("full_address_name_map", {})

    # 2. 生成 CFG
    contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)
    all_blocks = processor.process_multiple_contracts(contracts_bytecode)
    token_decimals_map = {addr: formatter.get_token_decimals(addr) for addr in erc20_token_map.keys()}
    cfg_constructor = CFGConstructor(all_blocks, token_decimals_map)

    plain_cfg, folded_cfg, original_cfg, all_changes, folded_node_map, _ = cfg_constructor.construct_cfg(
        standardized_trace, slot_map, erc20_token_map
    )

    # 3. 资产流分析
    original_transfer = [from_address.lower(), to_address.lower(), int(amount)]

    pairs, annotations, pending_erc20 = pair_transactions(original_transfer, all_changes, token_decimals_map)
    edge_link_fcfg = afg_to_fcfg(pairs, pending_erc20, original_cfg, folded_node_map)
    json_output_fcfg = edge_link_to_json(edge_link_fcfg)
    arb_result = detect_arbitrage(pairs, pending_erc20)
    addr_balances = compute_address_balances(pairs, pending_erc20)

    # 4. 保存文件
    # Changes are needed by the evidence panels; the very large full trace is
    # persisted only after AFG has been published.
    with open(os.path.join(result_dir, "balance_and_eth_changes.json"), "w", encoding="utf-8") as f:
        json.dump(all_changes, f, indent=2, ensure_ascii=False)

    # Save edge link mappings
    with open(os.path.join(result_dir, "TFG_link_FCFG.json"), "w", encoding="utf-8") as f:
        f.write(json_output_fcfg)

    # Arbitrage & Balances
    with open(os.path.join(result_dir, "arbitrage.json"), "w", encoding="utf-8") as f:
        json.dump({
            "is_arbitrage": len(arb_result["cycles"]) > 0,
            "cycles": arb_result["cycles"],
            "arb_edge_orders": list(arb_result["arb_edge_orders"])
        }, f, indent=2, ensure_ascii=False)

    with open(os.path.join(result_dir, "address_balances.json"), "w", encoding="utf-8") as f:
        json.dump(addr_balances, f, indent=2, ensure_ascii=False)

    def publish_stage(stage: str) -> None:
        if stage == "afg":
            _write_analysis_status(result_dir, stage)
            with open(os.path.join(result_dir, "trace.json"), "w", encoding="utf-8") as f:
                json.dump(standardized_trace, f, indent=2, ensure_ascii=False)
            return
        if stage == "folded_cfg":
            folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
            folded_blocks_map = cfg_constructor.build_fcfg_blocks_information(folded_cfg)
            folded_blocks_map = _enrich_folded_blocks_information(folded_cfg, folded_blocks_map)
            with open(folded_blocks_path, "w", encoding="utf-8") as f:
                json.dump(folded_blocks_map, f, indent=2, ensure_ascii=False)
            filter_to_file(folded_blocks_path, os.path.join(result_dir, "swap_in_fcfg.json"))

            edge_step_map = _build_edge_step_information_compat(folded_cfg)
            with open(os.path.join(result_dir, "edge_id-step.json"), "w", encoding="utf-8") as f:
                json.dump(edge_step_map, f, indent=2, ensure_ascii=False)
        elif stage == "plain_cfg":
            edge_link_pcfg = afg_to_pcfg(pairs, pending_erc20, plain_cfg)
            with open(os.path.join(result_dir, "TFG_link_PCFG.json"), "w", encoding="utf-8") as f:
                f.write(edge_link_to_json(edge_link_pcfg))

            plain_blocks_path = os.path.join(result_dir, "plain_blocks_information.json")
            plain_blocks_map = cfg_constructor.build_pcfg_blocks_information(plain_cfg, standardized_trace)
            plain_blocks_map = _enrich_folded_blocks_information(plain_cfg, plain_blocks_map)
            with open(plain_blocks_path, "w", encoding="utf-8") as f:
                json.dump(plain_blocks_map, f, indent=2, ensure_ascii=False)
            filter_to_file(plain_blocks_path, os.path.join(result_dir, "swap_in_pcfg.json"))

        _write_analysis_status(result_dir, stage)

    # 5. 渲染图表
    save_graphs(
        result_dir=result_dir,
        plain_cfg=plain_cfg,
        folded_cfg=folded_cfg,
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,
        users_addresses=users_addresses,
        pairs=pairs,
        annotations=annotations,
        pending_erc20=pending_erc20,
        tree_data=lambda: build_refined_hierarchical_trace(standardized_trace["steps"]),
        arb_result=arb_result,
        progress_callback=publish_stage,
    )

    _write_analysis_status(result_dir, "complete")

    print(f"RESULT_DIR={os.path.abspath(result_dir)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main_api.py <tx_hash>", file=sys.stderr)
        sys.exit(1)
    tx_hash_arg = sys.argv[1]
    try:
        run(tx_hash_arg)
    except Exception as exc:
        result_dir_arg = create_result_directory(tx_hash_arg)
        _write_analysis_status(result_dir_arg, "error", str(exc))
        raise
