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

def format_scientific_html(value: float, precision: int = 4, sup_size: int = 8) -> str:
    if value == 0:
        return "0"
    s = f"{value:.{precision}e}"
    mantissa, exp = s.split('e')
    exp = int(exp)
    return f"{mantissa}×10<sup><font point-size='{sup_size}'>{exp}</font></sup>"

def pair_transactions(original_transfer, all_changes, token_decimals_map=None):
    """
    按 all_changes 顺序配对余额变化并确定交易顺序
    """
    paired = []
    node_annotations = defaultdict(list)
    order_counter = 0
    pending_erc20 = {}

    # 交易发起时ETH Transfer
    paired.append({
        "order": order_counter,
        "codecontract_address": None,
        "from": original_transfer[0],
        "to": original_transfer[1],
        "amount":original_transfer[2] / (10 ** 18),
        "token": "ETH",
        "token_addr": "ETH",
        "source_pcs": None,
    })

    for c in all_changes:
        # -------- ETH --------
        if c["type"] == "ETH_TRANSFER":
            formatted_val = abs(int(c["eth_value"])) / (10 ** 18)
            order_counter += 1
            paired.append({
                "order": order_counter,
                "codecontract_address": c["codecontract_address"],
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
        codecontract_address = c["codecontract_address"]

        decimals = 18
        if token_decimals_map and token_addr in token_decimals_map:
            decimals = token_decimals_map[token_addr]

        c_structured = {
            "order": order_counter,
            "codecontract_address": codecontract_address,
            "user": user,
            "value": val,
            "token": token_name,
            "token_addr": token_addr,
            "source_pcs": [c["SLOAD_pc"], c["SSTORE_pc"]],
            "decimals": decimals,
        }    

        if token_addr not in pending_erc20:
            order_counter += 1
            pending_erc20[token_addr] = {
            "order": order_counter,
            "codecontract_address": codecontract_address,
            "user": user,
            "value": val,
            "token": token_name,
            "token_addr": token_addr,
            "source_pcs": [c["SLOAD_pc"], c["SSTORE_pc"]],
            "decimals": decimals,
        }  
        else:
            prev = pending_erc20[token_addr]
            # 配对条件：金额互补
            if prev["value"] + val == 0:
                if prev["value"] < 0:
                    sender, receiver = prev, c_structured
                else:
                    sender, receiver = c_structured, prev

                formatted_val = abs(val) / (10 ** decimals)
                paired.append({
                    "order": prev["order"],
                    "from": sender.get("user"),
                    "from_codecontract": sender.get("codecontract_address"),
                    "to_codecontract": receiver.get("codecontract_address"),
                    "to": receiver.get("user"),
                    "amount": formatted_val,
                    "token": token_name,
                    "token_addr": token_addr,
                    "source_pcs": {
                        "sender_sload_pc": sender.get("source_pcs", [])[0],
                        "sender_sstore_pc": sender.get("source_pcs", [])[1],
                        "receiver_sload_pc": receiver.get("source_pcs", [])[0],
                        "receiver_sstore_pc": receiver.get("source_pcs", [])[1]
                    }
                })
                del pending_erc20[token_addr]
            else:
                # 如果金额不匹配，暂不处理（NFT通常是1:1匹配，不会进入这里）
                pass

    # 遍历结束，剩余的是孤立变化（包含所有 ERC20/NFT 的铸造和销毁）
    # 注意：此处不再过滤 WETH，也不再存入 node_annotations，统一在渲染层处理为“边”
    
    paired.sort(key=lambda x: x["order"])
    return paired, node_annotations, pending_erc20

def detect_arbitrage(paired: list, pending_erc20: dict = None) -> dict:
    from collections import defaultdict

    graph = defaultdict(list)

    for e in paired:
        frm = e.get("from")
        to  = e.get("to")
        tok = e["token"]
        ord_= e["order"]
        if frm and to and frm != to:
            graph[frm].append((to, tok, ord_))

    if pending_erc20:
        for v in pending_erc20.values():
            user       = v.get("user")
            token_addr = v.get("token_addr")
            tok        = v.get("token")
            ord_       = v.get("order")
            if not (user and token_addr and tok and ord_):
                continue
            if v["value"] > 0:
                graph[token_addr].append((user, tok, ord_))
            else:
                graph[user].append((token_addr, tok, ord_))

    node_arb_orders = defaultdict(set)

    def dfs(start, current, path_edges, visited_tokens, path_edge_keys):
        for (nxt, tok, ord_) in graph[current]:
            edge_key = (current, nxt, ord_)

            if nxt == start and len(path_edges) >= 2:
                all_tokens = visited_tokens | {tok}
                first_tok = path_edges[0][2]
                if len(all_tokens) > 1 and tok == first_tok:
                    cycle_orders = [o for (_, _, _, o) in path_edges] + [ord_]
                    node_arb_orders[start].update(cycle_orders)

            elif edge_key not in path_edge_keys and len(path_edges) < 10:
                path_edge_keys.add(edge_key)
                dfs(start, nxt,
                    path_edges + [(current, nxt, tok, ord_)],
                    visited_tokens | {tok},
                    path_edge_keys)
                path_edge_keys.discard(edge_key)

    for node in list(graph.keys()):
        dfs(node, node, [], set(), set())

    seen = set()
    unique_cycles = []
    all_arb_orders = set()

    for start_node, orders in node_arb_orders.items():
        key = frozenset(orders)
        if key not in seen:
            seen.add(key)
            unique_cycles.append(list(orders))
            all_arb_orders.update(orders)

    return {"cycles": unique_cycles, "arb_edge_orders": all_arb_orders}

def compute_address_balances(paired: list, pending_erc20: dict = None) -> dict:
    """
    计算每个地址在本次交易中各代币的净变化量。
    返回格式：
    {
        "0xcontract_to": {"USDC": -100.0, "WETH": 0.0},
        "0xUser_A":      {"USDC": +100.0},
        ...
    }
    """
    from collections import defaultdict

    # 格式：balances[address][token] = net_amount
    balances = defaultdict(lambda: defaultdict(float))

    for p in paired:
        if p.get("order", 0) == 0:
            continue
        frm   = p.get("from")
        to    = p.get("to")
        tok   = p.get("token")
        amount = p.get("amount", 0)
        if frm and tok:
            balances[frm][tok] -= amount
        if to and tok:
            balances[to][tok]  += amount

    if pending_erc20:
        for v in pending_erc20.values():
            user    = v.get("user")
            tok     = v.get("token")
            decimals = v.get("decimals", 18)
            raw_val  = v.get("value", 0)
            amount   = abs(raw_val) / (10 ** decimals)
            if not (user and tok):
                continue
            if raw_val > 0:
                balances[user][tok] += amount   # mint
            else:
                balances[user][tok] -= amount   # burn

    # 转成普通 dict 方便序列化
    return {addr: dict(tokens) for addr, tokens in balances.items()}

def render_asset_flow(paired, node_annotations, users_addresses,
                      full_address_name_map, pending_erc20, addr_color_map,
                      output_file="asset_flow.dot",
                      arb_edge_orders: set = None):
    dot = Digraph(engine="dot")
    dot.graph_attr['rankdir'] = 'LR'

    users_set = set(users_addresses)
    user_alias_map = {addr: full_address_name_map.get(addr) for addr in users_set}

    # 收集所有相关地址
    addresses = set()
    for p in paired:
        addresses.add(p["from"])
        addresses.add(p["to"])
    for v in pending_erc20.values():
        addresses.add(v["user"])
        addresses.add(v["token_addr"]) # 确保代币合约本身也被作为节点

    # 建立名称到地址的映射（用于合并同名合约节点）
    name_to_addrs = {}
    name_to_merged_color = {}
    
    for addr in addresses:
        is_user = addr in users_set
        display_name = full_address_name_map.get(addr, addr[:8] + "...")
        
        # 用户不合并，合约按名称合并
        unique_key = addr if (is_user or display_name.startswith("User_")) else display_name
        
        if unique_key not in name_to_addrs:
            name_to_addrs[unique_key] = []
            name_to_merged_color[unique_key] = addr_color_map.get(addr, "#FFFFFF")
        name_to_addrs[unique_key].append(addr)

    # 渲染节点
    for unique_key, addr_list in name_to_addrs.items():
        main_addr = addr_list[0]
        is_user = main_addr in users_set
        
        if is_user:
            shape = "diamond"
        elif any(name for addr in addr_list for name in [full_address_name_map.get(addr, "")] if not (name.startswith("contract_") or name.startswith("User_"))):
            shape = "ellipse" # 代币合约通常呈现为椭圆
        else:
            shape = "record"
        
        display_name = user_alias_map.get(main_addr, unique_key) if is_user else unique_key
        label = f"<{display_name}>"
        
        dot.node(unique_key, label=label, shape=shape, fillcolor=name_to_merged_color[unique_key], style="filled")

    def get_merged_node_id(addr):
        if addr in users_set: return addr
        display_name = full_address_name_map.get(addr, addr[:8] + "...")
        return display_name if not display_name.startswith("User_") else addr

    # 1. 绘制已配对的转账边（实线）
    for p in sorted(paired, key=lambda x: x["order"]):
        src_id = get_merged_node_id(p["from"])
        tgt_id = get_merged_node_id(p["to"])
        edge_color = addr_color_map.get(p["token_addr"] if p["token"] != "ETH" else p["from"], "#000000")
        amount_str = format_scientific_html(p["amount"])
        edge_label = f"({p['order']}) {p['token']}: {amount_str}"
        is_arb = arb_edge_orders and p["order"] in arb_edge_orders
        penwidth = "4.0" if is_arb else "1.0"
        arrowsize = "1.5" if is_arb else "0.8"
        extra_style = ", bold" if is_arb else ""
        dot.edge(src_id, tgt_id,
                 label="<" + edge_label + ">",
                 color=edge_color, fontcolor=edge_color,
                 penwidth=penwidth, arrowsize=arrowsize,
                 style="solid" + extra_style)

    # 2. 绘制所有孤立的 ERC20/NFT 变化（虚线边：表示铸造或销毁）
    for v in pending_erc20.values():
        user_id = get_merged_node_id(v["user"])
        token_id = get_merged_node_id(v["token_addr"])
        amount = abs(v["value"]) / (10 ** v["decimals"])
        amount_str = format_scientific_html(amount)
        edge_color = addr_color_map.get(v["token_addr"], "#FFFFFF")
        
        if v["value"] > 0:
            # 铸造 (Mint): 合约 -> 用户
            src, tgt = token_id, user_id
            action = "mint"
        else:
            # 销毁 (Burn): 用户 -> 合约
            src, tgt = user_id, token_id
            action = "burn"
        
        edge_label = f"({v['order']}) {v['token']}({action}): {amount_str}"
        dot.edge(src, tgt, label="<" + edge_label + ">", color=edge_color, fontcolor=edge_color, style="dashed")

    dot.save(output_file)
    return dot

# --- 后续的 afg_to_cfg 和序列化函数保持不变 ---
def afg_to_cfg(paired, pending_erc20, cfg_constructor: CFGConstructor, tx_cfg: CFG, folded_node_map):
    edge_link = []
    for p in paired:
        if p["order"] == 0:
            continue
        if p["token"] == "ETH":
            matched_node = cfg_constructor.find_node_by_pc_address(tx_cfg, p["codecontract_address"], p["source_pcs"][0])
            if matched_node is None:
                continue
            matched_block = matched_node.id
            edge_link.append({"edge_id": p["order"], "type": "ETH_TRANSFER", "matched_blocks": matched_block})
        else:
            # ERC20 Transfer 配对
            # from_codecontract_address 对应的 sload/sstore
            s_l_node = cfg_constructor.find_node_by_pc_address(tx_cfg, p["from_codecontract"], p["source_pcs"]["sender_sload_pc"])
            s_s_node = cfg_constructor.find_node_by_pc_address(tx_cfg, p["from_codecontract"], p["source_pcs"]["sender_sstore_pc"])
            r_l_node = cfg_constructor.find_node_by_pc_address(tx_cfg, p["to_codecontract"], p["source_pcs"]["receiver_sload_pc"])
            r_s_node = cfg_constructor.find_node_by_pc_address(tx_cfg, p["to_codecontract"], p["source_pcs"]["receiver_sstore_pc"])
            if any(n is None for n in (s_l_node, s_s_node, r_l_node, r_s_node)):
                continue
            s_l, s_s, r_l, r_s = s_l_node.id, s_s_node.id, r_l_node.id, r_s_node.id
            blocks = {
                "s_l": next((rid for rid, nids in folded_node_map.items() if s_l in nids), s_l),
                "s_s": next((rid for rid, nids in folded_node_map.items() if s_s in nids), s_s),
                "r_l": next((rid for rid, nids in folded_node_map.items() if r_l in nids), r_l),
                "r_s": next((rid for rid, nids in folded_node_map.items() if r_s in nids), r_s),
            }
            if all(blocks.values()):
                edge_link.append({
                    "edge_id": p["order"], "type": "ERC20_TOKEN_TRANSFER",
                    "matched_blocks": {"sender": (blocks["s_l"], blocks["s_s"]), "receiver": (blocks["r_l"], blocks["r_s"])}
                })  

    for v in pending_erc20.values():
        sload_node = cfg_constructor.find_node_by_pc_address(tx_cfg, v["token_addr"], v["source_pcs"][0])
        sstore_node = cfg_constructor.find_node_by_pc_address(tx_cfg, v["token_addr"], v["source_pcs"][1])
        if sload_node is None or sstore_node is None:
            continue
        sload_block = next((rid for rid, nids in folded_node_map.items() if sload_node.id in nids), sload_node.id)
        sstore_block = next((rid for rid, nids in folded_node_map.items() if sstore_node.id in nids), sstore_node.id)
        edge_link.append({
            "edge_id": v["order"], "type": "ERC20_BALANCE_CHANGE",
            "matched_blocks": [sload_block, sstore_block]
        })

    edge_link.sort(key=lambda x: x["edge_id"])
    return edge_link



def edge_link_to_json(edge_link):
    """
    将全ID格式的edge_link序列化为JSON字符串
    :param edge_link: afg_to_cfg函数返回的列表（全ID格式）
    :return: 格式化的JSON字符串
    """
    # 直接序列化，因为edge_link全是基础类型（ID为str/int，无对象）
    return json.dumps(
        edge_link,
        indent=4,        # 缩进4格，美观易读
        ensure_ascii=False,  # 支持特殊字符（如合约地址）
        sort_keys=False  # 保持原有字段顺序，不打乱edge_id/type等
    )