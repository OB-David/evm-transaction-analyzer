import json
import os
from contextlib import contextmanager
from time import perf_counter
from web3 import Web3
from typing import Callable, Dict, List, Any, Optional
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.render_cfg import get_valid_nodes_and_colors, render_transaction
from utils.extract_token_changes import pair_transactions, render_asset_flow, afg_to_fcfg, afg_to_pcfg, afg_to_call_tree, build_link_artifact, compute_address_balances, build_balance_timeline, filter_asset_flow_user_addresses
from utils.tfg_cycles import detect_tfg_cycles
from utils.call_tree import build_refined_hierarchical_trace, write_call_tree_json
from utils.indentify_swap import filter_to_file
from utils.analysis_paths import analysis_directory

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
    # Extended pastel palette: same low-saturation visual language, with
    # enough distinct hues for larger transactions before cycling is needed.
    "#E18E8E",
    "#E1A38E",
    "#E1B88E",
    "#E1CC8E",
    "#E1E18E",
    "#CCE18E",
    "#B8E18E",
    "#A3E18E",
    "#8EE18E",
    "#8EE1A3",
    "#8EE1B8",
    "#8EE1CC",
    "#8EE1E1",
    "#8ECCE1",
    "#8EB8E1",
    "#8EA3E1",
    "#8E8EE1",
    "#A38EE1",
    "#B88EE1",
    "#CC8EE1",
    "#E18EE1",
    "#E18ECC",
    "#E18EB8",
    "#E18EA3",
]

EDGE_COLOR_MAP = {
    "NORMAL": "#64748B",
    "JUMP": "#6B5B73",
    "CALL": "#4D7C61",
    "DELEGATECALL": "#4F78A0",
    "TERMINATE": "#9A6658",
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
    TX_HASH = "0x2a0eea5a8c34b9fb296b69f38e78d47f22bb808ede04032876732860e27dac77"

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
        tree_data = build_refined_hierarchical_trace(
            standardized_trace["steps"],
            root_calldata=tx.get("input"),
        )

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
        edge_link1 = afg_to_fcfg(pairs, pending_erc20, folded_cfg)
        edge_link2 = afg_to_pcfg(pairs, pending_erc20, plain_cfg)
        edge_link_call_tree = afg_to_call_tree(pairs, pending_erc20, tree_data)
        
        print(f"共提取到 {len(all_changes)} 条资产变更事件，配对成功 {len(pairs)} 对交易流,存在孤立变动{len(annotations)}条\n")
        tfg_cycle_result = detect_tfg_cycles(pairs)
        addr_balances = compute_address_balances(pairs, pending_erc20)
        balance_timeline = build_balance_timeline(pairs, pending_erc20)

        # 8. 保存折叠后Block ID与Instructions映射数据
        print("正在导出可见Block ID与Instructions映射...")
        folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
        cfg_constructor.export_fcfg_blocks_information(folded_cfg, folded_blocks_path)

        plain_blocks_path = os.path.join(result_dir, "plain_blocks_information.json")
        plain_blocks_map = cfg_constructor.build_pcfg_blocks_information(plain_cfg, standardized_trace)
        with open(plain_blocks_path, "w", encoding="utf-8") as f:
            json.dump(plain_blocks_map, f, ensure_ascii=False, indent=2)


        swap_fcfg_path = os.path.join(result_dir, "swap_in_fcfg.json")
        swap_pcfg_path = os.path.join(result_dir, "swap_in_pcfg.json")
        # 提取swap模式
        filter_to_file(folded_blocks_path, swap_fcfg_path)
        filter_to_file(plain_blocks_path, swap_pcfg_path)

        print(f"blcokid-information映射数据已保存到: {folded_blocks_path}")

        # 9. 保存资产变更数据
        changes_path = os.path.join(result_dir, "balance_and_eth_changes.json") 
        with open(changes_path, "w", encoding="utf-8") as f:
            json.dump(all_changes, f, indent=2, ensure_ascii=False)
        print(f"资产变更数据已保存到: {changes_path}")

        # 11. 折叠/普通 CFG 关联只保存为一个版本化文件。
        link_path = os.path.join(result_dir, "link.json")
        with open(link_path, "w", encoding="utf-8") as f:
            json.dump(build_link_artifact(edge_link1, edge_link2, edge_link_call_tree), f, ensure_ascii=False)
        for legacy_name in ("TFG_link_FCFG.json", "TFG_link_PCFG.json"):
            legacy_path = os.path.join(result_dir, legacy_name)
            if os.path.isfile(legacy_path):
                os.remove(legacy_path)
        print(f"图关联数据已保存到: {link_path}")

        tfg_cycles_path = os.path.join(result_dir, "tfg_cycles.json")
        with open(tfg_cycles_path, "w", encoding="utf-8") as f:
            json.dump(tfg_cycle_result, f, indent=2, ensure_ascii=False)

        addr_balances_path = os.path.join(result_dir, "address_balances.json")
        with open(addr_balances_path, "w", encoding="utf-8") as f:
            json.dump(addr_balances, f, indent=2, ensure_ascii=False)

        balance_timeline_path = os.path.join(result_dir, "balance_timeline.json")
        with open(balance_timeline_path, "w", encoding="utf-8") as f:
            json.dump(balance_timeline, f, indent=2, ensure_ascii=False)

        print("\n===== 处理完成 =====")
        print(f"所有结果已保存到: {os.path.abspath(result_dir)}")

        # 12. 渲染并保存三个核心图
        save_graphs(result_dir=result_dir, plain_cfg=plain_cfg, folded_cfg = folded_cfg, full_address_name_map = full_address_name_map, erc20_token_map=erc20_token_map, 
                    users_addresses=users_addresses, pairs=pairs, annotations=annotations, pending_erc20=pending_erc20,
                    tree_data=tree_data)

    except Exception as e:
        import traceback
        print(f"\n[ERROR] 执行失败: {str(e)}")
        print("详细错误堆栈：")
        traceback.print_exc()

def create_result_directory(tx_hash: str) -> str:
    """创建结果目录结构: data_base/analysis/交易哈希/。"""
    result_dir = analysis_directory(tx_hash)
    result_dir.mkdir(parents=True, exist_ok=True)
    return str(result_dir)

def save_graphs(result_dir: str, plain_cfg: object,folded_cfg:object, full_address_name_map: Dict[str, str], erc20_token_map: Dict[str, Any], users_addresses: List[str], pairs: List[Dict[str, Any]], annotations: List[Dict[str, Any]], pending_erc20: List[Dict[str, Any]], tree_data, progress_callback: Optional[Callable[[str], None]] = None, timing_callback: Optional[Callable[[str, float], None]] = None):
    '''保存可重建图源；SVG 只按请求渲染，不进入分析目录。'''

    # Re-analysis may reuse a result directory created by an older version.
    for name in os.listdir(result_dir):
        path = os.path.join(result_dir, name)
        if name.lower().endswith(".svg") and os.path.isfile(path):
            os.remove(path)

    def report(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(stage)

    @contextmanager
    def measure(name: str):
        started = perf_counter()
        try:
            yield
        finally:
            if timing_callback is not None:
                timing_callback(name, perf_counter() - started)

    # AFG needs the shared contract palette, but not a rendered CFG yet.
    with measure("prepare_graph_color_map"):
        _, _, addr_color_map = get_valid_nodes_and_colors(plain_cfg, CONTRACT_COLORS)

    # 保存代币交易流图的DOT文件
    token_flow_dot_path = os.path.join(result_dir, "asset_flow.dot")
    tfg_user_addresses = filter_asset_flow_user_addresses(
        users_addresses, pairs, pending_erc20
    )
    with measure("render_afg_dot"):
        render_asset_flow(pairs, annotations, tfg_user_addresses,
                      full_address_name_map, pending_erc20,
                      addr_color_map, token_flow_dot_path,
                      erc20_token_map=erc20_token_map)
    print(f"代币交易流图DOT文件已保存到: {token_flow_dot_path}.dot")

    # TFG 完成后再构建 legend，只保留真实参与 TFG 的用户地址。
    legend_data: Dict[str, Any] = {"user_addresses": [], "erc20_tokens": [], "normal_contracts": []}
    if tfg_user_addresses:
        user_items = [
            (addr, full_address_name_map.get(addr, f"User_{addr[:6]}"))
            for addr in tfg_user_addresses
        ]
        user_items.sort(
            key=lambda item: (0, item[1].lower())
            if item[1] == "User_From"
            else (1, item[1].lower())
        )
        for addr, name in user_items:
            legend_data["user_addresses"].append({"name": name, "address": addr})

    # Contract entries retain the shared CFG palette used across visualizations.
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
    with measure("write_legend_json"):
        with open(legend_json_path, "w", encoding="utf-8") as f:
            json.dump(legend_data, f, indent=2, ensure_ascii=False)
    print(f"图例JSON已保存到: {legend_json_path}")
    report("afg")


    # Persist the semantic call tree directly; the frontend renders it as SVG.
    with measure("build_sequence_tree"):
        resolved_tree_data = tree_data() if callable(tree_data) else tree_data
    call_tree_path = os.path.join(result_dir, "call_tree.json")
    with measure("write_call_tree_json"):
        write_call_tree_json(
            trace_tree=resolved_tree_data,
            output_file=call_tree_path,
            erc20_token_map=erc20_token_map,
            full_address_name_map=full_address_name_map,
        )
    print(f"调用树 JSON 已保存到: {call_tree_path}")
    report("sequence")

    # Persist graph source, not rendered SVG. The API renders SVG on demand.
    folded_dot_path = os.path.join(result_dir, "folded_cfg.dot")
    with measure("render_folded_cfg_dot"):
        render_transaction(
            contract_colors=CONTRACT_COLORS,
            edge_color_map=EDGE_COLOR_MAP,
            cfg=folded_cfg,
            output_path=folded_dot_path,
            full_address_name_map=full_address_name_map,
            erc20_token_map=erc20_token_map,
            rankdir="LR",
            show_priority_opcode=False,
        )
    report("folded_cfg")

    # Plain CFG is intentionally last because it is usually the largest view.
    plain_dot_path = os.path.join(result_dir, "plain_cfg.dot")
    with measure("render_plain_cfg_dot"):
        render_transaction(
            contract_colors=CONTRACT_COLORS,
            edge_color_map=EDGE_COLOR_MAP,
            cfg=plain_cfg,
            output_path=plain_dot_path,
            full_address_name_map=full_address_name_map,
            erc20_token_map=erc20_token_map,
            rankdir="LR",
            show_priority_opcode=True,
        )
    report("plain_cfg")


if __name__ == "__main__":
    main()
