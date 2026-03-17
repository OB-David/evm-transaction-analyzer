import json

def tree_to_puml(trace_tree, output_file="trace_sequence.puml"):
    """
    将 trace_tree 转换为可正常渲染的 PUML 时序图
    :param trace_tree: build_refined_hierarchical_trace 返回的嵌套字典
    :param output_file: 输出的 PUML 文件路径
    """
    # 修复后的 PUML 基础模板（正确样式语法）
    puml_lines = [
        "@startuml",
        "title 合约调用时序图 (Calldata 智能切片版)",
        "skinparam participant {",
        "    BorderColor black",
        "    BackgroundColor lightblue",
        "}",
        "skinparam call {",
        "    BorderColor black",
        "    BackgroundColor lightgreen",
        "}",
        "skinparam return {",
        "    BorderColor black",
        "    BackgroundColor lightyellow",
        "}",
        ""
    ]
    
    participants = set()
    
    def _add_participant(contract_addr):
        """添加参与者（去重，修复别名格式）"""
        if contract_addr and contract_addr not in participants:
            # 简化合约地址显示，别名使用纯地址（去除特殊符号）
            short_addr = f"{contract_addr[:10]}...{contract_addr[-4:]}"
            # 别名用纯地址字符串，避免冒号等特殊字符
            alias = contract_addr.replace("0x", "addr_")
            puml_lines.append(f'participant "{short_addr}" as {alias}')
            participants.add(contract_addr)
            return alias
        return ""
    
    def _recursive_build(node, parent_node=None, depth=0):
        """递归构建时序图节点"""
        current_contract = node.get("contract", "UNKNOWN")
        entry_op = node.get("entry_op", "OUTSIDECALL")
        exit_op = node.get("exit_op", "STOP")
        segments = node.get("calldata_active_segments", [])
        exit_gas = node.get("exit_gas", 0)
        
        current_alias = _add_participant(current_contract)
        
        if parent_node:
            parent_contract = parent_node.get("contract", "CALLER")
            parent_alias = parent_contract.replace("0x", "addr_")
            seg_info = ", ".join([f"段{i['count']}:{i['val'][:20]}..." for i in segments[:3]])
            call_note = f"{entry_op} | Gas累计:{exit_gas} | Calldata切片: {seg_info if seg_info else '无'}"
            puml_lines.append(f"{'    '*depth}{parent_alias} -> {current_alias}: {call_note}")
        
        for child_node in node.get("calls", []):
            _recursive_build(child_node, parent_node=node, depth=depth+1)
        
        if parent_node:
            parent_alias = parent_node.get("contract", "CALLER").replace("0x", "addr_")
            return_note = f"{exit_op} | 退出Gas:{exit_gas}"
            puml_lines.append(f"{'    '*depth}{current_alias} --> {parent_alias}: {return_note}")
    
    _recursive_build(trace_tree)
    puml_lines.append("@enduml")
    
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(puml_lines))
    
    print(f"✅ 修复版 PUML 文件已生成：{output_file}")

# ------------------- 测试示例 -------------------
if __name__ == "__main__":
    # 模拟一个 trace_tree 测试数据（你可替换为实际的 tree 结果）
    sample_tree = {
        "contract": "0x1234567890abcdef1234567890abcdef12345678",
        "entry_op": "OUTSIDECALL",
        "calldata_raw": "0x095ea7b30000000000000000000000001234567890abcdef1234567890abcdef12345678",
        "calldata_active_segments": [
            {"count": 0, "val": "0x095ea7b30000000000000000000000001234567890abcdef"},
            {"count": 32, "val": "0x1234567890abcdef1234567890abcdef12345678"}
        ],
        "calls": [
            {
                "contract": "0x876543210fedcba9876543210fedcba987654321",
                "entry_op": "CALL",
                "calldata_raw": "0x1234567800000000000000000000000000000000",
                "calldata_active_segments": [{"count": 0, "val": "0x1234567800000000000000000000000000000000"}],
                "calls": [],
                "exit_op": "RETURN",
                "exit_gas": 120000
            }
        ],
        "exit_op": "STOP",
        "exit_gas": 250000
    }
    
    # 1. 若使用实际 tree：先加载/生成 trace_tree
    # with open("your_trace_tree.json", "r") as f:
    #     trace_tree = json.load(f)
    
    # 2. 转换为 PUML
    tree_to_puml(sample_tree)