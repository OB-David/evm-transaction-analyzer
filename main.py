import json
import os
from typing import Dict, List, Any
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.render_cfg import render_transaction
from utils.render_token_table import generate_table_excel
from utils.extract_token_changes import pair_transactions, render_asset_flow, afg_to_cfg, edge_link_to_json
from utils.render_legend import render_legend_matplotlib

# 加载环境变量
load_dotenv()
try:
    load_dotenv('.env')
except Exception:
    pass

def create_result_directory(tx_hash: str) -> str:
    """创建结果目录结构: Result/交易哈希/"""
    # 移除交易哈希中的0x前缀
    tx_dir_name = tx_hash.lstrip('0x')
    # 构建完整目录路径
    result_dir = os.path.join("Result", tx_dir_name)
    # 创建目录（如果不存在）
    os.makedirs(result_dir, exist_ok=True)
    return result_dir

def save_graphs(result_dir: str, tx_cfg: object, full_address_name_map: Dict[str, str], erc20_token_map: Dict[str, Any], users_addresses: List[str], pairs: List[Dict[str, Any]], annotations: List[Dict[str, Any]], pending_erc20: List[Dict[str, Any]]):
    '''保存所有图：交易级CFG图、CFG图例、代币交易流图'''

    # 定义Tx_CFG,Asset_Flow和图例的共用颜色规则
    CONTRACT_COLORS = [
    "#FF9E9E", "#81C784", "#64B5F6", "#BA68C8", "#FFCD07", 
    "#4DD0E1", "#FD9800", "#F48FB1", "#AED581", "#7986CB"
    ]
    EDGE_COLOR_MAP = {
        "NORMAL": "#939393",
        "JUMP": "#000000",
        "CALL": "#1F6800",
        "TERMINATE": "#C14A00",
    }

    # 保存交易级CFG的DOT文件
    tx_dot_path = os.path.join(result_dir, "transaction_cfg")
    addr_color_map = render_transaction(
        contract_colors = CONTRACT_COLORS,
        edge_color_map = EDGE_COLOR_MAP,
        cfg=tx_cfg, 
        output_path=tx_dot_path, 
        full_address_name_map = full_address_name_map, 
        erc20_token_map = erc20_token_map,
        rankdir="TB")
    print(f"交易级CFG DOT文件已保存到: {tx_dot_path}.dot")

    # 保存图例 
    print("正在生成CFG图例...")
    render_legend_matplotlib(
        addr_color_map=addr_color_map,
        edge_color_map = EDGE_COLOR_MAP,                       
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,        
        users_addresses=users_addresses,             
        output_path=tx_dot_path)          
    print(f"CFG图例已保存到: {tx_dot_path}_legend.svg")
    
    # 保存代币交易流图的DOT文件
    token_flow_dot_path = os.path.join(result_dir, "asset_flow.dot")
    render_asset_flow(pairs, annotations, users_addresses, full_address_name_map, pending_erc20, addr_color_map, token_flow_dot_path)
    print(f"代币交易流图DOT文件已保存到: {token_flow_dot_path}.dot")


def main():
    # 配置参数
    PROVIDER_URL = os.environ.get("GETH_API")
    TX_HASH = "0xd76d6cf2885323fbe0b9d1795763f8f9d30be648dcf0df4a524f7c3fe5c37177"

    try:
        # 创建结果目录
        result_dir = create_result_directory(TX_HASH)
        print(f"所有结果将保存到: {os.path.abspath(result_dir)}\n")

        # 初始化工具
        formatter = TraceFormatter(PROVIDER_URL)
        processor = BasicBlockProcessor()
        
        # 1. 获取交易的标准化trace（包含 contracts_addresses、slot_map、users_addresses）
        print(f"正在获取交易 {TX_HASH} 的执行轨迹...")
        standardized_trace = formatter.get_standardized_trace(TX_HASH)

        # 2. 提取关键映射数据
        contracts_addresses = standardized_trace.get("contracts_addresses", [])
        slot_map = standardized_trace.get("slot_map", {})
        users_addresses = standardized_trace.get("users_addresses", [])
        erc20_token_map = standardized_trace.get("erc20_token_map", {})
        full_address_name_map = standardized_trace.get("full_address_name_map", {}) 

        print(f"发现合约地址数量: {len(contracts_addresses)}，发现用户地址数量: {len(users_addresses)}")
        print(f"slot_map 项数: {len(slot_map)}\n")

        # 3. 获取所有合约的字节码
        print("正在获取合约字节码...")
        contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)

        # 4. 转换字节码为基本块
        print("正在将字节码转换为基本块...")
        all_blocks = processor.process_multiple_contracts(contracts_bytecode)
        print(f"成功生成 {len(all_blocks)} 个基本块\n")

        # 5. 构建交易级控制流图(CFG)
        print("正在构建交易级控制流图...")
        cfg_constructor = CFGConstructor(all_blocks)
        tx_cfg, all_changes = cfg_constructor.construct_cfg(standardized_trace, slot_map, erc20_token_map)
        print(f"成功构建交易级CFG，包含 {len(tx_cfg.nodes)} 个节点和 {len(tx_cfg.edges)} 条边\n")

        # 6. 生成交易操作表格数据（仅打印提示，实际生成在后续步骤）
        print("正在生成交易操作表格Excel...")
        table = cfg_constructor.table

        # 7. 构建代币交易流，生成边与基本块的映射
        print("正在提取代币交易流...")
        # 先构建代币精度映射
        token_decimals_map = {}
        for token_addr in erc20_token_map.keys():
            decimals = formatter.get_token_decimals(token_addr)
            token_decimals_map[token_addr] = decimals
        # 调用 pair_transactions 时传入精度映射
        pairs, annotations, pending_erc20 = pair_transactions(all_changes, token_decimals_map)
        edge_link = afg_to_cfg(pairs, pending_erc20, cfg_constructor, tx_cfg)
        json_output = edge_link_to_json(edge_link)
        print(f"共提取到 {len(all_changes)} 条资产变更事件，配对成功 {len(pairs)} 对交易流,存在孤立变动{len(annotations)}条\n")

        # 8. 保存轨迹数据
        trace_path = os.path.join(result_dir, "trace.json")
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(standardized_trace, f, indent=2, ensure_ascii=False)
        print(f"轨迹数据（含 addresses 与 slot_map）已保存到: {trace_path}")
        
        # 9. 保存资产变更数据
        changes_path = os.path.join(result_dir, "balance_and_eth_changes.json") 
        with open(changes_path, "w", encoding="utf-8") as f:
            json.dump(all_changes, f, indent=2, ensure_ascii=False)
        print(f"资产变更数据已保存到: {changes_path}")

        # 10. 保存交易操作表格Excel文件
        table_excel_path = os.path.join(result_dir, "transaction_table.xlsx")
        generate_table_excel(cfg_constructor, output_path=table_excel_path)
        print(f"交易操作表格Excel已保存到: {table_excel_path}")

        # 11. 保存边映射JSON文件
        edge_link_path = os.path.join(result_dir, "edge_link.json")
        with open(edge_link_path, "w", encoding="utf-8") as f:
            f.write(json_output)
        print(f"边映射数据已保存到: {edge_link_path}")

        print("\n===== 处理完成 =====")
        print(f"所有结果已保存到: {os.path.abspath(result_dir)}")

        # 12. 渲染并保存三个核心图
        save_graphs(result_dir=result_dir, tx_cfg=tx_cfg, full_address_name_map = full_address_name_map, erc20_token_map=erc20_token_map, 
                    users_addresses=users_addresses, pairs=pairs, annotations=annotations, pending_erc20=pending_erc20)
        
    except Exception as e:
        import traceback
        print(f"\n❌ 执行失败: {str(e)}")
        print("详细错误堆栈：")
        traceback.print_exc()

if __name__ == "__main__":
    main()