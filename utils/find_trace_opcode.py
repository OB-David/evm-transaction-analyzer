import json
import os
import re

def extract_call_sstore_steps(trace_file, target_contract_address):
    """
    从 EVM trace JSON 文件中提取指定合约的 CALL 和 SSTORE 操作
    """
    CALL_SSTORE = ['CALL', 'STATICCALL', 'DELEGATECALL', 'CALLCODE', 'SSTORE']
    call_sstore_steps = []
    normalized_address = target_contract_address.lower().strip()

    try:
        with open(trace_file, 'r', encoding='utf-8') as f:
            trace_data = json.load(f)
    except FileNotFoundError:
        print(f"❌ 错误：找不到文件 '{trace_file}'")
        return []
    except json.JSONDecodeError as e:
        print(f"❌ 错误：JSON 解析失败：{e}")
        return []

    if 'steps' not in trace_data:
        print("❌ 错误：trace 文件中没有 'steps' 字段")
        return []

    for step in trace_data['steps']:
        addr = step.get('address', '').lower()
        opcode = step.get('opcode', '')

        if addr == normalized_address and opcode in CALL_SSTORE:
            step_info = {
                'address': addr,
                'pc': step['pc'],
                'opcode': opcode,
                'stack': step.get('stack', [])  # 保留完整栈
            }
            call_sstore_steps.append(step_info)

    return call_sstore_steps


def main():
    # 第一次输入：trace 文件路径
    trace_path = input("请输入 EVM trace JSON 文件的路径：").strip().strip('"')
    
    if not os.path.exists(trace_path):
        print(f"❌ 文件不存在：{trace_path}")
        return

    # 第二次输入：目标合约地址
    contract_addr = input("请输入要分析的合约地址：").strip()
    if not re.match(r'^0x[a-fA-F0-9]{40}$', contract_addr):
        print("❌ 地址格式错误，必须是 40 位十六进制地址（含 0x）")
        return

    normalized_addr = contract_addr.lower()
    addr_short = normalized_addr[2:10]  # 去掉 0x，取前 8 位

    print(f"\n🔍 正在分析合约 {contract_addr} 在 trace 中的 CALL 和 SSTORE 操作...\n")

    # 执行提取
    results = extract_call_sstore_steps(trace_path, normalized_addr)

    if not results:
        print(f"⚠️  在合约 {contract_addr} 中未找到 CALL 或 SSTORE 指令。")
        return

    # 准备输出文件
    output_dir = "Result_call_sstore"
    output_filename = f"trace_extract_{addr_short}.txt"
    output_path = os.path.join(output_dir, output_filename)

    # 创建目录
    os.makedirs(output_dir, exist_ok=True)

    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"文件: {trace_path}\n")
        f.write(f"目标合约: {contract_addr}\n")
        f.write(f"共找到 {len(results)} 个 CALL/SSTORE 操作\n")
        f.write("=" * 60 + "\n\n")

        for i, step in enumerate(results, 1):
            f.write(f"[{i:2d}] 地址: {step['address']}  PC: {step['pc']}  指令: {step['opcode']}\n")
            f.write("     栈内容:\n")
            if step['stack']:
                for j, item in enumerate(step['stack']):
                    f.write(f"          [{j:2d}] {item}\n")
            else:
                f.write("          [empty]\n")
            f.write("\n")  # 每个操作之间空一行

        f.write("=" * 60 + "\n")
    
    print(f"✅ 成功！共找到 {len(results)} 个操作，结果已保存至：\n   {output_path}")


if __name__ == '__main__':
    main()