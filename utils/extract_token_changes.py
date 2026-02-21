# extract_token_changes.py 负责从余额变化表格中提取代币转移事件
# 生成资产流向图的 DOT 文件

from collections import defaultdict
from graphviz import Digraph
from utils.cfg_transaction import CFGConstructor
from utils.cfg_structure import CFG
from utils.basic_block import Block
import json

def hex_to_int_safe(x: str) -> int:
    try:
        return int(x, 16)
    except Exception:
        return 0

def format_scientific_html(value: float, precision: int = 2, sup_size: int = 8) -> str:
    """
    将浮点数格式化为 HTML 科学计数法，指数部分使用较小的字体
    :param value: 要格式化的浮点数
    :param precision: 尾数小数位数
    :param sup_size: 指数部分的字体大小（点）
    """
    if value == 0:
        return "0"
    s = f"{value:.{precision}e}"
    mantissa, exp = s.split('e')
    exp = int(exp)
    # 在 <sup> 内再套一层 <font> 控制字体大小
    return f"{mantissa}×10<sup><font point-size='{sup_size}'>{exp}</font></sup>"

def pair_transactions(all_changes, token_decimals_map=None):
    """
    按 all_changes 顺序配对余额变化并确定交易顺序
    :param all_changes: 所有余额变化列表
    :param token_decimals_map: 代币地址到精度的映射，用于格式化孤立余额
    """
    paired = []
    node_annotations = defaultdict(list)

    order_counter = 0
    pending_erc20 = {}

    for c in all_changes:

        # -------- ETH --------
        if c["type"] == "ETH_TRANSFER":
            formatted_val = abs(int(c["eth_value"])) / (10 ** 18)
            order_counter += 1
            paired.append({
                "order": order_counter,
                "from": c["from_address"],
                "to": c["to_address"],
                "amount": formatted_val,
                "token": "ETH",
                "token_addr": "ETH",
                "source_pcs": [c["pc"]],
            })
            continue

        # -------- ERC20 --------
        if c["type"] != "ERC20_BALANCE_CHANGE":
            continue

        token_addr = c["erc20_token_address"]
        token_name = c["token_name"]
        user = c["user_address"]
        val = int(c["changed_balance"])

        # 获取该代币的精度，默认为 18
        decimals = 18
        if token_decimals_map and token_addr in token_decimals_map:
            decimals = token_decimals_map[token_addr]

        if token_addr not in pending_erc20:
            # 第一次出现，占一个顺序
            order_counter += 1
            pending_erc20[token_addr] = {
                "order": order_counter,
                "user": user,
                "value": val,
                "token": token_name,
                "token_addr": token_addr,
                "source_pcs": [c["SLOAD_pc"], c["SSTORE_pc"]],
                "decimals": decimals,  # 保存精度，用于后续格式化
            }
        else:
            prev = pending_erc20[token_addr]

            # 配对条件：两次变化金额之和为 0（即一正一负）
            if prev["value"] + val == 0:
                # 确定发送方和接收方
                if prev["value"] < 0:
                    sender = prev
                    receiver = c
                else:
                    sender = c
                    receiver = prev

                formatted_val = abs(val) / (10 ** decimals)
                paired.append({
                    "order": prev["order"],
                    "from": sender["user"],
                    "to": receiver["user_address"],
                    "amount": formatted_val,
                    "token": token_name,
                    "token_addr": token_addr,
                    "source_pcs": {
                        "sender_sload_pc": sender["source_pcs"][0],
                        "sender_sstore_pc": sender["source_pcs"][1],
                        "receiver_sload_pc": receiver["SLOAD_pc"],
                        "receiver_sstore_pc": receiver["SSTORE_pc"],
                    }
                })

                del pending_erc20[token_addr]
            else:
                # 未配对，暂时保留（可根据业务决定是否合并累积，此处简单跳过）
                pass

    # 遍历结束，剩余的是孤立变化
    for v in pending_erc20.values():
        # --- 新增：跳过 WETH 的注释，避免与后续绘制的边重复 ---
        if v["token"].lower() == "weth" or v["token"].lower() == "wrapped ether":
            continue
        sign = "+" if v["value"] > 0 else "-"
        raw = abs(v["value"])
        # 格式化数值
        formatted_val = raw / (10 ** v["decimals"])
        amount_str = format_scientific_html(formatted_val)
        node_annotations[v["user"]].append(
            f"({v['order']}) {v['token']}: {sign}{amount_str}"
        )

    paired.sort(key=lambda x: x["order"])

    return paired, node_annotations, pending_erc20

def render_asset_flow(paired, node_annotations, users_addresses, full_address_name_map, pending_erc20, addr_color_map, output_file="asset_flow.dot"):
    """
    绘制资产流向图（DOT格式）
    :param paired: 已配对的交易流列表（含ETH和ERC20转账）
    :param node_annotations: 节点注释（非配对ERC20变化，已排除WETH）
    :param users_addresses: 用户地址列表（用于生成别名）
    :param full_address_name_map: 合约名称映射（地址 -> 名称）
    :param color_map: 地址颜色映射（地址 -> 颜色代码）
    :param pending_erc20: 所有未配对的ERC20变化（包含WETH等）
    :param output_file: 输出DOT文件路径
    """
    dot = Digraph(engine="dot")
    dot.graph_attr['rankdir'] = 'LR'

    users_set = set(users_addresses)

    # --- 为用户生成别名 User 1, User 2 ...
    user_alias_map = {}
    sorted_users = sorted(list(users_set))  # 保证排序一致性
    for idx, addr in enumerate(sorted_users):
        user_alias_map[addr] = f"User {idx + 1}"

    # -------- 收集所有需要绘制的地址 --------
    addresses = set()
    for p in paired:
        addresses.add(p["from"])
        addresses.add(p["to"])
    addresses.update(node_annotations.keys())
    for v in pending_erc20.values():
        addresses.add(v["user"])
        addresses.add(v["token_addr"])

    # -------- 绘制所有节点 --------
    for addr in addresses:
        is_user = addr in users_set
        full_name_map_lower = {addr.lower(): name for addr, name in full_address_name_map.items()}
        erc20_addrs_lower = [
            addr for addr, name in full_name_map_lower.items()
            if not (name.startswith("contract_") or name.startswith("User_"))
        ]
        if is_user:
            shape = "diamond"
        elif addr.lower() in erc20_addrs_lower:
            shape = "ellipse"
        else:
            shape = "record"

        node_color = addr_color_map.get(addr, "#FFFFFF")

        # 确定节点显示名称
        if is_user:
            display_name = user_alias_map.get(addr, "Unknown User")
        else:
            display_name = full_address_name_map.get(addr, addr[:8] + "...")

        # 构建节点标签（支持多行HTML）
        if addr in node_annotations and node_annotations[addr]:
            lines = [display_name] + node_annotations[addr]
            label = "<" + "<br/>".join(lines) + ">"
        else:
            label = "<" + display_name + ">"

        dot.node(
            addr,  # 内部ID使用真实地址，确保边连接正确
            label=label,
            shape=shape,
            fillcolor=node_color,
            style="filled"
        )

    # -------- 绘制已配对的边（按顺序） --------
    paired_sorted = sorted(paired, key=lambda x: x["order"])
    for p in paired_sorted:

        if p["token"] == "ETH":
            edge_color = addr_color_map.get(p["from"], "#FFFFFF")
        else:
            edge_color = addr_color_map.get(p["token_addr"], "#FFFFFF")

        amount_str = format_scientific_html(p["amount"])
        edge_label = f"({p['order']}) {p['token']}: {amount_str}"

        dot.edge(
            p["from"],
            p["to"],
            label="<" + edge_label + ">",   # HTML标签，支持换行
            color=edge_color,
            fontcolor=edge_color
        )

    # -------- 新增：绘制WETH的铸造/销毁边（来自未配对变化） --------
    for v in pending_erc20.values():
        token_name = v["token"]
        if token_name.lower() != "weth" and token_name.lower() != "wrapped ether":
            continue

        user = v["user"]
        token_addr = v["token_addr"]
        value = v["value"]
        order = v["order"]
        decimals = v["decimals"]
        amount = abs(value) / (10 ** decimals)
        amount_str = format_scientific_html(amount)
        edge_color = addr_color_map.get(token_addr, "#FFFFFF")

        if value > 0:
            # 铸造：从WETH合约指向用户
            src = token_addr
            tgt = user
            label = f"({order}) WETH(mint): {amount_str}"
        else:
            # 销毁：从用户指向WETH合约
            src = user
            tgt = token_addr
            label = f"({order}) WETH(burn): {amount_str}"

        dot.edge(src, tgt, label="<" + label + ">", color=edge_color, fontcolor=edge_color, style="dashed")

    # 保存DOT文件
    dot.save(output_file)
    return dot

def afg_to_cfg(paired, pending_erc20, cfg_constructor: CFGConstructor, tx_cfg: CFG):
    edge_link = []
    for p in paired:
        if p["token"] == "ETH":
            matched_block = cfg_constructor.find_node_by_pc_address(tx_cfg, p["from"], p["source_pcs"][0])
            if matched_block:
               edge_link.append({
                   "edge_id": p["order"],
                   "type": "ETH_TRANSFER",
                   "matched_blocks": matched_block
               })
        else:
            sender_sload_block = cfg_constructor.find_node_by_pc_address(tx_cfg, p["token_addr"], p["source_pcs"]["sender_sload_pc"])
            sender_sstore_block = cfg_constructor.find_node_by_pc_address(tx_cfg, p["token_addr"], p["source_pcs"]["sender_sstore_pc"])
            receiver_sload_block = cfg_constructor.find_node_by_pc_address(tx_cfg, p["token_addr"], p["source_pcs"]["receiver_sload_pc"])
            receiver_sstore_block = cfg_constructor.find_node_by_pc_address(tx_cfg, p["token_addr"], p["source_pcs"]["receiver_sstore_pc"])

            if sender_sload_block and sender_sstore_block and receiver_sload_block and receiver_sstore_block:
                edge_link.append({
                    "edge_id": p["order"],
                    "type": "ERC20_TOKEN_TRANSFER",
                    "matched_blocks": {
                        "sender": (sender_sload_block, sender_sstore_block),
                        "receiver": (receiver_sload_block, receiver_sstore_block)
                    }
                })  

    for v in pending_erc20.values():
        sload_block = cfg_constructor.find_node_by_pc_address(tx_cfg, v["token_addr"], v["source_pcs"][0])
        sstore_block = cfg_constructor.find_node_by_pc_address(tx_cfg, v["token_addr"], v["source_pcs"][1])
        edge_link.append({
            "edge_id": v["order"],
            "type": "ERC20_BALANCE_CHANGE",
            "matched_blocks": [sload_block, sstore_block]
        })

    edge_link.sort(key=lambda x: x["edge_id"])
    return edge_link

def serialize_block_node(node):
    """将 BlockNode 转换为可 JSON 序列化的字典"""
    if node is None:
        return None
    return {
        "BlockID": node.id
    }

def edge_link_to_json(edge_link):
    """处理 edge_link 列表中的嵌套逻辑并转为字典列表"""
    serializable_list = []
    
    for item in edge_link:
        # 深拷贝基础字段
        entry = {
            "edge_id": item["edge_id"],
            "type": item["type"]
        }
        
        raw_blocks = item["matched_blocks"]
        
        # 根据不同类型的 matched_blocks 进行处理
        if item["type"] == "ETH_TRANSFER":
            # 单个 BlockNode
            entry["matched_blocks"] = serialize_block_node(raw_blocks)
            
        elif item["type"] == "ERC20_TOKEN_TRANSFER":
            # 嵌套字典: {"sender": (node1, node2), "receiver": (node3, node4)}
            entry["matched_blocks"] = {
                "sender": [serialize_block_node(n) for n in raw_blocks["sender"]],
                "receiver": [serialize_block_node(n) for n in raw_blocks["receiver"]]
            }
            
        elif item["type"] == "ERC20_BALANCE_CHANGE":
            entry["matched_blocks"] = [serialize_block_node(n) for n in raw_blocks]
            
        serializable_list.append(entry)
    
    # 转换为格式化的 JSON 字符串
    return json.dumps(serializable_list, indent=4, ensure_ascii=False)