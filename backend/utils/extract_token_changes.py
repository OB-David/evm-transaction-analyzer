# extract_token_changes.py 负责从余额变化表格中提取代币转移事件
# 生成资产流向图的 DOT 文件

from collections import defaultdict
from graphviz import Digraph
from utils.cfg_structure import CFG
from typing import List, Dict
import json

THEME_FILLS = [
    "#F4B9B9",
    "#F3DAB5",
    "#F2EBB5",
    "#D2F3B4",
    "#B4F3BA",
    "#B5F2D3",
    "#B5EBF4",
    "#B6CDF3",
    "#C3B5F2",
    "#EBB8F4",
    "#F3B4DB",
    "#E2E2E2",
]

THEME_DARKS = [
    "#C79696",
    "#C9B495",
    "#C3BD90",
    "#ADC893",
    "#8EC293",
    "#89BAA2",
    "#8EBBC1",
    "#91A4C2",
    "#968CBE",
    "#B289B9",
    "#BA85A6",
    "#B6B6B6",
]

FILL_TO_DARK = {fill.lower(): dark for fill, dark in zip(THEME_FILLS, THEME_DARKS)}

def dark_accent(color: str | None, fallback: str = "#6B7280") -> str:
    if not color:
        return fallback
    normalized = color.strip().lower()[:7]
    return FILL_TO_DARK.get(normalized, fallback)

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
    【正确版】
    1. 所有ERC20变化 → 先加入pending队列
    2. 逐个尝试匹配正负抵消的变化
    3. 匹配成功 → 移除
    4. 最后剩下的 → 纯孤立变动
    5. 绝对不覆盖、不丢失
    """
    paired = []
    node_annotations = defaultdict(list)
    order_counter = 0
    pending_erc20 = []  # ✅ 改成列表
    token_queues = defaultdict(list)

    # 预扫描 ETH 转移，用于识别 wrap/unwrap 场景下的 ERC20 余额变化，避免误配成普通转账
    eth_mirror_pool = []
    for c in all_changes:
        if c.get("type") != "ETH_TRANSFER":
            continue
        eth_mirror_pool.append({
            "from": c.get("from_address"),
            "to": c.get("to_address"),
            "value": abs(int(c.get("eth_value", 0))),
            "step": c.get("step"),
            "used": False,
        })

    def _to_int_or_none(v):
        try:
            return int(v)
        except Exception:
            return None

    def _match_eth_mirror(token_addr, user_addr, raw_val, erc20_step):
        """
        识别 ERC20 变化是否与 ETH 转移构成镜像：
        - raw_val > 0: token_addr -> user 的 mint，需匹配 user -> token_addr 的 ETH
        - raw_val < 0: user -> token_addr 的 burn，需匹配 token_addr -> user 的 ETH
        命中后该 ERC20 变化应保留为 pending（mint/burn），不参与 ERC20 正负配对。
        """
        target = abs(raw_val)
        if target == 0:
            return None

        erc20_step_int = _to_int_or_none(erc20_step)
        best_idx = None
        best_dist = None

        for idx, e in enumerate(eth_mirror_pool):
            if e["used"]:
                continue
            if e["value"] != target:
                continue

            if raw_val > 0:
                # wrap: user 交 ETH 给 token 合约，用户余额增加 token
                direction_ok = (e["from"] == user_addr and e["to"] == token_addr)
            else:
                # unwrap: token 合约返 ETH 给 user，用户 token 余额减少
                direction_ok = (e["from"] == token_addr and e["to"] == user_addr)

            if not direction_ok:
                continue

            e_step_int = _to_int_or_none(e.get("step"))
            if erc20_step_int is None or e_step_int is None:
                dist = 0
            else:
                dist = abs(erc20_step_int - e_step_int)

            if best_idx is None or dist < best_dist:
                best_idx = idx
                best_dist = dist

        if best_idx is not None:
            eth_mirror_pool[best_idx]["used"] = True
        return best_idx

    # 交易发起时ETH Transfer
    paired.append({
        "order": order_counter,
        "codecontract_address": None,
        "from": original_transfer[0],
        "to": original_transfer[1],
        "amount": original_transfer[2] / (10 ** 18),
        "token": "ETH",
        "token_addr": "ETH",
        "source_pcs": None,
        "source_steps": None,
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
                "source_steps": [c["step"]],
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

        # 构建当前变化结构
        order_counter += 1
        c_structured = {
            "order": order_counter,
            "codecontract_address": codecontract_address,
            "user": user,
            "value": val,
            "token": token_name,
            "token_addr": token_addr,
            "source_pcs": [c["SLOAD_pc"], c["SSTORE_pc"]],
            "source_steps": [c["SLOAD_step"], c["SSTORE_step"]],
            "decimals": decimals,
        }

        # 优先识别 ETH<->Token 镜像变化（例如 ETH<->WETH wrap/unwrap），命中则不进入普通配对队列
        mirror_idx = _match_eth_mirror(token_addr, user, val, c.get("SSTORE_step"))
        if mirror_idx is not None:
            pending_erc20.append(c_structured)
            continue


        # 队列存储一个token下的余额变化
        token_queues[token_addr].append(c_structured)

        # 匹配
        queue = token_queues[token_addr]
        if len(queue) >= 2:
            # 取最后两笔尝试配对
            prev = queue[-2]
            curr = queue[-1]

            if prev["value"] + curr["value"] == 0:
                # 能配对 → 确定发送/接收
                if prev["value"] < 0:
                    sender, receiver = prev, curr
                else:
                    sender, receiver = curr, prev

                # 限制：不允许自己给自己转账
                if sender.get("user") == receiver.get("user"):
                    continue

                formatted_val = abs(curr["value"]) / (10 ** decimals)
                paired.append({
                    "order": sender["order"],
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
                        "receiver_sstore_pc": receiver.get("source_pcs", [])[1],
                    },
                    "source_steps": {
                        "sender_sload_step": sender.get("source_steps", [])[0],
                        "sender_sstore_step": sender.get("source_steps", [])[1],
                        "receiver_sload_step": receiver.get("source_steps", [])[0],
                        "receiver_sstore_step": receiver.get("source_steps", [])[1],
                    }
                })
                # 匹配成功 → 移除这两笔
                queue.pop()
                queue.pop()


    for q in token_queues.values():
        pending_erc20.extend(q)

    # 压缩序号，保证最终序号连续且保持原始先后关系
    combined = []
    for p in paired:
        combined.append((p.get("order", 0), 0, p, "paired"))
    for v in pending_erc20:
        combined.append((v.get("order", 0), 1, v, "pending"))

    combined.sort(key=lambda x: (x[0], x[1]))

    next_order = 0
    for _, _, item, _ in combined:
        item["order"] = next_order
        next_order += 1

    paired.sort(key=lambda x: x["order"])
    pending_erc20.sort(key=lambda x: x["order"])
    return paired, node_annotations, pending_erc20


def detect_arbitrage(paired: list, pending_erc20: list = None) -> dict:
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
        for v in pending_erc20:
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

def compute_address_balances(paired: list, pending_erc20: list = None) -> dict:
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
        frm   = p.get("from")
        to    = p.get("to")
        tok   = p.get("token")
        amount = p.get("amount", 0)
        if frm and tok:
            balances[frm][tok] -= amount
        if to and tok:
            balances[to][tok]  += amount

    if pending_erc20:
        for v in pending_erc20:
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
    dot.graph_attr.update({
        'rankdir': 'TB',
        'pad': '0.25',
        'nodesep': '0.46',
        'ranksep': '0.88',
        'splines': 'spline',
        'overlap': 'false',
        'outputorder': 'edgesfirst',
    })
    dot.node_attr.update({
        'fontname': 'Arial',
        'fontsize': '18',
        'margin': '0.18,0.12',
    })
    dot.edge_attr.update({
        'fontname': 'Arial',
        'fontsize': '14',
        'penwidth': '1.8',
        'arrowsize': '0.82',
    })

    users_set = set(users_addresses)
    user_alias_map = {addr: full_address_name_map.get(addr) for addr in users_set}

    # 收集所有相关地址
    addresses = set()
    for p in paired:
        addresses.add(p["from"])
        addresses.add(p["to"])
    for v in pending_erc20:
        addresses.add(v["user"])
        addresses.add(v["token_addr"]) # 确保代币合约本身也被作为节点

    # 建立地址到地址的映射（不去重：节点数量与地址数量一致）
    name_to_addrs = {}
    name_to_merged_color = {}
    
    for addr in addresses:
        is_user = addr in users_set
        display_name = full_address_name_map.get(addr, addr[:8] + "...")
        
        # 不合并：每个地址都单独作为一个节点 ID
        unique_key = addr
        
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

        # 节点 ID 用地址，label 展示名称（若缺失则回退到地址短写）
        display_name = full_address_name_map.get(main_addr) or user_alias_map.get(main_addr) or (main_addr[:8] + "...")
        label = f"<{display_name}>"
        fill_color = name_to_merged_color[unique_key]
        stroke_color = dark_accent(fill_color, "#2C2C2C")

        dot.node(
            unique_key,
            label=label,
            shape=shape,
            fillcolor=fill_color,
            color=stroke_color,
            fontcolor=stroke_color,
            penwidth="1.6",
            style="filled",
        )

    def get_merged_node_id(addr):
        return addr

    # 1. 绘制已配对的转账边（实线）
    for p in sorted(paired, key=lambda x: x["order"]):
        src_id = get_merged_node_id(p["from"])
        tgt_id = get_merged_node_id(p["to"])
        edge_fill = addr_color_map.get(p["token_addr"] if p["token"] != "ETH" else p["from"], "#E2E2E2")
        edge_color = dark_accent(edge_fill, "#6B7280")
        amount_str = format_scientific_html(p["amount"])
        edge_label = f"({p['order']}) {p['token']}: {amount_str}"
        is_arb = arb_edge_orders and p["order"] in arb_edge_orders
        penwidth = "3.1" if is_arb else "1.8"
        arrowsize = "1.02" if is_arb else "0.82"
        extra_style = ", bold" if is_arb else ""
        dot.edge(src_id, tgt_id,
                 label="<" + edge_label + ">",
                 color=edge_color, fontcolor=edge_color,
                 penwidth=penwidth, arrowsize=arrowsize,
                 style="solid" + extra_style)

    # 2. 绘制所有孤立的 ERC20/NFT 变化（虚线边：表示铸造或销毁）
    for v in pending_erc20:
        user_id = get_merged_node_id(v["user"])
        token_id = get_merged_node_id(v["token_addr"])
        amount = abs(v["value"]) / (10 ** v["decimals"])
        amount_str = format_scientific_html(amount)
        edge_fill = addr_color_map.get(v["token_addr"], "#E2E2E2")
        edge_color = dark_accent(edge_fill, "#6B7280")
        
        if v["value"] > 0:
            # 铸造 (Mint): 合约 -> 用户
            src, tgt = token_id, user_id
            action = "mint"
        else:
            # 销毁 (Burn): 用户 -> 合约
            src, tgt = user_id, token_id
            action = "burn"
        
        edge_label = f"({v['order']}) {v['token']}({action}): {amount_str}"
        dot.edge(
            src,
            tgt,
            label="<" + edge_label + ">",
            color=edge_color,
            fontcolor=edge_color,
            style="dashed",
            penwidth="1.5",
            arrowsize="0.78",
        )

    dot.save(output_file)
    return dot


def find_node_by_pc_address(original_cfg: CFG, folded_node_map: Dict[str, List[str]], address: str, pc: str):
    def pc_to_int(v):
        if v is None:
            return None
        try:
            if isinstance(v, int):
                return v
            s = str(v)
            if s.startswith("0x"):
                return int(s, 16)
            return int(s)
        except Exception:
            return None
        
    pc_int = pc_to_int(pc)
    if pc_int is None:
        return None
    # 第一步：在原始图中精确定位该 PC 属于哪一个原始块
    target_original_id = None
    for node in original_cfg.nodes:
        if node.address == address:
            s_int = pc_to_int(node.start_pc)
            e_int = pc_to_int(node.end_pc)
            
            if s_int is not None and e_int is not None:
                if s_int <= pc_int <= e_int:
                    target_original_id = node.id
                    break
    
    if not target_original_id:
        return None

    # 第二步：在映射表中查找该原始 ID 属于哪一个折叠后的根节点
    # folded_node_map 的 key 是根节点 ID，value 是被它吞并的所有原始节点 ID 列表
    for root_id, original_ids in folded_node_map.items():
        if target_original_id in original_ids:
            return root_id
    
    return None

# --- 后续的 afg_to_cfg 和序列化函数保持不变 ---
def afg_to_fcfg(paired, pending_erc20, tx_cfg: CFG, folded_node_map):
    edge_link = []
    for p in paired:
        if p["order"] == 0:
            continue
        if p["token"] == "ETH":
            matched_block = find_node_by_pc_address(tx_cfg, folded_node_map, p["codecontract_address"], p["source_pcs"][0])
            if matched_block:
               edge_link.append({"edge_id": p["order"], "type": "ETH_TRANSFER", "matched_blocks": matched_block})
        else:
            # ERC20 Transfer 配对
            # from_codecontract_address 对应的 sload/sstore
            s_l = find_node_by_pc_address(tx_cfg, folded_node_map, p["from_codecontract"], p["source_pcs"]["sender_sload_pc"])
            s_s = find_node_by_pc_address(tx_cfg, folded_node_map, p["from_codecontract"], p["source_pcs"]["sender_sstore_pc"])
            r_l = find_node_by_pc_address(tx_cfg, folded_node_map, p["to_codecontract"], p["source_pcs"]["receiver_sload_pc"])
            r_s = find_node_by_pc_address(tx_cfg, folded_node_map, p["to_codecontract"], p["source_pcs"]["receiver_sstore_pc"])
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

    for v in pending_erc20:
        sload_block = find_node_by_pc_address(tx_cfg, folded_node_map, v["token_addr"], v["source_pcs"][0])
        sload_block = next((rid for rid, nids in folded_node_map.items() if sload_block in nids), sload_block)
        sstore_block = find_node_by_pc_address(tx_cfg, folded_node_map, v["token_addr"], v["source_pcs"][1])
        sstore_block = next((rid for rid, nids in folded_node_map.items() if sstore_block in nids), sstore_block)
        if sload_block is None or sstore_block is None:
            continue
        edge_link.append({
            "edge_id": v["order"], "type": "ERC20_BALANCE_CHANGE",
            "matched_blocks": [sload_block, sstore_block]
        })

    edge_link.sort(key=lambda x: x["edge_id"])
    return edge_link



def find_node_by_step(cfg: CFG, step: int):
    """
    根据 step 值，匹配落在 cfg 节点 [start_step, end_step] 区间内的块
    """
    def to_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except:
            return None

    step_val = to_int(step)
    if step_val is None:
        return None

    # 遍历所有块，匹配 step 区间
    for node in cfg.nodes:
        if not hasattr(node, "fold_info"):
            continue
        
        start = node.fold_info.get("start_step", 0)
        end = node.fold_info.get("end_step", -1)

        # 区间匹配逻辑
        if end == -1:
            # 末尾 return 块：step >= start 即匹配
            if step_val >= start:
                return node.id
        else:
            # 普通块：start ≤ step ≤ end
            if start <= step_val <= end:
                return node.id

    return None


def afg_to_pcfg(paired, pending_erc20, plain_cfg):
    edge_link = []

    for p in paired:
        if p["order"] == 0:
            continue

        # ------------------------------
        # ETH 转移：从 source_steps 获取 step
        # ------------------------------
        if p["token"] == "ETH":
            # ✅ 正确：从 source_steps 列表取 step
            step_list = p.get("source_steps", [])
            step = step_list[0] if len(step_list) > 0 else None
            
            matched_block = find_node_by_step(plain_cfg, step)
            if matched_block:
                edge_link.append({
                    "edge_id": p["order"],
                    "type": "ETH_TRANSFER",
                    "matched_blocks": matched_block
                })

        else:
            source_steps = p.get("source_steps", {})
            s_l_step = source_steps.get("sender_sload_step")
            s_s_step = source_steps.get("sender_sstore_step")
            r_l_step = source_steps.get("receiver_sload_step")
            r_s_step = source_steps.get("receiver_sstore_step")

            s_l = find_node_by_step(plain_cfg, s_l_step)
            s_s = find_node_by_step(plain_cfg, s_s_step)
            r_l = find_node_by_step(plain_cfg, r_l_step)
            r_s = find_node_by_step(plain_cfg, r_s_step)

            blocks = {
                "s_l": s_l,
                "s_s": s_s,
                "r_l": r_l,
                "r_s": r_s
            }

            if all(blocks.values()):
                edge_link.append({
                    "edge_id": p["order"],
                    "type": "ERC20_TOKEN_TRANSFER",
                    "matched_blocks": {
                        "sender": (blocks["s_l"], blocks["s_s"]),
                        "receiver": (blocks["r_l"], blocks["r_s"])
                    }
                })

    for v in pending_erc20:
        # ✅ 正确：从 source_steps 取（不是 source_pcs）
        source_steps = v.get("source_steps", [])
        if len(source_steps) < 2:
            continue
        
        sload_step = source_steps[0]
        sstore_step = source_steps[1]

        sload_block = find_node_by_step(plain_cfg, sload_step)
        sstore_block = find_node_by_step(plain_cfg, sstore_step)

        if sload_block is None or sstore_block is None:
            continue

        edge_link.append({
            "edge_id": v["order"],
            "type": "ERC20_BALANCE_CHANGE",
            "matched_blocks": [sload_block, sstore_block]
        })

    edge_link.sort(key=lambda x: x["edge_id"])
    return edge_link

def edge_link_to_json(edge_link):
    return json.dumps(
        edge_link,
        indent=4,        # 缩进4格，美观易读
        ensure_ascii=False,  # 支持特殊字符（如合约地址）
        sort_keys=False  # 保持原有字段顺序，不打乱edge_id/type等
    )
