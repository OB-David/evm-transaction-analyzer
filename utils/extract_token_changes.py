# extract_token_changes.py 负责从余额变化表格中提取代币转移事件
# 生成资产流向图的 DOT 文件

import os
from collections import defaultdict
from graphviz import Digraph

def hex_to_int_safe(x: str) -> int:
    try:
        return int(x, 16)
    except Exception:
        return 0

def balance_change(table):
    """
    从 CFGConstructor.table 构造语义级资产事件
    """
    all_changes = []
    balance_traces = defaultdict(lambda: {"SLOAD": None, "SSTORE": None})
    for row in table:
        op = row["op"]
        token_addr = row.get("token_address")
        token_name = row.get("token_name")
        user = row.get("from") if op == "SLOAD" else row.get("to")
        balance = hex_to_int_safe(row.get("balance/amount"))

        # 1. 处理 ETH 转账
        if op == "CALL" and token_name == "ETH":
            eth_value = balance
            if eth_value > 0:
                all_changes.append({
                    "type": "ETH_TRANSFER",
                    "from_address": row["from"],
                    "to_address": row["to"],
                    "eth_value": str(eth_value),
                    "pc": row["pc"]
                })

        # 2. 处理 ERC20 余额变化（SLOAD / SSTORE）
        if token_addr and user:
            if op in {"SLOAD", "SSTORE"}:
                # 更新相应的 SLOAD 或 SSTORE
                if op == "SLOAD":
                    balance_traces[(token_addr, user)]["SLOAD"] = balance
                elif op == "SSTORE":
                    balance_traces[(token_addr, user)]["SSTORE"] = balance

                # 如果两个操作都有了，计算余额差异
                if balance_traces[(token_addr, user)]["SLOAD"] is not None and balance_traces[(token_addr, user)]["SSTORE"] is not None:
                    diff = balance_traces[(token_addr, user)]["SSTORE"] - balance_traces[(token_addr, user)]["SLOAD"]
                    if diff != 0:
                        all_changes.append({
                            "type": "ERC20_BALANCE_CHANGE",
                            "erc20_token_address": token_addr,
                            "token_name": token_name,
                            "user_address": user,
                            "changed_balance": str(diff),
                            "pc": row["pc"]
                        })
                    # 重置状态，避免重复计算
                    balance_traces[(token_addr, user)]["SLOAD"] = None
                    balance_traces[(token_addr, user)]["SSTORE"] = None

    return all_changes


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
                "token_addr": "ETH"
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
                "token_addr": token_addr
            }
        else:
            prev = pending_erc20[token_addr]

            if prev["value"] + val == 0:
                sender = prev["user"] if prev["value"] < 0 else user
                receiver = user if prev["value"] < 0 else prev["user"]

                paired.append({
                    "order": prev["order"],
                    "from": sender,
                    "to": receiver,
                    "amount": abs(val),
                    "token": token_name,
                    "token_addr": token_addr
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

    return paired, node_annotations


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
            f"{p['token_addr']}"
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


