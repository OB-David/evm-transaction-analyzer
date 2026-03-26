import json
import os
from web3 import Web3
from typing import Dict, List, Any
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.render_cfg import render_transaction
from utils.extract_token_changes import pair_transactions, render_asset_flow, afg_to_fcfg, afg_to_pcfg, edge_link_to_json, detect_arbitrage, compute_address_balances
from utils.sequence_diagram import build_refined_hierarchical_trace, render_puml_to_svg, tree_to_puml
from utils.indentify_swap import filter_to_file

CONTRACT_COLORS = [
    "#F4B9B9",
    "#F3DAB5",
    "#F2EBB5",
    "#D2F3B4",
    "#B4F3BA",
    "#B5F2D3",
    "#B5EBF4",
    "#B6CDF3",
    "#C3B5F2",
    "#EBB8F4",
    "#F3B4DB",
    "#E2E2E2",
]

EDGE_COLOR_MAP = {
    "NORMAL": "#C2CAD7",
    "JUMP": "#D8D2CA",
    "CALL": "#A9C7AE",
    "DELEGATECALL": "#ABC0D9",
    "TERMINATE": "#DABAAE",
}

# 加载环境变量
load_dotenv()
try:
    load_dotenv('.env')
except Exception:
    pass

def main():
    # 配置参数
    PROVIDER_URL = os.environ.get("GETH_API")
    TX_HASH = "0xe01eac5e811602c54f3fe5484d44f13a5b621aa19f670dde7139a0f6760a1916"

    try:
        # ========== 前置检查 ==========
        print(f"正在检查交易 {TX_HASH} 的基础信息...")
        
        # 初始化Web3
        web3 = Web3(Web3.HTTPProvider(PROVIDER_URL))
        if not web3.is_connected():
            raise Exception(f"无法连接到以太坊节点: {PROVIDER_URL}")
        
        # 获取交易信息
        tx = web3.eth.get_transaction(TX_HASH)
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


        # 通过检查后开始分析
        # 创建结果目录
        result_dir = create_result_directory(TX_HASH)
        print(f"所有结果将保存到: {os.path.abspath(result_dir)}\n")

        # 初始化工具
        formatter = TraceFormatter(PROVIDER_URL)
        processor = BasicBlockProcessor()
        
        # 1. 获取交易的标准化trace（包含 contracts_addresses、slot_map、users_addresses）
        print(f"正在获取交易 {TX_HASH} 的执行轨迹...")
        standardized_trace = formatter.get_standardized_trace(TX_HASH)


        print("正在生成调用树")
        # 2. 生成调用树
        tree_data = build_refined_hierarchical_trace(standardized_trace["steps"])

        # 3. 提取关键映射数据
        contracts_addresses = standardized_trace.get("contracts_addresses", [])
        slot_map = standardized_trace.get("slot_map", {})
        users_addresses = standardized_trace.get("users_addresses", [])
        erc20_token_map = standardized_trace.get("erc20_token_map", {})
        full_address_name_map = standardized_trace.get("full_address_name_map", {}) 

        print(f"发现合约地址数量: {len(contracts_addresses)}，发现用户地址数量: {len(users_addresses)}")
        print(f"slot_map 项数: {len(slot_map)}\n")

        # 4. 获取所有合约的字节码
        print("正在获取合约字节码...")
        contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)

        # 5. 转换字节码为基本块
        print("正在将字节码转换为基本块...")
        all_blocks = processor.process_multiple_contracts(contracts_bytecode)
        print(f"成功生成 {len(all_blocks)} 个基本块\n")

        token_decimals_map = {}
        for token_addr in erc20_token_map.keys():
            decimals = formatter.get_token_decimals(token_addr)
            token_decimals_map[token_addr] = decimals

        # 6. 构建交易级控制流图(CFG)
        print("正在构建交易级控制流图...")
        cfg_constructor = CFGConstructor(all_blocks, token_decimals_map)
        # 返回基本块连接的原始original_cfg，折叠后的tx_cfg
        # 查询关联都基于original_cfg
        # original_cfg基于pc，tx_cfg基于original_cfg的blockid
        plain_cfg, folded_cfg, original_cfg, all_changes, folded_node_map, table = cfg_constructor.construct_cfg(standardized_trace, slot_map, erc20_token_map)
        print(f"成功构建交plain CFG，包含 {len(plain_cfg.nodes)} 个节点和 {len(plain_cfg.edges)} 条边\n")
        print(f"成功构建交folded CFG，包含 {len(folded_cfg.nodes)} 个节点和 {len(folded_cfg.edges)} 条边\n")


        # 7. 构建代币交易流，生成边与基本块的映射
        print("正在提取代币交易流...")
        # 调用 pair_transactions 时传入精度映射
        original_transfer = [from_address.lower(),to_address.lower(), int(amount)]
        print(int(amount))
        pairs, annotations, pending_erc20 = pair_transactions(original_transfer,all_changes, token_decimals_map)
        edge_link1 = afg_to_fcfg(pairs, pending_erc20, original_cfg, folded_node_map)
        edge_link2 = afg_to_pcfg(pairs, pending_erc20, plain_cfg)
        
        json_output1 = edge_link_to_json(edge_link1)
        json_output2 = edge_link_to_json(edge_link2)
        print(f"共提取到 {len(all_changes)} 条资产变更事件，配对成功 {len(pairs)} 对交易流,存在孤立变动{len(annotations)}条\n")
        arb_result = detect_arbitrage(pairs, pending_erc20)
        addr_balances = compute_address_balances(pairs, pending_erc20)

        # 8. 保存轨迹数据
        trace_path = os.path.join(result_dir, "trace.json")
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(standardized_trace, f, indent=2, ensure_ascii=False)
        print(f"轨迹数据（含 addresses 与 slot_map）已保存到: {trace_path}")

        # 9. 保存折叠后Block ID与Instructions映射数据
        print("正在导出可见Block ID与Instructions映射...")
        folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
        cfg_constructor.export_fcfg_blocks_information(folded_cfg, folded_blocks_path)

        plain_blocks_path = os.path.join(result_dir, "plain_blocks_information.json")
        cfg_constructor.export_pcfg_blocks_information(plain_cfg, standardized_trace, plain_blocks_path)


        swap_fcfg_path = os.path.join(result_dir, "swap_in_fcfg.json")
        swap_pcfg_path = os.path.join(result_dir, "swap_in_pcfg.json")
        # 提取swap模式
        filter_to_file(folded_blocks_path, swap_fcfg_path)
        filter_to_file(plain_blocks_path, swap_pcfg_path)

        print(f"blcokid-information映射数据已保存到: {folded_blocks_path}")

        # 10. 保存资产变更数据
        changes_path = os.path.join(result_dir, "balance_and_eth_changes.json") 
        with open(changes_path, "w", encoding="utf-8") as f:
            json.dump(all_changes, f, indent=2, ensure_ascii=False)
        print(f"资产变更数据已保存到: {changes_path}")

        # 11. 保存边映射JSON文件
        edge_link_path1 = os.path.join(result_dir, "TFG_link_FCFG.json")
        edge_link_path2 = os.path.join(result_dir, "TFG_link_PCFG.json")
        with open(edge_link_path1, "w", encoding="utf-8") as f:
            f.write(json_output1)
        print(f"边映射数据已保存到: {edge_link_path1}")
        with open(edge_link_path2, "w", encoding="utf-8") as f:
            f.write(json_output2)

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

        print("\n===== 处理完成 =====")
        print(f"所有结果已保存到: {os.path.abspath(result_dir)}")

        # 12. 渲染并保存三个核心图
        save_graphs(result_dir=result_dir, plain_cfg=plain_cfg, folded_cfg = folded_cfg, full_address_name_map = full_address_name_map, erc20_token_map=erc20_token_map, 
                    users_addresses=users_addresses, pairs=pairs, annotations=annotations, pending_erc20=pending_erc20,
                    tree_data = tree_data, arb_result  = arb_result)

    except Exception as e:
        import traceback
        print(f"\n[ERROR] 执行失败: {str(e)}")
        print("详细错误堆栈：")
        traceback.print_exc()

def create_result_directory(tx_hash: str) -> str:
    """创建结果目录结构: Result/交易哈希/"""
    # 移除交易哈希中的0x前缀
    tx_dir_name = tx_hash.lstrip('0x')
    # 构建完整目录路径
    result_dir = os.path.join("Result", tx_dir_name)
    # 创建目录（如果不存在）
    os.makedirs(result_dir, exist_ok=True)
    return result_dir

def save_graphs(result_dir: str, plain_cfg: object,folded_cfg:object, full_address_name_map: Dict[str, str], erc20_token_map: Dict[str, Any], users_addresses: List[str], pairs: List[Dict[str, Any]], annotations: List[Dict[str, Any]], pending_erc20: List[Dict[str, Any]], tree_data, arb_result):
    '''渲染并保存所有图：交易级CFG图、CFG图例、代币交易流图'''

    # 保存交易级CFG的DOT文件
    tx_dot_path = os.path.join(result_dir, "plain_cfg")
    addr_color_map = render_transaction(
        contract_colors = CONTRACT_COLORS,
        edge_color_map = EDGE_COLOR_MAP,
        cfg=plain_cfg, 
        output_path=tx_dot_path, 
        full_address_name_map = full_address_name_map, 
        erc20_token_map = erc20_token_map,
        rankdir="LR",
        show_priority_opcode=True)
    print(f"交易级CFG DOT文件已保存到: {tx_dot_path}.dot")

    # Render DOT to SVG using Graphviz CLI for frontend display
    import subprocess
    cfg_dot_file = f"{tx_dot_path}.dot"
    cfg_svg_file = os.path.join(result_dir, "plain_cfg.svg")
    try:
        subprocess.run(
            ["dot", "-Tsvg", cfg_dot_file, "-o", cfg_svg_file],
            check=True, capture_output=True, text=True, timeout=120
        )
        print(f"CFG SVG已生成: {cfg_svg_file}")
    except Exception as e:
        print(f"WARNING: CFG SVG生成失败: {e}")




        # 保存交易级CFG的DOT文件
    tx_dot_path = os.path.join(result_dir, "folded_cfg")
    addr_color_map = render_transaction(
        contract_colors = CONTRACT_COLORS,
        edge_color_map = EDGE_COLOR_MAP,
        cfg=folded_cfg, 
        output_path=tx_dot_path, 
        full_address_name_map = full_address_name_map, 
        erc20_token_map = erc20_token_map,
        rankdir="LR",
        show_priority_opcode=False)
    print(f"交易级CFG DOT文件已保存到: {tx_dot_path}.dot")

    # Render DOT to SVG using Graphviz CLI for frontend display
    import subprocess
    cfg_dot_file = f"{tx_dot_path}.dot"
    cfg_svg_file = os.path.join(result_dir, "folded_cfg.svg")
    try:
        subprocess.run(
            ["dot", "-Tsvg", cfg_dot_file, "-o", cfg_svg_file],
            check=True, capture_output=True, text=True, timeout=120
        )
        print(f"CFG SVG已生成: {cfg_svg_file}")
    except Exception as e:
        print(f"WARNING: CFG SVG生成失败: {e}")

    # 保存legend.json供前端使用
    legend_data: Dict[str, Any] = {"user_addresses": [], "erc20_tokens": [], "normal_contracts": []}

    # User addresses (same sorting as render_legend)
    if users_addresses:
        user_items = [(addr, full_address_name_map.get(addr, f"User_{addr[:6]}")) for addr in users_addresses]
        user_items.sort(key=lambda item: (0, item[1].lower()) if item[1] == "User_From" else (1, item[1].lower()))
        for addr, name in user_items:
            legend_data["user_addresses"].append({"name": name, "address": addr})

    # Split contracts into ERC20 and normal
    for contract_addr, color in addr_color_map.items():
        contract_addr_lower = contract_addr.lower()
        contract_name = full_address_name_map.get(contract_addr_lower, contract_addr[:10])
        entry = {"name": contract_name, "address": contract_addr, "color": color}
        if contract_addr_lower in erc20_token_map:
            legend_data["erc20_tokens"].append(entry)
        else:
            legend_data["normal_contracts"].append(entry)

    legend_data["erc20_tokens"].sort(key=lambda x: x["name"].lower())
    legend_data["normal_contracts"].sort(key=lambda x: x["name"].lower())

    legend_json_path = os.path.join(result_dir, "legend.json")
    with open(legend_json_path, "w", encoding="utf-8") as f:
        json.dump(legend_data, f, indent=2, ensure_ascii=False)
    print(f"图例JSON已保存到: {legend_json_path}")
    
    # 保存代币交易流图的DOT文件
    arb_orders  = arb_result["arb_edge_orders"]
    token_flow_dot_path = os.path.join(result_dir, "asset_flow.dot")
    render_asset_flow(pairs, annotations, users_addresses,
                  full_address_name_map, pending_erc20,
                  addr_color_map, token_flow_dot_path,
                  arb_edge_orders=arb_orders)
    print(f"代币交易流图DOT文件已保存到: {token_flow_dot_path}.dot")


    # 生成时序图
    print("正在生成时序图PUML文件...")
    puml_path = os.path.join(result_dir, "trace_sequence.puml")
    tree_to_puml(
        trace_tree=tree_data,
        output_file=puml_path,
        erc20_token_map=erc20_token_map,
        full_address_name_map=full_address_name_map,
        addr_color_map=addr_color_map
    )
    print(f"时序图PUML已保存到: {puml_path}")
    render_puml_to_svg(puml_path)


if __name__ == "__main__":
    main()
