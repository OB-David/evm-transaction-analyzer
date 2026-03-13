import json
import os
import math

def build_refined_hierarchical_trace(steps):
    """
    构建 EVM 调用树，计算全局 Gas 进度
    """
    if not steps:
        return {}

    # --- 第一步：补全 depth ---
    processed_steps = []
    current_depth = 0
    # 扩展了操作码覆盖面以确保深度计算准确
    CALL_OPS = {"CALL", "DELEGATECALL", "STATICCALL", "CALLCODE", "CREATE", "CREATE2"}
    RETURN_OPS = {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}

    for i, step in enumerate(steps):
        s = step.copy()
        s["depth"] = current_depth
        processed_steps.append(s)
        
        op = s.get("opcode", s.get("op", ""))
        current_address = s.get("address", "")
        
        # 修正：通过索引 i+1 获取下一步，并检查是否越界
        next_address = None
        if i + 1 < len(steps):
            next_step = steps[i + 1]
            next_address = next_step.get("address", "")
        
        # 深度调整逻辑
        if op in CALL_OPS and current_address != next_address:
            current_depth += 1
        elif op in RETURN_OPS:
            current_depth -= 1
            if current_depth < 0: 
                current_depth = 0

    global_gas_counter = 0

    def process_level(start_index):
        nonlocal global_gas_counter
        start_step = processed_steps[start_index]
        
        # 获取触发当前层级的 Opcode
        last_step_op = "CALL"
        last_step_index = 0
        if start_index != 0:
            last_step_index = start_index - 1
            last_step = processed_steps[last_step_index]
            last_step_op = last_step.get("opcode", last_step.get("op", "CALL"))
            
            
        current_depth = start_step["depth"]
        
        # 节点定义：严格遵循你原始的 Gas 记录逻辑
        node = {
            "contract": start_step.get("address"),
            "op": last_step_op,
            "entry_gas": global_gas_counter,
            "entry_step": last_step_index,
            "calls": [],
            "exit_op": "UNKNOWN",
            "exit_gas": 0,
            "exit_step":0
        }

        i = start_index
        while i < len(processed_steps):
            step = processed_steps[i]
            depth = step["depth"]

            # 情况 A：进入子合约 (深度增加)
            if depth > current_depth:
                child_node, next_i = process_level(i)
                if child_node:
                    node["calls"].append(child_node)
                i = next_i
                continue

            # 情况 B：合约返回 (深度减少)
            if depth < current_depth:
                break

            # 情况 C：本层指令执行，累加原始代码中的 gascost
            # 注意：某些 trace 格式中 key 是 gasCost
            global_gas_counter += int(step.get("gascost", step.get("gasCost", 0)))
            i += 1

        # 记录该合约结束时的全局 Gas 进度
        last_step_idx = min(i - 1, len(processed_steps) - 1)
        last_step = processed_steps[last_step_idx]
        node["exit_op"] = last_step.get("opcode", last_step.get("op", "RETURN"))
        node["exit_gas"] = global_gas_counter
        node["exit_step"] = last_step_idx
        
        return node, i

    root_tree, _ = process_level(0)
    return root_tree


def generate_flame_graph_svg(tree_data, name_map, token_map, addr_color_map):
    """
    生成倒置火焰图（Icicle Graph），根合约在底部。
    颜色应用：高能火焰色阶（红->黄->蓝->白）。
    """
    elements = [] 
    lines = []
    
    # --- 配置参数 ---
    LAYER_HEIGHT = 50
    GAS_SCALE = 0.005  
    MIN_WIDTH = 4      
    CHAR_WIDTH = 7     

    # --- 1. 计算最大深度以进行 Y 轴翻转 ---
    def get_max_depth(node, current_depth):
        if not node or not node.get("calls"):
            return current_depth
        return max([get_max_depth(child, current_depth + 1) for child in node["calls"]])

    max_depth = get_max_depth(tree_data, 0)

    # --- 2. 内部递归遍历函数 ---
    def traverse(node, depth=0):
        if not node: return
        
        addr_l = node.get("contract", "unknown").lower()
        entry_gas = node["entry_gas"]
        exit_gas = node["exit_gas"]
        entry_step = node["entry_step"]
        exit_step = node["exit_step"]
        
        # 计算 X 坐标和宽度
        entry_x = entry_gas * GAS_SCALE
        exit_x = exit_gas * GAS_SCALE
        width = max(exit_x - entry_x, MIN_WIDTH)
        height = LAYER_HEIGHT - 15
        
        # 计算翻转后的 Y 坐标：根合约 (depth=0) 在最下面
        # Y = (总深度 - 当前深度) * 层高
        flipped_y = (max_depth - depth) * LAYER_HEIGHT
        
        # 合约类型与显示名称
        is_token = addr_l in [a.lower() for a in token_map.keys()]
        base_name = name_map.get(addr_l, addr_l[:10])
        show_text = width > (len(base_name) * CHAR_WIDTH)

        # 悬浮提示信息
        tooltip_text = (
            f"Name: {base_name}\n"
            f"Address: {addr_l}\n"
            f"Type: {'ERC20/Token' if is_token else 'Standard Contract'}\n"
            f"Step Range: {entry_step} -- {exit_step}\n"
            f"Gas Consumed: {exit_gas - entry_gas}\n"
            f"Depth: {depth}"
        )
        
        # 存储形状数据
        elements.append({
            "type": "token" if is_token else "contract",
            "x": entry_x,
            "y": flipped_y,
            "w": width,
            "h": height,
            "color": addr_color_map.get(addr_l, "#D3D3D3"), # 此时已应用 FLAME_BLUE_WHITE_OUTER
            "name": base_name,
            "show_text": show_text,
            "tooltip": tooltip_text
        })

        # 处理子调用连接线
        for child in node.get("calls", []):
            child_x = child["entry_gas"] * GAS_SCALE
            is_delegate = "DELEGATECALL" in child.get("op", "").upper()
            
            # 子合约的 Y 坐标（在父合约上方）
            child_y_flipped = (max_depth - (depth + 1)) * LAYER_HEIGHT
            
            lines.append({
                "x1": child_x,
                "y1": flipped_y, # 从父合约顶部开始
                "x2": child_x,
                "y2": child_y_flipped + height, # 连向子合约底部
                "dashed": is_delegate
            })
            traverse(child, depth + 1)

    # 开始遍历
    traverse(tree_data)

    # --- 3. 构建 SVG 字符串 ---
    total_width = tree_data["exit_gas"] * GAS_SCALE + 100
    total_height = (max_depth + 1) * LAYER_HEIGHT + 20

    svg = [f'<svg width="{total_width}" height="{total_height}" xmlns="http://www.w3.org/2000/svg">']
    # 样式：增加了一些阴影和悬浮效果使火焰感更强
    svg.append('''<style>
        rect, ellipse { stroke-width: 1; transition: all 0.1s; } 
        rect:hover, ellipse:hover { stroke-width: 2; stroke: #000; filter: drop-shadow(0 0 5px rgba(255,165,0,0.5)); }
    </style>''')
    
    # 绘制连接线
    for l in lines:
        dash = 'stroke-dasharray="4"' if l['dashed'] else ''
        svg.append(f'<line x1="{l["x1"]}" y1="{l["y1"]}" x2="{l["x2"]}" y2="{l["y2"]}" stroke="#888" stroke-width="1" {dash} />')
    
    # 绘制形状与文字
    for e in elements:
        if e['type'] == "token":
            cx, cy = e['x'] + (e['w'] / 2), e['y'] + (e['h'] / 2)
            rx, ry = e['w'] / 2, e['h'] / 2
            shape_tag = f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="{e["color"]}" stroke="#444" />'
            text_tag = f'<text x="{cx}" y="{cy+4}" font-family="Verdana" font-size="10" fill="black" text-anchor="middle" pointer-events="none">{e["name"]}</text>'
        else:
            shape_tag = f'<rect x="{e["x"]}" y="{e["y"]}" width="{e["w"]}" height="{e["h"]}" fill="{e["color"]}" stroke="#555" rx="2" />'
            text_tag = f'<text x="{e["x"]+5}" y="{e["y"]+22}" font-family="Verdana" font-size="10" fill="black" pointer-events="none">{e["name"]}</text>'

        svg.append(f'''
        <g>
            {shape_tag}
            {text_tag if e['show_text'] else ""}
            <title>{e['tooltip']}</title>
        </g>''')
            
    svg.append('</svg>')
    return "".join(svg)

def export_visual_trace(result_dir, tree_data, name_map, token_map, addr_color_map):
    if not tree_data: return
    # 导出火焰图 SVG
    svg_code = generate_flame_graph_svg(tree_data, name_map, token_map, addr_color_map)
    file_path = os.path.join(result_dir, "trace_flame_graph.svg")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(svg_code)
    print(f"Flame Graph exported to: {file_path}")