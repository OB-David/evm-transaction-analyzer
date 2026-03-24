# render_cfg.py
# 仅负责CFG DOT文件生成，无任何图例相关代码/依赖/调用
import re
from typing import Any, List, Dict, Tuple

OPCODE_LABEL_FONTSIZE = 24
OPCODE_LABEL_MIN_FONTSIZE = 16

# 语义优先（高分更重要）：主流 opcode 排序
OPCODE_PRIORITY: Dict[str, int] = {
    # 资产语义 / 状态读写
    "SELFDESTRUCT": 200,
    "SSTORE": 199,
    "TSTORE": 198,
    "SLOAD": 197,
    "TLOAD": 196,
    "LOG4": 195,
    "LOG3": 194,
    "LOG2": 193,
    "LOG1": 192,
    "LOG0": 191,
    # 账户与链环境
    "CALLER": 180,
    "CALLVALUE": 179,
    "ORIGIN": 178,
    "ADDRESS": 177,
    "BALANCE": 176,
    "SELFBALANCE": 175,
    "CHAINID": 174,
    "BASEFEE": 173,
    "GASPRICE": 172,
    "COINBASE": 171,
    "TIMESTAMP": 170,
    "NUMBER": 169,
    "PREVRANDAO": 168,
    "BLOCKHASH": 167,
    "GAS": 166,
    # calldata / returndata / code 语义
    "CALLDATALOAD": 160,
    "CALLDATASIZE": 159,
    "CALLDATACOPY": 158,
    "RETURNDATASIZE": 157,
    "RETURNDATACOPY": 156,
    "CODESIZE": 155,
    "CODECOPY": 154,
    "EXTCODESIZE": 153,
    "EXTCODECOPY": 152,
    "EXTCODEHASH": 151,
    "KECCAK256": 150,
    "SHA3": 149,
    # 内存 / 机器状态
    "MSTORE": 145,
    "MSTORE8": 144,
    "MLOAD": 143,
    "MCOPY": 142,
    "MSIZE": 141,
    "PC": 140,
    # 条件与比较
    "EQ": 130,
    "ISZERO": 129,
    "LT": 128,
    "GT": 127,
    "SLT": 126,
    "SGT": 125,
    # 算术与位运算（主流）
    "ADD": 120,
    "SUB": 119,
    "MUL": 118,
    "DIV": 117,
    "SDIV": 116,
    "MOD": 115,
    "SMOD": 114,
    "ADDMOD": 113,
    "MULMOD": 112,
    "EXP": 111,
    "SIGNEXTEND": 110,
    "AND": 109,
    "OR": 108,
    "XOR": 107,
    "NOT": 106,
    "BYTE": 105,
    "SHL": 104,
    "SHR": 103,
    "SAR": 102,
}

# 内部折叠边界标记或低信号通用指令，不参与显示
IGNORED_OPCODES_EXACT = {
    "POP",
    "JUMPDEST",
    # 边语义已由CFG边表达，不在节点标签重复展示
    "JUMP",
    "JUMPI",
    "CALL",
    "DELEGATECALL",
    "CALLCODE",
    "STATICCALL",
    "CREATE",
    "CREATE2",
    "RETURN",
    "REVERT",
    "STOP",
    "DISPATCH_LOGIC_SINK",
    "MERGE_POINT_SEGMENT",
    "SELF_LOOP_DETECTED",
    "FEEDBACK_LOOP_START",
    "FEEDBACK_LOOP_END",
}
IGNORED_OP_PREFIXES = ("PUSH", "DUP", "SWAP", "TIMELINE_SEG_", "BRANCH_SEGMENT_")

INSTRUCTION_TUPLE_RE = re.compile(r"\(\s*'[^']+'\s*,\s*'([^']+)'\s*\)")
INSTRUCTION_DICT_RE = re.compile(r"'opcode'\s*:\s*'([^']+)'")

def escape_dot(s: Any) -> str:
    """转义DOT特殊字符"""
    if s is None or s == "" or str(s) == "Unknown":
        return "Unknown"
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s.replace('"', '\\"').replace("|", "\\|").replace("{", "\\{").replace("}", "\\}")

def escape_dot_label(s: Any) -> str:
    """转义DOT标签，保留换行（\n）"""
    if s is None:
        return ""
    text = str(s).replace("\r", "")
    text = text.replace("\\", "\\\\")
    text = text.replace("\n", "\\n")
    return text.replace('"', '\\"').replace("|", "\\|").replace("{", "\\{").replace("}", "\\}")

def _extract_opcode(instr: Any) -> str:
    """从 tuple/dict/字符串 指令中提取 opcode"""
    if isinstance(instr, tuple) and len(instr) >= 2:
        return str(instr[1]).strip().upper()

    if isinstance(instr, dict):
        op = instr.get("opcode") or instr.get("op")
        return str(op).strip().upper() if op else ""

    if isinstance(instr, str):
        m = INSTRUCTION_TUPLE_RE.search(instr)
        if m:
            return m.group(1).strip().upper()
        m = INSTRUCTION_DICT_RE.search(instr)
        if m:
            return m.group(1).strip().upper()

    return ""

def _is_ignored_opcode(opcode: str) -> bool:
    if not opcode:
        return True
    if opcode in IGNORED_OPCODES_EXACT:
        return True
    return opcode.startswith(IGNORED_OP_PREFIXES)

def get_priority_opcode_label(node: Any, max_tied_opcodes: int = 2) -> str:
    """
    选出节点内最重要 opcode（并列时最多显示两个，按两行输出）
    若节点仅包含低优先级/未排序opcode，则返回空字符串
    """
    instructions = getattr(node, "instructions", []) or []
    best_score = None
    best_opcodes: List[str] = []

    for instr in instructions:
        opcode = _extract_opcode(instr)
        if _is_ignored_opcode(opcode):
            continue

        score = OPCODE_PRIORITY.get(opcode)
        if score is None:
            # 低优先级或罕见opcode不单独排序，不显示
            continue

        if best_score is None or score > best_score:
            best_score = score
            best_opcodes = [opcode]
        elif score == best_score and opcode not in best_opcodes:
            best_opcodes.append(opcode)

    if not best_opcodes:
        return ""

    return "\n".join(best_opcodes[:max_tied_opcodes])

def get_opcode_label_fontsize(opcode_label: str) -> int:
    """根据标签长度和行数动态缩放字号，避免文本溢出节点"""
    if not opcode_label:
        return OPCODE_LABEL_FONTSIZE

    lines = opcode_label.split("\n")
    line_count = len(lines)
    max_line_len = max((len(line.strip()) for line in lines), default=0)

    fontsize = OPCODE_LABEL_FONTSIZE
    if line_count >= 2:
        fontsize -= 2

    if max_line_len >= 18:
        fontsize -= 6
    elif max_line_len >= 14:
        fontsize -= 4
    elif max_line_len >= 11:
        fontsize -= 2

    return max(OPCODE_LABEL_MIN_FONTSIZE, fontsize)

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

def render_transaction(contract_colors: List[str], edge_color_map: Dict[str, str], cfg: object, output_path: str, full_address_name_map: Dict[str, str], erc20_token_map: Dict[str, Any], rankdir: str = "LR", show_priority_opcode: bool = False) -> None:
    """
    仅生成CFG DOT文件
    :param cfg: 包含nodes/edges的CFG对象
    :param output_path: DOT文件输出路径
    :param full_address_name_map: 地址→名称映射
    :param erc20_token_map: ERC20合约地址→名称映射
    :param rankdir: 图表方向 LR=左到右
    :param show_priority_opcode: 是否在节点内显示最重要opcode
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

    node_style_line = (
        '  node [fontname="Arial", fontsize=24, shape=rect, style=filled, fixedsize=false, width=2.55, height=1.62, margin="0.10,0.06"];'
        if show_priority_opcode
        else '  node [fontname="Arial", fontsize=128, shape=rect, style=filled, fixedsize=true, width=2.55, height=1.62];'
    )

    dot_lines = [
            "digraph CFG {",
            f"  rankdir={rankdir};",
            # 全局设置：spline曲线边、compound支持跨cluster边、newrank改善排列
            '  graph [nodesep=0.62, ranksep=1.05, pad=0.18, charset="utf-8", splines=spline, compound=true, newrank=true, overlap=false];',
            # 节点设置：plain模式启用自适应尺寸，避免opcode文字溢出
            node_style_line,
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
            opcode_label = get_priority_opcode_label(node) if show_priority_opcode else ""
            escaped_label = escape_dot_label(opcode_label) if opcode_label else ""
            node_attrs = [
                f'shape="{node_shape}"',
                f'label="{escaped_label}"',
                f'style="{style_str}"',
                f'fillcolor="{color}"',
                f'color="{"red" if has_action else "black"}"',
                f'penwidth={current_penwidth}',
            ]
            if opcode_label:
                node_attrs.append(f"fontsize={get_opcode_label_fontsize(opcode_label)}")
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
