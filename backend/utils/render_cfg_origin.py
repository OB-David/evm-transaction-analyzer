# render_cfg.py
# 仅负责CFG DOT文件生成，无任何图例相关代码/依赖/调用
from typing import Any, Optional, List, Dict, Tuple

def escape_dot(s: Any) -> str:
    """转义DOT特殊字符"""
    if s is None or s == "" or str(s) == "Unknown":
        return "Unknown"
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s.replace('"', '\\"').replace("|", "\\|").replace("{", "\\{").replace("}", "\\}")

def addr_short(s: Any) -> str:
    """缩短以太坊地址"""
    s = str(s)
    return s[:8] + "..." + s[-4:] if s.startswith("0x") and len(s) > 8 else s

def extract_edge_seq(edge_id: Optional[str]) -> str:
    """提取边的序号"""
    if not edge_id or not str(edge_id).startswith("edge_"):
        return "0"
    parts = str(edge_id).split("_")
    return parts[1] if len(parts)>=2 and parts[1].isdigit() else "0"

def get_valid_nodes_and_colors(cfg: object, contract_colors: List[str]) -> Tuple[List[object], List[str], List[str], Dict[str, int]]:
    """
    按合约第一次出现顺序依次分配颜色
    """
    valid_nodes = []
    node_colors = []
    node_contract_addrs = []
    addr_color_map = {}

    # 记录合约地址→颜色索引（按首次出现顺序）
    contract_to_color_idx = {}
    color_index = 0

    for node in cfg.nodes:
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        if not (is_fold_root or not is_folded):
            continue
            
        node_addr = str(getattr(node, "address", "Unknown")).strip()
        
        # 同一个合约永远同一种颜色
        if node_addr not in contract_to_color_idx:
            contract_to_color_idx[node_addr] = color_index
            color_index += 1

        # 按顺序取色，超过长度循环
        cidx = contract_to_color_idx[node_addr] % len(contract_colors)
        color = contract_colors[cidx]

        valid_nodes.append(node)
        node_colors.append(color)
        node_contract_addrs.append(node_addr)
        addr_color_map[node_addr] = color

    return valid_nodes, node_colors, addr_color_map

def render_transaction(contract_colors: List[str], edge_color_map: Dict[str, str], cfg: object, output_path: str, full_address_name_map: Dict[str, str], erc20_token_map: Dict[str, Any], rankdir: str = "LR") -> None:
    """
    仅生成CFG DOT文件
    :param cfg: 包含nodes/edges的CFG对象
    :param output_path: DOT文件输出路径
    :param full_address_name_map: 地址→名称映射
    :param erc20_token_map: ERC20合约地址→名称映射
    :param rankdir: 图表方向 LR=左到右
    """
    if not hasattr(cfg, 'nodes') or not hasattr(cfg, 'edges'):
        raise TypeError(f"cfg必须包含nodes/edges属性")

    # 获取有效节点、颜色、合约地址映射
    valid_nodes, node_colors, addr_color_map = get_valid_nodes_and_colors(cfg, contract_colors)

    # 预处理地址名称映射
    full_name_map_lower = {addr.lower(): name for addr, name in full_address_name_map.items()}

    # 提取ERC20合约地址
    erc20_addrs = [
        addr for addr, val in erc20_token_map.items()
    ]

    # ====================== 竖向加宽布局（核心修改）======================
    dot_lines = [
        "digraph CFG {",
        f"  rankdir={rankdir};",
        # 节点：增大纵向内边距，让节点自身更高
        '  node [fontname="Arial", fontsize=7, color=black, style=filled, margin=0.08, width=0, height=0.8, fontmargin=0.02];',
        # 边：拉长纵向长度，节点间纵向距离更大
        '  edge [fontname="Arial", fontsize=4, len=0.01, labelfontsize=4, labelmargin=0.02, penwidth=1];',
        # 图表：大幅增加纵向行间距，取消纵向最大限制
        '  graph [nodesep=0.05, ranksep=0.1, charset="utf-8", maxiter=200000, dpi=96, ratio=auto, overlap=false, splines=polyline];',
    ]
    # ====================================================================

    rendered_node_ids = set()
    
    # 生成节点（文字内容完全保留，仅调整布局参数）
    for idx, node in enumerate(valid_nodes):
        node_id = f"node_{node.id}"
        rendered_node_ids.add(node_id)
        node_addr_original = str(getattr(node, "address", "Unknown")).strip()
        node_addr_lower = node_addr_original.lower()
        
        # 获取合约名称（完全保留原文字）
        contract_name = full_name_map_lower.get(node_addr_lower, "Unknown")
        contract_name_escaped = escape_dot(contract_name)
        
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        color = node_colors[idx]

        # 判断节点形状（椭圆=ERC20，矩形=普通合约）
        node_shape = "ellipse" if node_addr_lower in erc20_addrs else "record"

        # 获取Gas值（完全保留原数值）
        if is_fold_root and hasattr(node, "fold_info"):
            gas = node.fold_info.get("total_gas", 0)
        else:
            gas = getattr(node, "total_gas", 0)

        # 判断是否有Action（用于红色粗边框）
        actions = node.fold_info.get("actions", []) if (is_fold_root or (not is_folded) and hasattr(node, "fold_info")) else []
        has_action = len(actions) > 0

        # ERC20节点（椭圆）- 仅调整布局，文字完全保留
        if node_shape == "ellipse":
            block_id = node.id
            blocks_num = escape_dot(node.fold_info.get('blocks_number', 1) if is_fold_root else 1)
            start_pc = escape_dot(node.start_pc)
            end_pc = escape_dot(node.fold_info.get('end_pc', node.end_pc if hasattr(node, 'end_pc') else '0x0'))
            gas_str = f"{gas:.2f}"
            
            #  有action情况
            if has_action: 
                # 处理Action文本（完全保留原文字）
                action_text = []
                act_idx = 1
                for act in actions:
                    if "eth_event" in act and act["eth_event"]:
                        eth_item = act["eth_event"]
                        from_addr = eth_item['from'].lower() if isinstance(eth_item['from'], str) else str(eth_item['from']).lower()
                        from_name = full_name_map_lower.get(from_addr, addr_short(from_addr))
                        to_addr = eth_item['to'].lower() if isinstance(eth_item['to'], str) else str(eth_item['to']).lower()
                        to_name = full_name_map_lower.get(to_addr, addr_short(to_addr))
                        action_text.append(f"Action{act_idx}: Send_ETH {from_name}→{to_name} {eth_item['amount']}")
                        act_idx += 1
                    for erc in act.get("erc20_events", []):
                        user_addr = erc['user'].lower() if isinstance(erc['user'], str) else str(erc['user']).lower()
                        user_name = full_name_map_lower.get(user_addr, addr_short(user_addr))
                        action_text.append(f"Action{act_idx}:  {erc['type']} {user_name} {erc['balance']}")
                        act_idx += 1
                actions_str = "\\n".join(action_text)

                # 节点标签（文字完全保留）
                label_text = (
                    f"ID: {block_id} \\n"
                    f"{contract_name_escaped}\\n"
                    f"Blocks: {blocks_num}\\n"
                    f"StartPC: {start_pc} | EndPC: {end_pc}\\n"
                    f"Gas: {gas_str}\\n"
                    f"{actions_str}"
                )
                label_text_escaped = escape_dot(label_text)

                # 节点属性（仅布局参数）
                style_str = "filled, shadow" + (", bold" if has_action else "")
                node_attrs = [
                    f'shape="{node_shape}"',
                    f'label="{label_text_escaped}"',
                    f'style="{style_str}"',
                    f'fillcolor="{color}"',
                    f'color="{"red" if has_action else "black"}"',
                    f'margin=0.08',  # 增大节点内边距（纵向）
                    f'fontmargin=0.02',  # 文字与边框间距
                    f'height=0.8',  # 强制节点最小高度
                    f'penwidth=2'
                ]

            # 无action情况
            else:
                # 节点标签（文字完全保留）
                label_text = (
                    f"ID: {block_id} \\n"
                    f"{contract_name_escaped}\\n"
                    f"Blocks: {blocks_num}\\n"
                    f"StartPC: {start_pc} | EndPC: {end_pc}\\n"
                    f"Gas: {gas_str}"
                )
                label_text_escaped = escape_dot(label_text)

                # 节点属性（仅布局参数）
                style_str = "filled, shadow" + (", bold" if has_action else "")
                node_attrs = [
                    f'shape="{node_shape}"',
                    f'label="{label_text_escaped}"',
                    f'style="{style_str}"',
                    f'fillcolor="{color}"',
                    f'color="{"red" if has_action else "black"}"',
                    f'margin=0.08',
                    f'fontmargin=0.02',
                    f'height=0.8'
                ]
            dot_lines.append(f"  {node_id} [{', '.join(node_attrs)}];")


        # 普通合约节点（矩形）- 仅调整布局，文字完全保留
        else:
            # 有action情况
            if has_action:

                # 处理Action文本（完全保留原文字）
                action_text = []
                act_idx = 1
                for act in actions:
                    if "eth_event" in act and act["eth_event"]:
                        eth_item = act["eth_event"]
                        from_addr = eth_item['from'].lower() if isinstance(eth_item['from'], str) else str(eth_item['from']).lower()
                        from_name = full_name_map_lower.get(from_addr, addr_short(from_addr))
                        to_addr = eth_item['to'].lower() if isinstance(eth_item['to'], str) else str(eth_item['to']).lower()
                        to_name = full_name_map_lower.get(to_addr, addr_short(to_addr))
                        action_text.append(f"Action{act_idx}: Send_ETH {from_name} → {to_name} {eth_item['amount']}")
                        act_idx += 1
                actions_joined = '\\n'.join(action_text) if action_text else 'No actions'
                semantic_table = [
                    f"{{ID: {node.id} | {contract_name_escaped} | Blocks: {escape_dot(node.fold_info.get('blocks_number', 1) if is_fold_root else 1)} | StartPC: {escape_dot(node.start_pc)} | EndPC: {escape_dot(node.fold_info.get('end_pc', node.end_pc if hasattr(node, 'end_pc') else '0x0'))} | Gas: {escape_dot(gas)} | {actions_joined}}}"
                    ]
                label_semantic = "|".join(semantic_table)

                # 节点属性（仅布局参数）
                style_str = "filled" + (", bold" if has_action else "")
                node_attrs = [
                    f"shape=\"{node_shape}\"",
                    f"label=\"{{{label_semantic}}}\"",
                    f"style=\"{style_str}\"",
                    f"fillcolor=\"{color}\"",
                    f"color=\"{'red' if has_action else 'black'}\"",
                    f"margin=0.08",  # 增大节点内边距（纵向）
                    f'fontmargin=0.02',  # 文字与边框间距
                    f'height=0.8',  # 强制节点最小高度
                    f'penwidth=2'
                ]
                
            # 无action情况
            else: 
                semantic_table = [
                    f"{{ID: {node.id} | {contract_name_escaped} | Blocks: {escape_dot(node.fold_info.get('blocks_number', 1) if is_fold_root else 1)}  | StartPC: {escape_dot(node.start_pc)} | EndPC: {escape_dot(node.fold_info.get('end_pc', node.end_pc if hasattr(node, 'end_pc') else '0x0'))} | Gas: {escape_dot(gas)}}}"
                ]
                
                # 节点属性（仅布局参数）
                style_str = "filled" + (", bold" if has_action else "")
                node_attrs = [
                    f"shape=\"{node_shape}\"",
                    f"label=\"{{{'|'.join(semantic_table)}}}\"",
                    f"style=\"{style_str}\"",
                    f"fillcolor=\"{color}\"",
                    f"color=\"{'red' if has_action else 'black'}\"",
                    f"margin=0.08",
                    f'fontmargin=0.02',
                    f'height=0.8'
                ]

            dot_lines.append(f"  {node_id} [{', '.join(node_attrs)}];")

    # 生成边（仅调整布局，标签文字完全保留）
    for edge in getattr(cfg, 'edges', []):
        if not (hasattr(edge, 'source') and hasattr(edge, 'target')):
            continue
        src_id = f"node_{edge.source.id}"
        tgt_id = f"node_{edge.target.id}"
        if src_id not in rendered_node_ids or tgt_id not in rendered_node_ids:
            continue

        edge_seq = getattr(edge, "merged_ids", extract_edge_seq(getattr(edge, "edge_id", "")))
        edge_type = escape_dot(getattr(edge, 'edge_type', 'UNKNOWN'))
        edge_color = edge_color_map.get(edge_type, "#607D8B")
        
        # 边属性（仅布局参数，文字保留）
        dot_lines.append(f"  {src_id} -> {tgt_id} [label=\"{edge_seq}\", color=\"{edge_color}\", style=\"solid\", fontsize=2, len=0.5, labelmargin=0.015];")

    dot_lines.append("}")

    # 写入DOT文件
    final_output_path = f"{output_path}.dot" if not output_path.endswith(".dot") else output_path
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(dot_lines))

    print(f"[OK] CFG DOT文件已生成：{final_output_path}")
    return addr_color_map