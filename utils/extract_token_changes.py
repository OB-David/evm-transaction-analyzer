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

def pair_transactions(all_changes):
    """
    按 all_changes 顺序配对余额变化并确定交易顺序
    """
    paired = []
    node_annotations = defaultdict(list)

    order_counter = 0
    # 建一个工作区，记录未配对的 ERC20 变化
    pending_erc20 = {}

    for c in all_changes:

        # -------- ETH --------
        if c["type"] == "ETH_TRANSFER":
            order_counter += 1
            paired.append({
                "order": order_counter,
                "from": c["from_address"],
                "to": c["to_address"],
                "amount": int(c["eth_value"]),
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

        if token_addr not in pending_erc20:
            # 第一次出现，占一个顺序
            order_counter += 1
            pending_erc20[token_addr] = {
                "order": order_counter,
                "user": user,
                "value": val,
                "token": token_name,
                "token_addr": token_addr,
                "source_pcs": [c["SLOAD_pc"], c["SSTORE_pc"]]
            }
        else:
            prev = pending_erc20[token_addr]

            if prev["value"] + val == 0:
                sender = prev if prev["value"] < 0 else user
                receiver = c if prev["value"] < 0 else prev["user"]

                paired.append({
                    "order": prev["order"],
                    "from": sender["user"],
                    "to": receiver["user_address"],
                    "amount": abs(val),
                    "token": token_name,
                    "token_addr": token_addr,
                    "source_pcs": {"sender_sload_pc": sender["source_pcs"][0], "sender_sstore_pc": sender["source_pcs"][1], 
                                   "receiver_sload_pc": receiver["SLOAD_pc"], "receiver_sstore_pc": receiver["SSTORE_pc"]}
                })

                del pending_erc20[token_addr]
            else:
                pass

    # -------- 遍历结束，剩余是孤立变化 --------
    for v in pending_erc20.values():
        sign = "+" if v["value"] > 0 else ""
        node_annotations[v["user"]].append(
            f"({v['order']}) {v['token']}: {sign}{v['value']}"
        )

    paired.sort(key=lambda x: x["order"])

    return paired, node_annotations, pending_erc20


def render_asset_flow(paired, node_annotations, users_addresses, output_file="asset_flow.dot"):
    """
    从配对数据渲染资产流向图（仅生成 DOT）
    """
    dot = Digraph(engine="dot")

    token_color = {}
    palette = ['#ff5733', '#33ff57', '#3357ff', '#ff33a1', '#ffb233']

    users_set = set(users_addresses)

    # -------- 收集所有地址 --------
    addresses = set()
    for p in paired:
        addresses.add(p["from"])
        addresses.add(p["to"])
    addresses.update(node_annotations.keys())

    # -------- 画节点 --------
    for addr in addresses:
        is_user = addr in users_set
        shape = "diamond" if is_user else "ellipse"

        if addr in node_annotations and node_annotations[addr]:
            label = addr + "\n" + "\n".join(node_annotations[addr])
        else:
            label = addr

        dot.node(
            addr,
            label=label,
            shape=shape
        )

    # -------- 按顺序画边 --------
    paired_sorted = sorted(paired, key=lambda x: x["order"])

    for p in paired_sorted:
        token_addr = p["token_addr"]

        if token_addr not in token_color:
            token_color[token_addr] = palette[len(token_color) % len(palette)]
        color = token_color[token_addr]

        edge_label = (
            f"({p['order']}) {p['token']}: {p['amount']}\n"
        )

        dot.edge(
            p["from"],
            p["to"],
            label=edge_label,
            color=color,
            fontcolor=color
        )

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