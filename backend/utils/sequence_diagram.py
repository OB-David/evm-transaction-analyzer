import json

def build_refined_hierarchical_trace(steps):
    """
    严谨断点切片版 (手术刀逻辑):
    1. 保持原代码的递归结构和严谨退出逻辑。
    2. Primary Breakpoints (首断点): CALLDATALOAD 传入的 offset。
    3. Hidden Breakpoints (隐藏断点): 每个首断点 + 32 字节。
    4. 自动截断: 段终点 = min(下一个首断点, 当前断点+32, 数据总长)。
    """
    if not steps:
        return {}

    # --- 第一步：深度重构 (保持原结构) ---
    processed_steps = []
    current_depth = 0
    CALL_OPS = {"CALL", "DELEGATECALL", "STATICCALL", "CALLCODE", "CREATE", "CREATE2"}
    RETURN_OPS = {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}

    for i, step in enumerate(steps):
        s = step.copy()
        s["depth"] = current_depth
        processed_steps.append(s)
        
        op = s.get("opcode", s.get("op", ""))
        current_address = s.get("address", "")
        next_address = steps[i+1].get("address", "") if i + 1 < len(steps) else None
        
        if op in CALL_OPS and current_address != next_address:
            current_depth += 1
        elif op in RETURN_OPS:
            current_depth -= 1
            if current_depth < 0: current_depth = 0

    global_gas_counter = 0

    def process_level(start_index):
        nonlocal global_gas_counter
        start_step = processed_steps[start_index]
        
        last_step_op = "OUTSIDECALL"
        raw_calldata = ""
        # 记录首断点
        primary_breakpoints = set()

        # --- 1. 提取原始数据 (保持原结构) ---
        if start_index != 0:
            prev_step = processed_steps[start_index - 1]
            last_step_op = prev_step.get("opcode", prev_step.get("op", "OUTSIDECALL"))
            stack = prev_step.get("stack", [])
            memory = prev_step.get("memory", [])
            full_mem = "".join([m for m in memory])
            
            try:
                if last_step_op in ["CALL", "CALLCODE"]:
                    offset, size = int(stack[-4], 16), int(stack[-5], 16)
                elif last_step_op in ["DELEGATECALL", "STATICCALL"]:
                    offset, size = int(stack[-3], 16), int(stack[-4], 16)
                elif last_step_op in ["CREATE", "CREATE2"]:
                    offset, size = int(stack[-2], 16), int(stack[-3], 16)
                else: offset, size = 0, 0
                
                if size > 0:
                    raw_calldata = full_mem[offset*2 : (offset+size)*2]
            except: pass

        num_bytes = len(raw_calldata) // 2
        current_level_depth = start_step["depth"]
        
        node = {
            "contract": start_step.get("address"),
            "entry_op": last_step_op,
            "calldata_raw": "0x" + raw_calldata if raw_calldata else "",
            "calldata_active_segments": [],
            "calls": [],
            "exit_op": "PENDING", 
            "exit_gas": 0
        }

        # --- 2. 遍历执行流并收集首断点 (保持原结构) ---
        i = start_index
        while i < len(processed_steps):
            step = processed_steps[i]
            if step["depth"] > current_level_depth:
                child_node, next_i = process_level(i)
                node["calls"].append(child_node)
                i = next_i
                continue
            
            if step["depth"] < current_level_depth: break

            op = step.get("opcode", step.get("op", ""))
            stk = step.get("stack", [])
            
            if op in RETURN_OPS:
                node["exit_op"] = op

            if op == "CALLDATALOAD" and len(stk) > 0:
                try:
                    off = int(stk[-1], 16)
                    if off < num_bytes:
                        primary_breakpoints.add(off)
                except: pass

            global_gas_counter += int(step.get("gascost", step.get("gasCost", 0)))
            i += 1

        # --- 3. 核心改进：智能切片逻辑 ---
        if raw_calldata:
            # 强制将 0 加入首断点（确保选择器等起始位被识别）
            primary_breakpoints.add(0)
            sorted_p = sorted(list(primary_breakpoints))
            segments = []
            
            for idx, p_start in enumerate(sorted_p):
                # 定义“隐藏断点”：即当前首断点读取的 32 字节边界
                hidden_end = p_start + 32
                
                # 定义“逻辑终点”：
                # 1. 如果有下一个首断点，则不能超过它
                # 2. 不能超过自己的隐藏断点（32字节）
                # 3. 不能超过数据总长
                if idx + 1 < len(sorted_p):
                    next_p = sorted_p[idx + 1]
                    actual_end = min(next_p, hidden_end, num_bytes)
                else:
                    actual_end = min(hidden_end, num_bytes)
                
                # 只有当区间有效时才截取（处理重叠或末尾情况）
                if p_start < actual_end:
                    seg_hex = raw_calldata[p_start*2 : actual_end*2]
                    segments.append({
                        "count": idx,
                        "val": f"0x{seg_hex}"
                    })
            
            node["calldata_active_segments"] = segments

        # 补全退出状态
        node["exit_gas"] = global_gas_counter
        if node["exit_op"] == "PENDING": node["exit_op"] = "STOP"

        return node, i

    # 从根节点开始构建
    root_tree, _ = process_level(0)
    return root_tree