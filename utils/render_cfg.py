# render_cfg.py
# 负责将CFG数据渲染为DOT文件，并生成包含目标节点双层信息的JSON文件
import json
from typing import Any, Optional

def escape_dot(s: Any) -> str:
    """转义DOT特殊字符，适配任意输入类型"""
    if s is None or s == "" or str(s) == "Unknown":
        return "Unknown"
    s = str(s).replace("\n", " ").replace("\r", " ")
    s = s.replace('"', '\\"').replace("|", "\\|").replace("{", "\\{").replace("}", "\\}")
    return s

def addr_short(s: Any) -> str:
    """缩短以太坊地址显示（0x开头的地址）"""
    s = str(s)
    return s[:8] + "..." + s[-4:] if s.startswith("0x") and len(s) > 8 else s

def extract_edge_seq(edge_id: Optional[str]) -> str:
    """从edge_id中提取数字序号（适配edge_1、edge_1_xxx等格式）"""
    if not edge_id or not str(edge_id).startswith("edge_"):
        return "0"
    parts = str(edge_id).split("_")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return "0"

def render_transaction(cfg: object, output_path: str, rankdir: str = "TB") -> None:
    """
    将CFG对象渲染为DOT文件 + 目标节点JSON文件
    :param cfg: CFG对象（需包含nodes/edges属性）
    :param output_path: 输出文件前缀（无需加.dot后缀）
    :param rankdir: 图布局方向（TB=上下，LR=左右）
    """
    # 严格参数校验：确保传入合法的CFG对象
    if not hasattr(cfg, 'nodes') or not hasattr(cfg, 'edges'):
        raise TypeError(f"cfg必须是包含nodes/edges属性的CFG对象，当前类型：{type(cfg)}")
    
    # 配色方案（固定）
    contract_colors = [
        "#FF9E9E", "#81C784", "#64B5F6", "#FFF176", "#BA68C8",
        "#4DD0E1", "#FFB74D", "#F48FB1", "#AED581", "#7986CB"
    ]
    edge_color_map = {
        "NORMAL":"#000000", "JUMP":"#FF5722", "NOTJUMP":"#FFC107",
        "CALL":"#2196F3", "TERMINATE":"#F48FB1", "JUMPI":"#9C27B0",
        "STATICCALL":"#00BCD4", "OTHERCALL":"#FF9800", "RETURN":"#4CAF50",
        "REVERT":"#FFEB3B", "DESTRUCT":"#9E9E9E", "CREATE":"#8BC34A", "UNKNOWN":"#607D8B"
    }

    # 初始化DOT文件内容
    dot_lines = [
        "digraph CFG {",
        f"  rankdir={rankdir};",
        '  node [shape=record, fontname="Arial", fontsize=9, color=black, style=filled, margin=0.2, fixedsize=false, width=0, height=0];',
        '  edge [fontname="Arial", fontsize=8];',
        '  graph [nodesep=1.2, ranksep=1.5, charset="utf-8", maxiter=1000, dpi=96];'
    ]

    # 构建合约地址->颜色映射（避免重复颜色）
    try:
        contract_addrs = list({n.address for n in cfg.nodes if hasattr(n, 'address')})
        color_map = {addr: contract_colors[i % len(contract_colors)] for i, addr in enumerate(contract_addrs)}
    except Exception as e:
        print(f"警告：构建合约颜色映射失败（{e}），使用默认颜色")
        color_map = {}

    # 收集目标节点数据 + 生成节点DOT代码
    target_nodes_data = []
    all_nodes = [n for n in cfg.nodes if hasattr(n, "fold_info")]

    for node in all_nodes:
        node_id = f"node_{node.id}"  # 使用BlockNode的自增ID
        node_addr = escape_dot(node.address)
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        is_hidden_node = is_folded and not is_fold_root

        # 1. 折叠层（语义层）内容
        semantic_table = [
            f"{{ID: {node.id} | Contract: {node_addr}}}",
            f"{{StartPC: {escape_dot(node.start_pc)} | EndPC: {escape_dot(node.fold_info.get('end_pc', '0x0'))} | Blocks: {escape_dot(node.fold_info.get('blocks_number', 0))} | Gas: {escape_dot(node.fold_info.get('total_gas', 0))}}}",
            "{ }"
        ]
        actions = node.fold_info.get("actions", [])
        action_text = []
        act_idx = 1

        # 遍历节点的所有Action事件（ETH+ERC20）
        for act in actions:
            # 处理ETH转账事件
            if "eth_event" in act and act["eth_event"]:
                eth_item = act["eth_event"]
                eth_str = (
                    f"Action{act_idx}: ETH From: {addr_short(escape_dot(eth_item['from']))} "
                    f"To: {addr_short(escape_dot(eth_item['to']))} Amount: {escape_dot(eth_item['amount'])}..."
                )
                action_text.append(eth_str)
                act_idx += 1
            
            # 处理ERC20读写事件
            for erc in act["erc20_events"]:
                erc_str = (
                    f"Action{act_idx}: {escape_dot(erc['tokenname'])} {escape_dot(erc['type'])} "
                    f"Address: {addr_short(escape_dot(erc['user']))} Balance: {escape_dot(erc['balance'])}"
                )
                action_text.append(erc_str)
                act_idx += 1

        # 拼接语义层Action文本
        action_line = "\\n".join(action_text) if action_text else "No actions"
        semantic_table[2] = f"{{ {action_line} }}"
        label_semantic = "|".join(semantic_table)

        # 2. 基础层（原始指令层）内容
        base_table = [
            f"{{ID: {node.id} | Contract: {node_addr}}}",
            f"{{StartPC: {escape_dot(node.start_pc)} | EndPC: {escape_dot(node.end_pc)} | Blocks: 1 | Gas: {escape_dot(node.total_gas)}}}",
            "{ }"
        ]
        original_instructions = getattr(node, "instructions", [])
        instruction_text = [f"{pc}: {escape_dot(op)}" for pc, op in original_instructions]
        # 指令分组显示（每4个一组，避免节点过长）
        group_size = 4
        instruction_groups = [instruction_text[i:i+group_size] for i in range(0, len(instruction_text), group_size)]
        instruction_line = "\\n".join(["\   ".join(group) for group in instruction_groups]) if instruction_text else "No base instructions"
        base_table[2] = f"{{ {instruction_line} }}"
        label_base = "|".join(base_table)

        # 收集目标节点数据（仅保留折叠根节点/未折叠节点）
        is_target_node = is_fold_root or (not is_folded)
        if is_target_node:
            target_nodes_data.append({
                "node_id": node_id,
                "node_internal_id": node.id,
                "contract_address": node.address,
                "node_type": "fold_root" if is_fold_root else "normal_unfolded",
                "semantic": {
                    "label": label_semantic,
                    "contract": node_addr,
                    "stats": {
                        "start_pc": node.start_pc,
                        "end_pc": node.fold_info.get("end_pc", "0x0"),
                        "blocks": node.fold_info.get("blocks_number", 0),
                        "gas": node.fold_info.get("total_gas", 0)
                    },
                    "actions": action_text
                },
                "base": {
                    "label": label_base,
                    "contract": node_addr,
                    "stats": {
                        "start_pc": node.start_pc,
                        "end_pc": node.end_pc,
                        "blocks": 1,
                        "gas": node.total_gas
                    },
                    "instructions": instruction_line
                }
            })

        # 生成节点DOT属性
        if is_hidden_node:
            # 隐藏节点（折叠的中间节点）
            style = "filled"
            fillcolor = "#f0f0f0"
            node_class = "hidden-node"
            label = label_base
        else:
            # 可见节点（根节点/未折叠节点）
            style = "filled"
            fillcolor = color_map.get(node.address, "#4DD0E1")
            node_class = "fold-root" if is_fold_root else "normal-node"
            label = label_semantic
        
        node_attrs = [
            f'label="{{{label}}}"',
            f'style="{style}"',
            f'fillcolor="{fillcolor}"',
            f'color="black"',
            f'class="{node_class}"',
            f'fixedsize=false',
            f'margin=0.3'
        ]
        node_attr_line = ", ".join(node_attrs)
        dot_lines.append(f"  {node_id} [{node_attr_line}];")

    # 生成边DOT代码
    edge_counter = 0
    for edge in getattr(cfg, 'edges', []):
        if not (hasattr(edge, 'source') and hasattr(edge, 'target')):
            continue
        
        edge_id = getattr(edge, "edge_id", f"edge_{edge_counter+1}")
        edge_seq = extract_edge_seq(edge_id)
        src_id = edge.source.id
        tgt_id = edge.target.id
        is_folded_edge = getattr(edge, "folded_edge", False)
        is_edge_visible = getattr(edge, "visible", True)

        # 边样式配置
        edge_style = "solid"
        edge_class = "hidden-edge" if (is_folded_edge or not is_edge_visible) else "normal-edge"
        edge_type = escape_dot(getattr(edge, 'edge_type', 'UNKNOWN'))
        edge_label = f"{edge_seq} \\n {edge_type}"
        edge_color = edge_color_map.get(edge_type, "#607D8B")
        if edge_class == "hidden-edge":
            edge_color = "#cccccc"

        edge_attrs = [
            f'label="{edge_label}"',
            f'color="{edge_color}"',
            f'style="{edge_style}"',
            f'class="{edge_class}"',
            f'labelfloat=true'
        ]
        edge_attr_line = ", ".join(edge_attrs)
        dot_lines.append(f"  node_{src_id} -> node_{tgt_id} [{edge_attr_line}];")
        edge_counter += 1

    dot_lines.append("}")

    # 写入DOT文件（自动处理后缀）
    final_output_path = f"{output_path}.dot" if not output_path.endswith(".dot") else output_path
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(dot_lines))

    # 写入目标节点JSON文件（包含双层信息）
    json_output_path = f"{output_path}_target_nodes_data.json"
    with open(json_output_path, 'w', encoding='utf-8') as f:
        json.dump(target_nodes_data, f, ensure_ascii=False, indent=2)

    print(f"✅ 语义层DOT已生成：{final_output_path}")
    print(f"✅ 目标节点双层信息JSON已生成：{json_output_path}")