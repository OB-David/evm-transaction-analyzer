# legend_renderer.py
# 生成cfg和asset_flow通用的图例，保持和render_cfg.py以及extract_token_changes.py完全一致的颜色规则
from typing import Dict, List, Any
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Ellipse, FancyArrowPatch, Polygon
from utils.render_cfg import get_valid_nodes_and_colors

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

# 复用CFG中的核心函数，生成合约→颜色映射
def get_contract_to_color(cfg: object) -> Dict[str, str]:
    _, _, _, contract_to_color_idx, _ = get_valid_nodes_and_colors(cfg, CONTRACT_COLORS)
    contract_to_color = {}
    for addr, idx in contract_to_color_idx.items():
        contract_to_color[addr] = CONTRACT_COLORS[idx % len(CONTRACT_COLORS)]
    return contract_to_color

# ========================
# 核心图例生成函数（标题样式优化版）
# ========================
def render_legend_matplotlib(
    cfg: object,                  # CFG对象（用于生成颜色映射）
    full_address_name_map: Dict[str, str],  # 地址→名称映射
    erc20_token_map: Dict[str, Any],  # ERC20映射（可选，预留扩展）
    output_path: str,             # 输出路径
    users_addresses: List[str] = None,  # 新增：用户地址列表
    figsize: tuple = (14, 16),    # 紧凑画布尺寸
    dpi: int = 150
) -> None:
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    contract_to_color = get_contract_to_color(cfg)
    edge_color_map = EDGE_COLOR_MAP
    users_addresses = users_addresses or []

    full_name_map_lower = {addr.lower(): name for addr, name in full_address_name_map.items()}
    erc20_token_map_lower = {addr.lower(): val for addr, val in erc20_token_map.items()} if erc20_token_map else {}

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 18)
    ax.axis('off')

    current_y = 17.0

    # ========================
    # 0. 用户地址图例（标题字号11，内部间距0.9，组间间距0.6）
    # ========================
    if users_addresses:
        # 标题字号
        ax.text(0.5, current_y, 'User Addresses', fontsize=10, ha='left', va='center', fontweight='bold')
        # 标题内部间距（标题到元素）
        current_y -= 0.9

        # 按用户地址字母序排序（小写，避免大小写干扰）
        user_alias_map = {}
        for idx, addr in enumerate(users_addresses):
            user_alias_map[addr] = f"User {idx + 1}"

        for addr, user_name in user_alias_map.items():
            # 菱形（和 asset_flow diamond 一致）
            diamond_vertices = [
                (1.5, current_y + 0.3),
                (1.5 + 0.6, current_y),
                (1.5, current_y - 0.3),
                (1.5 - 0.6, current_y)
            ]
            diamond = Polygon(
                diamond_vertices,
                facecolor="#FFFFFF",
                edgecolor="black",
                linewidth=1
            )
            ax.add_patch(diamond)

            # 用户名称放入菱形中心
            ax.text(1.5, current_y, user_name, fontsize=8, ha='center', va='center', color='black')
            
            # 地址显示在右侧
            addr_text = f"{addr}"
            ax.text(3.0, current_y, addr_text, fontsize=9, ha='left', va='center', wrap=True)

            current_y -= 0.7

        # 组间间距（用户到Token合约
        current_y -= 0.6

    # ========================
    # 1. 合约样式（标题字号11，内部间距0.9，组间间距0.6）
    # ========================
    # 先拆分Token合约和普通合约，并整理名称
    token_contracts = []  # 格式：(合约名称, 合约地址, 颜色)
    normal_contracts = [] # 格式：(合约名称, 合约地址, 颜色)

    for contract_addr, color in contract_to_color.items():
        contract_addr_lower = contract_addr.lower()
        # 获取合约名称（无名称则用Unknown）
        contract_name = full_name_map_lower.get(contract_addr_lower, 'Unknown')
        
        # 区分Token合约和普通合约
        if contract_addr_lower in erc20_token_map_lower:
            token_contracts.append((contract_name, contract_addr, color))
        else:
            normal_contracts.append((contract_name, contract_addr, color))

    # 1.1 Token合约（按合约名称字母序排序）
    if token_contracts:
        # 标题字号
        ax.text(0.5, current_y, 'ERC20 Token Contracts', fontsize=10, ha='left', va='center', fontweight='bold')
        # 标题内部间距
        current_y -= 0.9

        # 按合约名称小写字母序排序（abcd）
        sorted_token_contracts = sorted(token_contracts, key=lambda x: x[0].lower())
        for contract_name, contract_addr, color in sorted_token_contracts:
            # 椭圆（ERC20 Token合约
            shape = Ellipse((1.5, current_y), width=1.2, height=0.6, 
                           facecolor=color, edgecolor='black', linewidth=1)
            ax.add_patch(shape)
            # 合约名称放入椭圆中心
            ax.text(1.5, current_y, contract_name, fontsize=7, ha='center', va='center', color='black')

            # 地址显示在右侧
            addr_text = f"{contract_addr}"
            ax.text(3.0, current_y, addr_text, fontsize=9, ha='left', va='center', wrap=True)
            
            current_y -= 0.7
            if current_y < 4.0:
                break

        # 组间间距（Token到普通合约）
        current_y -= 0.5

    # 1.2 普通合约（按合约名称字母序排序）
    if normal_contracts:
        ax.text(0.5, current_y, 'Normal Contracts', fontsize=10, ha='left', va='center', fontweight='bold')
        # 标题内部间距
        current_y -= 0.9

        # 按合约名称小写字母序排序（abcd）
        sorted_normal_contracts = sorted(normal_contracts, key=lambda x: x[0].lower())
        for contract_name, contract_addr, color in sorted_normal_contracts:
            # 矩形（普通合约）
            shape = Rectangle((0.9, current_y-0.3), width=1.2, height=0.6, 
                             facecolor=color, edgecolor='black', linewidth=1)
            ax.add_patch(shape)
            # 合约名称放入矩形中心
            ax.text(1.5, current_y, contract_name, fontsize=7, ha='center', va='center', color='black')

            # 地址显示在右侧
            addr_text = f"{contract_addr}"
            ax.text(3.0, current_y, addr_text, fontsize=9, ha='left', va='center', wrap=True)
            
            current_y -= 0.7
            if current_y < 4.0:
                break

        #
        current_y -= 0.5

    # ========================
    # 2. 边类型（标题字号11，内部间距0.9）
    # ========================
    ax.text(0.5, current_y, 'CFG\'s Edge Types', fontsize=10, ha='left', va='center', fontweight='bold')
    current_y -= 0.9

    arrow_length = 1
    for edge_type, edge_color in edge_color_map.items():
        # 绘制彩色箭头
        arrow = FancyArrowPatch(
            (1.0, current_y),                # 起点
            (1.0 + arrow_length, current_y), # 终点
            arrowstyle='->',                 # 箭头样式
            mutation_scale=18,               # 箭头大小
            linewidth=3,                     # 箭头线宽
            color=edge_color,                # 箭头颜色
        )
        ax.add_patch(arrow)
        
        # 边类型 + Opcode说明文字
        edge_text = f'{edge_type}\n({EDGE_OPCODE_MAP[edge_type]})'
        ax.text(3.0, current_y, edge_text, fontsize=9, ha='left', va='center', wrap=True)
        current_y -= 0.8

    # ========================
    # 保存SVG图例
    # ========================
    svg_path = f"{output_path}_legend.svg"
    fig.savefig(
        svg_path,
        bbox_inches='tight', 
        pad_inches=0.3,    
        format='svg',
        dpi=dpi
    )

    plt.close(fig)
    print(f"✅ 最终版图例已生成（标题样式优化）：{svg_path}")