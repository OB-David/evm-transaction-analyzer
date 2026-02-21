# render_cfg.py
# 仅负责CFG DOT文件生成，无任何图例相关代码/依赖/调用
from typing import Any, Optional, List, Dict, Tuple
import math
import hashlib

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
    按【合约第一次出现顺序】依次分配颜色，不哈希、不重复
    返回：有效节点列表、节点颜色列表、节点合约地址列表、合约地址→颜色索引映射
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

    return valid_nodes, node_colors, node_contract_addrs, contract_to_color_idx, addr_color_map

def render_transaction(cfg: object, output_path: str, full_address_name_map: Dict[str, str], rankdir: str = "TB") -> None:
    """
    仅生成CFG DOT文件，无图例相关操作
    :param cfg: 包含nodes/edges的CFG对象
    :param output_path: DOT文件输出路径
    :param full_address_name_map: 地址→名称映射
    :param rankdir: 图表方向（TB/RL等）
    """
    if not hasattr(cfg, 'nodes') or not hasattr(cfg, 'edges'):
        raise TypeError(f"cfg必须包含nodes/edges属性")

    # 固定配色（仅用于CFG节点颜色）
    contract_colors = [
        "#FF9E9E", "#81C784", "#64B5F6", "#FFF176", "#BA68C8",
        "#4DD0E1", "#FFB74D", "#F48FB1", "#AED581", "#7986CB"
    ]
    edge_color_map = {
        "NORMAL": "#939393",
        "JUMP": "#575757",
        "CALL": "#0DFF00",
        "TERMINATE": "#FF5100",
    }

    # 获取有效节点、颜色、合约地址映射
    valid_nodes, node_colors, node_contract_addrs, contract_to_color_idx, addr_color_map = get_valid_nodes_and_colors(cfg, contract_colors)
    if not valid_nodes:
        print("警告：无有效节点可渲染")
        return

    # 预处理地址名称映射（转小写）
    full_name_map_lower = {addr.lower(): name for addr, name in full_address_name_map.items()}
    # 提取ERC20合约地址（名称不是contract_xxx/User_xxx）
    erc20_addrs_lower = [
        addr for addr, name in full_name_map_lower.items()
        if not (name.startswith("contract_") or name.startswith("User_"))
    ]

    # 初始化DOT文件内容
    dot_lines = [
        "digraph CFG {",
        f"  rankdir={rankdir};",
        # 节点参数：紧凑布局
        '  node [fontname="Arial", fontsize=7, color=black, style=filled, margin=0.1];',
        # 边参数：小字体
        '  edge [fontname="Arial", fontsize=4];',
        # 图表参数：紧凑布局
        '  graph [nodesep=0.3, ranksep=0.3, charset="utf-8", maxiter=100000, dpi=96, ratio=compress];',
    ]

    rendered_node_ids = set()
    
    # 生成节点
    for idx, node in enumerate(valid_nodes):
        node_id = f"node_{node.id}"
        rendered_node_ids.add(node_id)
        node_addr_original = str(getattr(node, "address", "Unknown")).strip()
        node_addr_lower = node_addr_original.lower()
        
        # 获取合约名称
        contract_name = full_name_map_lower.get(node_addr_lower, "Unknown")
        contract_name_escaped = escape_dot(contract_name)
        
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        color = node_colors[idx]

        # 判断节点形状（椭圆=ERC20，矩形=普通合约）
        node_shape = "ellipse" if node_addr_lower in erc20_addrs_lower else "record"

        # 获取Gas值
        if is_fold_root and hasattr(node, "fold_info"):
            gas = node.fold_info.get("total_gas", 0)
        else:
            gas = getattr(node, "total_gas", 0)

        # 判断是否有Action（用于红色粗边框）
        actions = node.fold_info.get("actions", []) if (is_fold_root or (not is_folded) and hasattr(node, "fold_info")) else []
        has_action = len(actions) > 0

        if node_shape == "ellipse":
            # ERC20节点（椭圆）
            block_id = node.id
            blocks_num = escape_dot(node.fold_info.get('blocks_number', 1) if is_fold_root else 1)
            start_pc = escape_dot(node.start_pc)
            end_pc = escape_dot(node.fold_info.get('end_pc', node.end_pc if hasattr(node, 'end_pc') else '0x0'))
            gas_str = f"{gas:.2f}"
            
            # 处理Action文本
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
            actions_str = "\\n".join(action_text) if action_text else "No actions"

            # 节点标签
            label_text = (
                f"ID: {block_id} | {contract_name_escaped} | Blocks: {blocks_num}\\n"
                f"StartPC: {start_pc} | EndPC: {end_pc} | Gas: {gas_str}"
                f"\\n {actions_str}"
            )
            label_text_escaped = escape_dot(label_text)

            # 节点属性（有Action则红色粗边框）
            style_str = "filled, shadow" + (", bold" if has_action else "")
            node_attrs = [
                f'shape="{node_shape}"',
                f'label="{label_text_escaped}"',
                f'style="{style_str}"',
                f'fillcolor="{color}"',
                f'color="{"red" if has_action else "black"}"',
                f'width=0',
                f'height=0',
                f'margin=0.1'
            ]
            if has_action:
                node_attrs.append(f'penwidth=2')
            dot_lines.append(f"  {node_id} [{', '.join(node_attrs)}];")
        else:
            # 普通合约节点（矩形）
            semantic_table = [
                f"{{ID: {node.id} | {contract_name_escaped} | Blocks: {escape_dot(node.fold_info.get('blocks_number', 1) if is_fold_root else 1)} }}",
                f"{{StartPC: {escape_dot(node.start_pc)} | EndPC: {escape_dot(node.fold_info.get('end_pc', node.end_pc if hasattr(node, 'end_pc') else '0x0'))} | Gas: {escape_dot(gas)}}}",
                "{ }"
            ]

            # 处理Action文本
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
            semantic_table[2] = f"{{ {actions_joined} }}"
            label_semantic = "|".join(semantic_table)

            # 节点属性
            style_str = "filled" + (", bold" if has_action else "")
            node_attrs = [
                f"shape=\"{node_shape}\"",
                f"label=\"{{{label_semantic}}}\"",
                f"style=\"{style_str}\"",
                f"fillcolor=\"{color}\"",
                f"color=\"{'red' if has_action else 'black'}\"",
                f"margin=0.1"
            ]
            if has_action:
                node_attrs.append(f'penwidth=2')
            dot_lines.append(f"  {node_id} [{', '.join(node_attrs)}];")

    # 生成边
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
        dot_lines.append(f"  {src_id} -> {tgt_id} [label=\"{edge_seq}\", color=\"{edge_color}\", style=\"solid\", labelfloat=true, fontsize=4];")

    dot_lines.append("}")

    # 写入DOT文件
    final_output_path = f"{output_path}.dot" if not output_path.endswith(".dot") else output_path
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(dot_lines))

    print(f"✅ CFG DOT文件已生成：{final_output_path}")
    return addr_color_map