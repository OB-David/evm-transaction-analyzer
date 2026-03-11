"""CLI wrapper that accepts tx_hash as argument and runs the analysis pipeline."""
import sys
import io
import json
import os
from web3 import Web3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.extract_token_changes import pair_transactions, afg_to_cfg, edge_link_to_json
from main import create_result_directory, save_graphs

load_dotenv()

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
    tx_cfg, all_changes, folded_node_map, table = cfg_constructor.construct_cfg(standardized_trace, slot_map, erc20_token_map)

    token_decimals_map = {}
    for token_addr in erc20_token_map.keys():
        token_decimals_map[token_addr] = formatter.get_token_decimals(token_addr)

    original_transfer = [from_address.lower(),to_address.lower(), int(amount)]
    pairs, annotations, pending_erc20 = pair_transactions(original_transfer, all_changes, token_decimals_map)
    edge_link = afg_to_cfg(pairs, pending_erc20, cfg_constructor, tx_cfg, folded_node_map)
    json_output = edge_link_to_json(edge_link)

    # Save trace
    with open(os.path.join(result_dir, "trace.json"), "w", encoding="utf-8") as f:
        json.dump(standardized_trace, f, indent=2, ensure_ascii=False)

    # Save balance changes
    with open(os.path.join(result_dir, "balance_and_eth_changes.json"), "w", encoding="utf-8") as f:
        json.dump(all_changes, f, indent=2, ensure_ascii=False)

    # Save edge link
    with open(os.path.join(result_dir, "edge_link.json"), "w", encoding="utf-8") as f:
        f.write(json_output)

    # Save folded blocks information
    folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
    cfg_constructor.export_folded_blocks_information(tx_cfg, folded_blocks_path)

    # Render graphs
    save_graphs(
        result_dir=result_dir,
        tx_cfg=tx_cfg,
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,
        users_addresses=users_addresses,
        pairs=pairs,
        annotations=annotations,
        pending_erc20=pending_erc20,
    )

    print(f"RESULT_DIR={os.path.abspath(result_dir)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main_api.py <tx_hash>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
