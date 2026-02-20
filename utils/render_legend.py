# legend_renderer.py
# 生成cfg和asset_flow通用的图例，保持和render_cfg.py以及extract_token_changes.py完全一致的颜色规则
from typing import Dict, List, Any
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Ellipse, FancyArrowPatch
import numpy as np

# ========================
# 内置和CFG完全一致的颜色规则
# ========================
# 合约固定配色（和render_cfg.py中完全一致）
CONTRACT_COLORS = [
    "#FF9E9E", "#81C784", "#64B5F6", "#FFF176", "#BA68C8",
    "#4DD0E1", "#FFB74D", "#F48FB1", "#AED581", "#7986CB"
]

# 边类型配色 + 对应Opcode说明（和render_cfg.py逻辑一致）
EDGE_COLOR_MAP = {
    "NORMAL": "#939393",
    "JUMP": "#575757",
    "CALL": "#0DFF00",
    "TERMINATE": "#FF5100",
}

# 边类型对应的Opcode映射（核心补充）
EDGE_OPCODE_MAP = {
    "NORMAL": "Non-terminating opcodes",
    "JUMP": "JUMP, JUMPI",
    "CALL": "CALL, CALLCODE, DELEGATECALL, STATICCALL",
    "TERMINATE": "RETURN, STOP, REVERT, INVALID, SELFDESTRUCT"
}

# 复用CFG中的核心函数
def get_valid_nodes_and_colors(cfg: object, contract_colors: List[str]) -> (List[object], List[str], List[str], Dict[str, int]):
    """
    按合约首次出现顺序分配颜色（和render_cfg.py完全一致）
    返回：有效节点、节点颜色、节点合约地址、合约→颜色索引映射
    """
    valid_nodes = []
    node_colors = []
    node_contract_addrs = []
    contract_to_color_idx = {}
    color_index = 0

    for node in cfg.nodes:
        is_fold_root = getattr(node, "is_fold_root", False)
        is_folded = getattr(node, "folded", False)
        if not (is_fold_root or not is_folded):
            continue
            
        node_addr = str(getattr(node, "address", "Unknown")).strip()
        
        if node_addr not in contract_to_color_idx:
            contract_to_color_idx[node_addr] = color_index
            color_index += 1

        cidx = contract_to_color_idx[node_addr] % len(contract_colors)
        color = contract_colors[cidx]

        valid_nodes.append(node)
        node_colors.append(color)
        node_contract_addrs.append(node_addr)

    return valid_nodes, node_colors, node_contract_addrs, contract_to_color_idx

# 复用CFG中的核心函数，生成合约→颜色映射
def get_contract_to_color(cfg: object) -> Dict[str, str]:
    _, _, _, contract_to_color_idx = get_valid_nodes_and_colors(cfg, CONTRACT_COLORS)
    contract_to_color = {}
    for addr, idx in contract_to_color_idx.items():
        contract_to_color[addr] = CONTRACT_COLORS[idx % len(CONTRACT_COLORS)]
    return contract_to_color

# ========================
# 核心图例生成函数
# ========================
def render_legend_matplotlib(
    cfg: object,                  # CFG对象（用于生成颜色映射）
    full_address_name_map: Dict[str, str],  # 地址→名称映射
    erc20_token_map: Dict[str, Any],  # ERC20映射（可选，预留扩展）
    output_path: str,             # 输出路径
    figsize: tuple = (14, 16),    # 加宽画布，适配Opcode说明
    dpi: int = 150
) -> None:
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    contract_to_color = get_contract_to_color(cfg)
    edge_color_map = EDGE_COLOR_MAP  # 内置边颜色规则

    full_name_map_lower = {addr.lower(): name for addr, name in full_address_name_map.items()}
    erc20_token_map_lower = {addr.lower(): val for addr, val in erc20_token_map.items()} if erc20_token_map else {}

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    ax.set_xlim(0, 12)   # 进一步加宽x轴，适配Opcode说明文字
    ax.set_ylim(0, 20)   # 保留足够y轴空间
    ax.axis('off')       # 隐藏坐标轴

    current_y = 19.0  # 起始y坐标（顶部）

    # ========================
    # 1. 合约样式（颜色+形状）与合约信息对应
    # ========================
    # 子标题
    ax.text(0.5, current_y, 'Contract Styles', fontsize=12, ha='left', va='center')
    current_y -= 1.2

    # 遍历所有合约，展示颜色+形状+名称+地址
    for contract_addr, color in contract_to_color.items():
        # 统一转小写，避免地址大小写不一致导致判断错误
        contract_addr_lower = contract_addr.lower()
        
        # 获取合约名称
        contract_name = full_name_map_lower.get(contract_addr_lower, 'Unknown')
        
        # 如果地址在erc20_token_map中，判定为Token合约（椭圆），否则为普通合约（矩形）
        if contract_addr_lower in erc20_token_map_lower:
            # 椭圆（ERC20 Token合约）- 带填充色
            shape = Ellipse((1.5, current_y), width=1.2, height=0.6, 
                           facecolor=color, edgecolor='black', linewidth=1)
        else:
            # 矩形（普通合约）- 带填充色
            shape = Rectangle((0.9, current_y-0.3), width=1.2, height=0.6, 
                             facecolor=color, edgecolor='black', linewidth=1)
        ax.add_patch(shape)

        # 合约信息文字（名称+地址）
        contract_text = f'{contract_name}\n({contract_addr})'
        ax.text(3.0, current_y, contract_text, fontsize=10, ha='left', va='center', wrap=True)
        
        current_y -= 1.0  # 行间距

        # 防止超出画布底部
        if current_y < 5.0:  # 预留边类型显示空间
            break

    current_y -= 1.5  # 合约和边类型之间增加间距

    # ========================
    # 2. 边类型（颜色）与类型名称、Opcode对应
    # ========================
    # 子标题
    ax.text(0.5, current_y, 'Edge Types (Corresponding Opcode)', fontsize=12, ha='left', va='center')
    current_y -= 1.2

    # 遍历边类型，绘制对应颜色的箭头+文字说明（含Opcode）
    arrow_length = 1  # 箭头长度
    for edge_type, edge_color in edge_color_map.items():
        # 绘制彩色箭头
        arrow = FancyArrowPatch(
            (1.0, current_y),                # 起点
            (1.0 + arrow_length, current_y), # 终点
            arrowstyle='->',                 # 箭头样式
            mutation_scale=20,               # 箭头大小
            linewidth=4,                     # 箭头线宽
            color=edge_color,                # 箭头颜色
            facecolor=edge_color             # 箭头填充色
        )
        ax.add_patch(arrow)
        
        # 边类型 + Opcode说明文字（分行显示，保持整洁）
        edge_text = f'{edge_type}\n({EDGE_OPCODE_MAP[edge_type]})'
        ax.text(3.0, current_y, edge_text, fontsize=10, ha='left', va='center', wrap=True)
        current_y -= 1.2  # 增加行间距，适配分行文字

    # ========================
    # 保存SVG图例
    # ========================
    svg_path = f"{output_path}_legend.svg"
    fig.savefig(
        svg_path,
        bbox_inches='tight', 
        pad_inches=0.5,    
        format='svg',
        dpi=dpi
    )

    plt.close(fig)
    print(f"✅ 简化版CFG图例已生成：{svg_path}")