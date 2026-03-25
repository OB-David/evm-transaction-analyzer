"""CLI wrapper that accepts tx_hash as argument and runs the analysis pipeline."""
import sys
import io
import json
import os
from typing import Any, Dict
from web3 import Web3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.extract_token_changes import pair_transactions, afg_to_fcfg, afg_to_pcfg, edge_link_to_json, detect_arbitrage, compute_address_balances
from utils.sequence_diagram import build_refined_hierarchical_trace
from main import create_result_directory, save_graphs

load_dotenv()


def _normalize_edge_step(value: Any) -> int:
    """Normalize edge_step to int for stable sorting and frontend filtering."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except Exception:
        return 0


def _build_edge_step_information_compat(cfg: Any) -> Dict[str, Dict[str, Any]]:
    """
    Build edge_id-step map for folded/timeline CFG output.
    Also rewrites edge.edge_id as edge_{N} for stable frontend filtering.
    """
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

        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)
        source_id = getattr(source, "id", "unknown")
        target_id = getattr(target, "id", "unknown")

        edge_step_map[edge_id] = {
            "edge_id": edge_id,
            "edge_step": edge_step,
            "source_node": f"node_{source_id}",
            "target_node": f"node_{target_id}",
        }
    return edge_step_map


def run(tx_hash: str):
    PROVIDER_URL = os.environ.get("GETH_API")
    result_dir = create_result_directory(tx_hash)

    formatter = TraceFormatter(PROVIDER_URL)
    processor = BasicBlockProcessor()

    web3 = Web3(Web3.HTTPProvider(PROVIDER_URL))
    tx = web3.eth.get_transaction(tx_hash)
    from_address = tx.get('from')
    to_address = tx.get('to')
    amount = tx.get('value')

    # 检查1：无to_address → 合约创建交易
    if to_address is None or to_address == "":
        print("This contract creation transaction is not the type of transaction we are concerned about.")
        return
    
    # 检查2：to_address不是合约地址 → 普通ETH转账
    contract_code = web3.eth.get_code(to_address)
    if len(contract_code) == 0:
        print("This ETH transfer transaction is not the type of transaction we are concerned about.")
        return


    standardized_trace = formatter.get_standardized_trace(tx_hash)

    contracts_addresses = standardized_trace.get("contracts_addresses", [])
    slot_map = standardized_trace.get("slot_map", {})
    users_addresses = standardized_trace.get("users_addresses", [])
    erc20_token_map = standardized_trace.get("erc20_token_map", {})
    full_address_name_map = standardized_trace.get("full_address_name_map", {})

    contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)
    all_blocks = processor.process_multiple_contracts(contracts_bytecode)

    cfg_constructor = CFGConstructor(all_blocks)
    plain_cfg, folded_cfg, original_cfg, all_changes, folded_node_map, table = cfg_constructor.construct_cfg(
        standardized_trace,
        slot_map,
        erc20_token_map,
    )

    token_decimals_map = {}
    for token_addr in erc20_token_map.keys():
        token_decimals_map[token_addr] = formatter.get_token_decimals(token_addr)

    original_transfer = [from_address.lower(),to_address.lower(), int(amount)]
    pairs, annotations, pending_erc20 = pair_transactions(original_transfer, all_changes, token_decimals_map)
    edge_link_fcfg = afg_to_fcfg(pairs, pending_erc20, original_cfg, folded_node_map)
    edge_link_pcfg = afg_to_pcfg(pairs, pending_erc20, plain_cfg)
    json_output_fcfg = edge_link_to_json(edge_link_fcfg)
    json_output_pcfg = edge_link_to_json(edge_link_pcfg)
    arb_result = detect_arbitrage(pairs, pending_erc20)
    addr_balances = compute_address_balances(pairs, pending_erc20)

    # Save trace
    with open(os.path.join(result_dir, "trace.json"), "w", encoding="utf-8") as f:
        json.dump(standardized_trace, f, indent=2, ensure_ascii=False)

    # Save balance changes
    with open(os.path.join(result_dir, "balance_and_eth_changes.json"), "w", encoding="utf-8") as f:
        json.dump(all_changes, f, indent=2, ensure_ascii=False)

    # Save edge link mappings
    with open(os.path.join(result_dir, "TFG_link_FCFG.json"), "w", encoding="utf-8") as f:
        f.write(json_output_fcfg)
    with open(os.path.join(result_dir, "TFG_link_PCFG.json"), "w", encoding="utf-8") as f:
        f.write(json_output_pcfg)

    # Save folded blocks information
    folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
    cfg_constructor.export_fcfg_blocks_information(folded_cfg, folded_blocks_path)

    plain_blocks_path = os.path.join(result_dir, "plain_blocks_information.json")
    cfg_constructor.export_pcfg_blocks_information(plain_cfg, plain_blocks_path)

    # Save edge step mapping
    edge_info_path = os.path.join(result_dir, "edge_id-step.json")
    edge_step_map = _build_edge_step_information_compat(folded_cfg)
    with open(edge_info_path, "w", encoding="utf-8") as f:
        json.dump(edge_step_map, f, indent=2, ensure_ascii=False)

    # Save arbitrage results
    arb_json_path = os.path.join(result_dir, "arbitrage.json")
    with open(arb_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "is_arbitrage": len(arb_result["cycles"]) > 0,
            "cycles": arb_result["cycles"],
            "arb_edge_orders": list(arb_result["arb_edge_orders"])
        }, f, indent=2, ensure_ascii=False)

    addr_balances_path = os.path.join(result_dir, "address_balances.json")
    with open(addr_balances_path, "w", encoding="utf-8") as f:
        json.dump(addr_balances, f, indent=2, ensure_ascii=False)

    # Build hierarchical trace for sequence diagram generation
    tree_data = build_refined_hierarchical_trace(standardized_trace["steps"])

    # Render graphs
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
        tree_data=tree_data,
        arb_result=arb_result,
    )

    print(f"RESULT_DIR={os.path.abspath(result_dir)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main_api.py <tx_hash>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
