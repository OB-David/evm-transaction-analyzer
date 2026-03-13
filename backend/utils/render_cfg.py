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

    dot_lines = [
            "digraph CFG {",
            f"  rankdir={rankdir};",
            # 全局图表设置：极小的节点间距(nodesep)和层级间距(ranksep)
            '  graph [nodesep=0.6, ranksep=0, charset="utf-8", splines=polyline, overlap=false];',
            # 节点设置：去掉强制高度(height)，压缩内边距(margin)
            '  node [fontname="Arial", fontsize=128, shape=rect, style=filled, margin="0.618,1", width=0, height=0];',
            # 边设置：减小字体，简化箭头
            '  edge [fontname="Arial", fontsize=100, arrowsize=1, penwidth=15];',
        ]

    rendered_node_ids = set()
    
    # 生成节点（文字内容完全保留，仅调整布局参数）
    for idx, node in enumerate(valid_nodes):
        node_id = f"node_{node.id}"
        rendered_node_ids.add(node_id)
        node_addr_original = str(getattr(node, "address", "Unknown")).strip()
        node_addr_lower = node_addr_original.lower()
        
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        color = node_colors[idx]

        # 判断节点形状（椭圆=ERC20，矩形=普通合约）
        node_shape = "ellipse" if node_addr_lower in erc20_addrs else "record"

        # 判断是否有Action（用于红色粗边框）
        actions = node.fold_info.get("actions", []) if (is_fold_root or (not is_folded) and hasattr(node, "fold_info")) else []
        has_action = len(actions) > 0

        # 节点属性（仅布局参数）
        style_str = "filled, shadow" + (", bold" if has_action else "")
        current_penwidth = 40 if has_action else 10
        node_attrs = [
            f'shape="{node_shape}"',
            f'label="{node.id}"',
            f'style="{style_str}"',
            f'fillcolor="{color}"',
            f'color="{"red" if has_action else "black"}"',
            f'penwidth = {current_penwidth}'
            f'fontmargin=0.02',  # 文字与边框间距'
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

        edge_seq = edge.edge_id
        edge_type = escape_dot(getattr(edge, 'edge_type', 'UNKNOWN'))
        edge_color = edge_color_map.get(edge_type, "#607D8B")
        edge_step = edge.edge_step
        
        # 边属性（仅布局参数，文字保留）
        dot_lines.append(f'  {src_id} -> {tgt_id} [label="{edge_seq}", color="{edge_color}", style="solid",minlen=1], comment="{edge_step}"')
    dot_lines.append("}")

    # 写入DOT文件
    final_output_path = f"{output_path}.dot" if not output_path.endswith(".dot") else output_path
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(dot_lines))

    print(f"[OK] CFG DOT文件已生成：{final_output_path}")
    return addr_color_map