import json

def build_refined_hierarchical_trace(steps):
    """
    calldata断点切片:
    Primary Breakpoints (首断点): CALLDATALOAD 传入的 offset。
    Hidden Breakpoints (隐藏断点) 分两类：
       - 起点=0：读取8个16进制字符（4字节）
       - 起点≠0：读取64个16进制字符（32字节）
    自动截断: 段终点 = min(下一个首断点, 隐藏断点, 数据总长)。
    """
    if not steps:
        return {}

    # 定义常量（移到函数内部，避免外部依赖）
    CALL_OPS = {"CALL", "DELEGATECALL", "STATICCALL", "CALLCODE", "CREATE", "CREATE2"}
    RETURN_OPS = {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}
    
    global_gas_counter = 0

    def process_level(start_index):
        nonlocal global_gas_counter
        start_step = steps[start_index]
        
        last_step_op = "OUTSIDECALL"
        raw_calldata = ""
        primary_breakpoints = set()

        # --- 1. 提取原始数据  ---
        if start_index != 0:
            prev_step = steps[start_index - 1]
            last_step_op = prev_step.get("opcode", "OUTSIDECALL")  # 适配opcode字段
            stack = prev_step.get("stack", [])
            memory = prev_step.get("memory", [])
            full_mem = "".join([m for m in memory if m])
            
            try:
                if last_step_op in ["CALL", "CALLCODE"]:
                    offset = int(stack[-4], 16) if len(stack) >=4 else 0
                    size = int(stack[-5], 16) if len(stack) >=5 else 0
                elif last_step_op in ["DELEGATECALL", "STATICCALL"]:
                    offset = int(stack[-3], 16) if len(stack) >=3 else 0
                    size = int(stack[-4], 16) if len(stack) >=4 else 0
                elif last_step_op in ["CREATE", "CREATE2"]:
                    offset = int(stack[-2], 16) if len(stack) >=2 else 0
                    size = int(stack[-3], 16) if len(stack) >=3 else 0
                else: 
                    offset, size = 0, 0
                
                if size > 0 and offset >=0:
                    # 计算内存截取范围（适配hex字符串）
                    start_pos = offset * 2
                    end_pos = (offset + size) * 2
                    if len(full_mem) >= end_pos: 
                        raw_calldata = full_mem[start_pos:end_pos]  
                    else:
                        raw_calldata = ''
                        print("no calldata error")
            except (IndexError, ValueError, TypeError): 
                # 容错：stack长度不足/转换失败时置空
                raw_calldata = ""

        # 直接使用原生depth字段
        current_level_depth = start_step.get("depth", 0)
        
        node = {
            "contract": start_step.get("address"),
            "depth": current_level_depth,
            "entry_op": last_step_op,
            "entry_step": start_index - 1,
            "calldata_raw": "0x" + raw_calldata if raw_calldata else "",
            "calldata_active_segments": [],
            "calls": [],
            "exit_op": "PENDING", 
            "exit_step": 1,
            "exit_gas": 0
        }

        # --- 2. 遍历执行流并收集首断点 (适配原生depth) ---
        i = start_index
        while i < len(steps):
            step = steps[i]
            # 直接使用原生depth判断层级（核心逻辑）
            step_depth = step.get("depth", 0)
            
            if step_depth > current_level_depth:
                # 子调用：递归处理
                child_node, next_i = process_level(i)
                node["calls"].append(child_node)
                i = next_i
                continue
            
            if step_depth < current_level_depth:
                # 层级回退：退出当前层级处理
                break

            op = step.get("opcode", "")  # 适配opcode字段
            stk = step.get("stack", [])
            
            # 记录退出操作
            if op in RETURN_OPS:
                node["exit_op"] = op
                node["exit_step"] = i

            # 收集CALLDATALOAD断点
            if op == "CALLDATALOAD" and len(stk) > 0:
                try:
                    off = int(stk[-1], 16)
                    num_bytes = len(raw_calldata) // 2
                    if off < num_bytes:
                        primary_breakpoints.add(off)
                except (ValueError, IndexError):
                    pass

            # 累计Gas消耗
            gas_cost = step.get("gascost", 0)
            global_gas_counter += int(gas_cost) if str(gas_cost).isdigit() else 0
            i += 1

        # --- 按起点类型区分切片长度 ---
        if raw_calldata:
            # 强制将 0 加入首断点（确保选择器等起始位被识别）
            primary_breakpoints.add(0)
            sorted_p = sorted(list(primary_breakpoints))
            segments = []
            
            num_bytes = len(raw_calldata) // 2  # 总字节数
            for idx, p_start in enumerate(sorted_p):
                # 隐藏断点规则：
                # 1. 起点=0 → 读取4字节（8个16进制字符）
                # 2. 起点≠0 → 读取32字节（64个16进制字符）
                if p_start == 0:
                    hidden_end = p_start + 4  # 4字节（函数选择器）
                else:
                    hidden_end = p_start + 32  # 32字节（标准EVM数据）
                
                # 定义“逻辑终点”：
                # 1. 如果有下一个首断点，则不能超过它
                # 2. 不能超过当前隐藏断点（按规则计算）
                # 3. 不能超过数据总长
                if idx + 1 < len(sorted_p):
                    next_p = sorted_p[idx + 1]
                    actual_end = min(next_p, hidden_end, num_bytes)
                else:
                    actual_end = min(hidden_end, num_bytes)
                
                # 只有当区间有效时才截取（处理重叠或末尾情况）
                if p_start < actual_end:
                    # 计算16进制字符串的截取范围（1字节=2个16进制字符）
                    hex_start = p_start * 2
                    hex_end = actual_end * 2
                    seg_hex = raw_calldata[hex_start:hex_end]
                    segments.append({
                        "count": idx,
                        "val": f"0x{seg_hex}"
                    })
            
            node["calldata_active_segments"] = segments

        # 补全退出状态
        node["exit_gas"] = global_gas_counter
        if node["exit_op"] == "PENDING": 
            node["exit_op"] = "STOP"

        return node, i

    # 从根节点开始构建（直接使用原始steps）
    root_tree, _ = process_level(0)
    return root_tree




def tree_to_puml(trace_tree, output_file, erc20_token_map, full_address_name_map, addr_color_map):
    # 初始化calldata映射字典（用于生成JSON）
    call_data_mapping = {
        "total_calls": 0,  # 总调用数
        "calls": []        # 每笔调用的详细映射
    }
    
    # PUML基础模板
    puml_lines = [
        "@startuml",
        "title CALL-Contract Sequence Diagram",
        "autonumber 0 0",
        "skinparam backgroundcolor #FFFFFF",
        # 合约样式：矩形
        "skinparam participant {",
        "    BorderColor black",
        "    BackgroundColor lightblue",
        "    FontSize 10",
        "    Shape rectangle",
        "}",
        # 交互样式
        "skinparam call {",
        "    BorderColor black",
        "    BackgroundColor lightgreen",
        "    FontSize 9",
        "}",
        "skinparam return {",
        "    BorderColor black",
        "    BackgroundColor lightyellow",
        "    FontSize 9",
        "}",
        # 深度分组box样式
        "skinparam box {",
        "    BorderColor black",
        "    FontSize 8",
        "    BackgroundColor #FFFFFF",
        "    Padding 8",
        "    Separator 5",
        "}",
        # note样式
        "skinparam note {",
        "    BackgroundColor #F0F8FF",
        "    BorderColor black",
        "    FontSize 8",
        "    Shape roundedbox",
        "}",
        "hide footbox"
    ]
    
    # 核心状态管理
    contract_call_seq = {}        # 地址→调用次数
    contract_instances = []       # 所有合约实例
    interaction_lines = []        # 所有交互行
    instance_id = 0               # 实例计数器
    DEPTH_BG_COLORS = [
        "#E0F7FF40",
        "#B3E5FC40",
        "#81D4FA40",
        "#4FC3F740",
        "#29B6F640",
        "#0288D140",
        "#0277BD40",
        "#01579B40"
    ]

    # -------------------------- 工具函数--------------------------
    def _get_contract_name(addr):
        """精准获取合约名称"""
        if not addr:
            return "UNKNOWN"
        addr_lower = addr.lower().strip()
        
        if addr_lower in full_address_name_map:
            return full_address_name_map[addr_lower]
        elif addr_lower in erc20_token_map:
            return erc20_token_map[addr_lower].get("name", f"ERC20_{addr_lower[:6]}")
        else:
            return f"{addr[:12]}..." if len(addr) > 12 else addr

    def _is_token_contract(addr):
        """判断是否为Token合约"""
        return addr and addr.lower().strip() in erc20_token_map

    def _get_contract_color(addr):
        """获取统一颜色"""
        addr_lower = addr.lower().strip()
        return addr_color_map.get(addr_lower, "#000000")

    def _create_contract_instance(addr, depth):
        """创建合约实例"""
        nonlocal instance_id
        addr = addr.strip() if addr else "UNKNOWN"
        addr_lower = addr.lower()
        
        if addr_lower not in contract_call_seq:
            contract_call_seq[addr_lower] = 0
        call_num = contract_call_seq[addr_lower]
        
        # 生成唯一别名
        addr_short = addr.replace("0x", "").replace(":", "_").replace(".", "_")[:16]
        alias = f"inst_{addr_short}_{call_num}"
        
        # 记录实例信息（新增：保留原始地址）
        instance = {
            "id": instance_id,
            "alias": alias,
            "address": addr,
            "address_short": addr[:12] + "..." if len(addr) > 12 else addr,
            "name": _get_contract_name(addr),
            "call_num": call_num,
            "depth": depth,
            "is_token": _is_token_contract(addr),
            "color": _get_contract_color(addr)
        }
        contract_instances.append(instance)
        
        contract_call_seq[addr_lower] += 1
        instance_id += 1
        
        return instance

    # -------------------------- 递归遍历调用树（核心修改：添加call_id显示） --------------------------
    def _traverse_call_tree(node, parent_instance=None, indent_level=0):
        """递归遍历所有深度的调用节点（新增call_id显示）"""
        current_addr = node.get("contract", "").strip()
        current_depth = node.get("depth", 0)
        entry_op = node.get("entry_op", "UNKNOWN").strip()
        exit_op = node.get("exit_op", "STOP").strip()
        segments = node.get("calldata_active_segments", [])  # 完整calldata列表

        # 创建当前合约实例
        current_instance = _create_contract_instance(current_addr, current_depth)
        indent = "    " * indent_level

        if parent_instance:
            # 1. 提取完整calldata数组（calldata0,1,2...）
            full_calldata = []
            for idx, seg in enumerate(segments):
                if seg and "val" in seg:
                    full_calldata.append(seg["val"])
                else:
                    full_calldata.append("无Calldata")
            calldata0 = full_calldata[0] if full_calldata else "无Calldata"

            # 2. 生成call_id（自增）
            call_data_mapping["total_calls"] += 1
            current_call_id = call_data_mapping["total_calls"]

            # 3. 生成PUML调用线
            op_type = entry_op.upper()
            call_title = f"[{current_call_id}] {entry_op}"  # 添加call_id序号
            if op_type in ["DELEGATECALL", "CALLCODE"]:
                call_line = f"{indent}{parent_instance['alias']} -[dashed]-> {current_instance['alias']}: {call_title}"
            else:
                call_line = f"{indent}{parent_instance['alias']} -> {current_instance['alias']}: {call_title}"
            interaction_lines.append(call_line)
            # 箭头下方显示calldata0（note）
            note_line = f"{indent}note on link: {calldata0}"
            interaction_lines.append(note_line)

            # 4. 记录到JSON映射（call_id和图中一致）
            call_data_mapping["calls"].append({
                "call_id": current_call_id,  # 和图中序号一一对应
                "from_address": parent_instance["address"],
                "from_name": parent_instance["name"],
                "to_address": current_instance["address"],
                "to_name": current_instance["name"],
                "op_type": entry_op,
                "calldata": full_calldata,  # 完整calldata列表
                "return_op": exit_op
            })

        # 递归处理子节点
        if "calls" in node and isinstance(node["calls"], list):
            for child_node in node["calls"]:
                _traverse_call_tree(child_node, current_instance, indent_level + 1)

        # 生成返回边
        if parent_instance:
            return_line = f"{indent}{current_instance['alias']} -> {parent_instance['alias']}: {exit_op}"
            interaction_lines.append(return_line)

    # -------------------------- 构建PUML --------------------------
    # 递归遍历调用树
    _traverse_call_tree(trace_tree)

    # 按深度分组生成合约实例
    depth_groups = {}
    for instance in contract_instances:
        depth = instance["depth"]
        if depth not in depth_groups:
            depth_groups[depth] = []
        depth_groups[depth].append(instance)

    # 生成各深度的box
    for depth in sorted(depth_groups.keys()):
        instances = depth_groups[depth]
        if not instances:
            continue
        
        # 深度背景色
        bg_color = DEPTH_BG_COLORS[depth] if depth < len(DEPTH_BG_COLORS) else DEPTH_BG_COLORS[-1]
        puml_lines.append(f'\nbox "Depth {depth}" {bg_color}')
        
        # 生成合约实例
        for instance in instances:
            display_name = f"{instance['name']}"
            puml_lines.append(f'    participant "{display_name}" as {instance["alias"]}')
            puml_lines.append(f'    skinparam participant::{instance["alias"]} {{ BackgroundColor {instance["color"]} }}')
        
        puml_lines.append("end box")
        puml_lines.append("")

    # 添加交互行
    puml_lines.append("\n' === 全深度调用交互序列 === '")
    interaction_lines.extend(interaction_lines)
    puml_lines.extend(interaction_lines)

    # 结束PUML
    puml_lines.append("\n@enduml")

    # -------------------------- 写入文件（保留原有） --------------------------
    # 1. 写入PUML文件
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(puml_lines))
    
    # 2. 写入JSON映射文件（和PUML同目录，同名不同后缀）
    json_file = output_file.replace(".puml", "_calldata_mapping.json")
    with open(json_file, "w", encoding="utf-8") as f:
        # 格式化输出，便于阅读
        json.dump(call_data_mapping, f, ensure_ascii=False, indent=4)

    # 输出统计信息
    print(f"✅ 时序图PUML已生成：{output_file}")
    print(f"✅ Calldata映射JSON已生成：{json_file}")
    print(f"📊 合约实例数：{len(contract_instances)} | 交互行数：{len(interaction_lines)} | 总调用数：{call_data_mapping['total_calls']}")
    
    return True