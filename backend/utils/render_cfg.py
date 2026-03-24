# render_cfg.py
# 仅负责CFG DOT文件生成，无任何图例相关代码/依赖/调用
from typing import Any, List, Dict, Tuple

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

def get_valid_nodes_and_colors(cfg: object, contract_colors: List[str]) -> Tuple[List[object], List[str], Dict[str, str]]:
    """
    按合约第一次出现顺序依次分配颜色
    """
    valid_nodes = []
    node_colors = []
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
            # 全局设置：spline曲线边、compound支持跨cluster边、newrank改善排列
            '  graph [nodesep=0.62, ranksep=1.05, pad=0.18, charset="utf-8", splines=spline, compound=true, newrank=true, overlap=false];',
            # 节点设置：fixedsize确保空label节点有固定可见尺寸
            '  node [fontname="Arial", fontsize=128, shape=rect, style=filled, fixedsize=true, width=2.55, height=1.62];',
            # 边设置：简化箭头
            '  edge [fontname="Arial", fontsize=100, arrowsize=1, penwidth=15];',
        ]

    rendered_node_ids = set()

    # 按合约地址分组节点
    from collections import OrderedDict
    contract_nodes: OrderedDict[str, list] = OrderedDict()

    for idx, node in enumerate(valid_nodes):
        node_addr = str(getattr(node, "address", "Unknown")).strip()
        if node_addr not in contract_nodes:
            contract_nodes[node_addr] = []
        contract_nodes[node_addr].append((idx, node))

    # 生成按合约分组的 subgraph cluster
    for cluster_idx, (contract_addr, nodes_in_contract) in enumerate(contract_nodes.items()):
        contract_addr_lower = contract_addr.lower()

        # 获取合约名称
        contract_name = full_name_map_lower.get(contract_addr_lower, addr_short(contract_addr))
        contract_name = escape_dot(contract_name)

        # 获取合约颜色（从第一个节点的颜色取）
        first_idx = nodes_in_contract[0][0]
        cluster_color = node_colors[first_idx]

        # cluster 背景色：在原色基础上降低透明度
        bg_color = cluster_color[:7] + "20" if len(cluster_color) >= 7 else cluster_color

        dot_lines.append(f'  subgraph cluster_{cluster_idx} {{')
        dot_lines.append(f'    label="{contract_name}";')
        dot_lines.append(f'    fontname="Arial";')
        dot_lines.append(f'    fontsize=80;')
        dot_lines.append(f'    style=filled;')
        dot_lines.append(f'    color="{cluster_color[:7]}80";')
        dot_lines.append(f'    fillcolor="{bg_color}";')
        dot_lines.append(f'    margin=26;')

        for idx, node in nodes_in_contract:
            node_id = f"node_{node.id}"
            rendered_node_ids.add(node_id)
            color = node_colors[idx]

            # 判断节点形状（椭圆=ERC20，矩形=普通合约）
            node_shape = "ellipse" if contract_addr_lower in erc20_addrs else "rect"

            # 判断是否有Action（用于红色粗边框）
            is_fold_root = getattr(node, "is_fold_root", False)
            is_folded = getattr(node, "folded", False)
            actions = node.fold_info.get("actions", []) if (is_fold_root or (not is_folded) and hasattr(node, "fold_info")) else []
            has_action = len(actions) > 0

            style_str = "filled" + (", bold" if has_action else "")
            current_penwidth = 40 if has_action else 10
            node_attrs = [
                f'shape="{node_shape}"',
                f'label=""',
                f'style="{style_str}"',
                f'fillcolor="{color}"',
                f'color="{"red" if has_action else "black"}"',
                f'penwidth={current_penwidth}',
            ]
            dot_lines.append(f"    {node_id} [{', '.join(node_attrs)}];")

        dot_lines.append("  }")

    # 生成边（去重：同向相同边合并为一条，无标签）
    edge_dedup = {}  # key: (src_id, tgt_id), value: edge_color
    for edge in getattr(cfg, 'edges', []):
        if not (hasattr(edge, 'source') and hasattr(edge, 'target')):
            continue
        src_id = f"node_{edge.source.id}"
        tgt_id = f"node_{edge.target.id}"
        if src_id not in rendered_node_ids or tgt_id not in rendered_node_ids:
            continue

        pair_key = (src_id, tgt_id)
        if pair_key not in edge_dedup:
            edge_type = escape_dot(getattr(edge, 'edge_type', 'UNKNOWN'))
            edge_color = edge_color_map.get(edge_type, "#607D8B")
            edge_dedup[pair_key] = edge_color

    for (src_id, tgt_id), edge_color in edge_dedup.items():
        dot_lines.append(f'  {src_id} -> {tgt_id} [color="{edge_color}", style="solid", minlen=1]')
    dot_lines.append("}")

    # 写入DOT文件
    final_output_path = f"{output_path}.dot" if not output_path.endswith(".dot") else output_path
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(dot_lines))

    print(f"[OK] CFG DOT文件已生成：{final_output_path}")
    return addr_color_map
