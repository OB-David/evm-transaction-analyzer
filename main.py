import shutil
print("cast path:", shutil.which("cast"))


import json
import os
from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor, render_transaction
from utils.token_table import generate_table_excel
from utils.extract_token_changes import balance_change, pair_transactions, render_asset_flow
from collections import defaultdict

load_dotenv()
try:
    load_dotenv('.env')
except Exception:
    pass

def create_result_directory(tx_hash: str) -> str:
    """创建结果目录结构: Result/交易哈希/"""
    # 移除交易哈希中的0x前缀作
    tx_dir_name = tx_hash.lstrip('0x')
    # 构建完整目录路径
    result_dir = os.path.join("Result", tx_dir_name)
    # 创建目录（如果不存在）
    os.makedirs(result_dir, exist_ok=True)
    return result_dir

def main():
    # 配置参数
    PROVIDER_URL = os.environ.get("GETH_API")
    TX_HASH = "0xed545a57f352451fd1cf9afa68b54a136169f2c9baa64f9c09e2b5cb477326e8"

    try:
        # 创建结果目录
        result_dir = create_result_directory(TX_HASH)
        print(f"所有结果将保存到: {os.path.abspath(result_dir)}\n")

        # 初始化工具
        formatter = TraceFormatter(PROVIDER_URL)
        processor = BasicBlockProcessor()
        
        # 1. 获取交易的标准化trace（现在 trace 内应包含 contracts_addresses、slot_map、users_addresses）
        print(f"正在获取交易 {TX_HASH} 的执行轨迹...")
        standardized_trace = formatter.get_standardized_trace(TX_HASH)

        # 2. 从 standardized_trace 中直接读取 contracts_addresses、slot_map、users_addresses（无需单独保存中间结果）
        contracts_addresses = standardized_trace.get("contracts_addresses", [])
        slot_map = standardized_trace.get("slot_map", {})
        users_addresses = standardized_trace.get("users_addresses", [])
        erc20_token_map = standardized_trace.get("erc20_token_map", {})

        print(f"发现合约地址数量: {len(contracts_addresses)}，发现用户地址数量: {len(users_addresses)}")
        # 可选：打印 slot_map 大小
        print(f"slot_map 项数: {len(slot_map)}")
        

        # 3. 获取所有合约的字节码（使用 standardized_trace 中提取的 contracts_addresses）
        print("正在获取合约字节码...")
        contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)

        # 4. 转换字节码为基本块
        print("正在将字节码转换为基本块...")
        all_blocks = processor.process_multiple_contracts(contracts_bytecode)
        print(f"成功生成 {len(all_blocks)} 个基本块\n")

        # 5. 构建交易级控制流图(CFG)
        print("正在构建交易级控制流图...")
        cfg_constructor = CFGConstructor(all_blocks)
        tx_cfg = cfg_constructor.construct_cfg(standardized_trace,slot_map,erc20_token_map)
        print(f"成功构建交易级CFG，包含 {len(tx_cfg.nodes)} 个节点和 {len(tx_cfg.edges)} 条边\n")

        # 6. 生成交易操作表格数据
        print("正在生成交易操作表格Excel...")
        table = cfg_constructor.table

        # 7. 构建代币交易流
        print("正在提取代币交易流...")
        all_changes = balance_change(table)
        pairs, annotations = pair_transactions(all_changes)
        print(f"共提取到 {len(all_changes)} 条资产变更事件，配对成功 {len(pairs)} 对交易流,存在孤立变动{len(annotations)}条\n")

        # 8. 保存轨迹数据（包含 contracts_addresses、slot_map、users_addresses）
        trace_path = os.path.join(result_dir, "trace.json")
        with open(trace_path, "w") as f:
            json.dump(standardized_trace, f, indent=2)
        print(f"\n轨迹数据（含 addresses 与 slot_map）已保存到: {trace_path}")
        
        # 9. 保存基本块数据
        blocks_path = os.path.join(result_dir, "blocks.json")
        with open(blocks_path, "w") as f:
            blocks_data = []
            for block in all_blocks:
                blocks_data.append({
                    "address": block.address,
                    "start_pc": block.start_pc,
                    "end_pc": block.end_pc,
                    "terminator": block.terminator,
                    "instructions": block.instructions
                })
            json.dump(blocks_data, f, indent=2)
        print(f"基本块数据已保存到: {blocks_path}")
        
        # 10. 保存交易级CFG的DOT文件
        tx_dot_path = os.path.join(result_dir, "transaction_cfg.dot")
        render_transaction(tx_cfg, tx_dot_path)
        print(f"交易级CFG DOT文件已保存到: {tx_dot_path}")

        # 11. 保存交易操作表格Excel文件
        table_excel_path = os.path.join(result_dir, "transaction_table.xlsx")
        generate_table_excel(cfg_constructor, output_path=table_excel_path)
        print(f"交易操作表格Excel已保存到: {table_excel_path}")

        # 12. 保存代币交易流图的DOT文件
        token_flow_dot_path = os.path.join(result_dir, "asset_flow")
        render_asset_flow(pairs,annotations,users_addresses,token_flow_dot_path)

        
        print("\n===== 处理完成 =====")
        print(f"所有结果已保存到: {os.path.abspath(result_dir)}")

        
        
    except Exception as e:
        print(f"执行失败: {str(e)}")

if __name__ == "__main__":
    main()