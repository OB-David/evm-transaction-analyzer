# extract_token_changes.py 负责从余额变化表格中提取代币转移事件
# 生成资产流向图的 DOT 文件

from collections import defaultdict
from graphviz import Digraph
from utils.cfg_structure import CFG
from typing import Any, Dict, List, Optional, Tuple
import json
import re

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
    match = re.fullmatch(r"#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", color.strip())
    if not match:
        return fallback
    normalized = f"#{match.group(1).lower()}"
    configured = FILL_TO_DARK.get(normalized)
    if configured:
        return configured
    channels = [int(match.group(1)[offset:offset + 2], 16) for offset in (0, 2, 4)]
    return "#" + "".join(f"{round(channel * 0.78):02X}" for channel in channels)

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
    original_value = int(original_transfer[2])
    # order=0 专用于非零的顶层 ETH 转移；其余资产变化始终从 order=1 开始。
    order_counter = 0
    pending_erc20 = []  # ✅ 改成列表
    token_queues = defaultdict(list)

    # 只保留执行到当前 step 时已经出现的 ETH，避免 WETH 变化抢占未来的 unwrap。
    eth_mirror_pool = []

    def _to_int_or_none(v):
        try:
            return int(v)
        except Exception:
            return None

    def _match_prior_wrap(token_addr, user_addr, raw_val, erc20_step):
        """
        将 WETH 正变化与先前 user -> token 的等额 ETH 匹配为 wrap。

        unwrap 在后续 ETH 转出到达时反向回看 WETH 负变化，
        不在这里预览未来事件。
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

            direction_ok = e["from"] == user_addr and e["to"] == token_addr

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

    # 交易发起时的 ETH Transfer。零 value 只是一次普通合约调用，不应生成资产流边。
    if original_value != 0:
        paired.append({
            "order": order_counter,
            "codecontract_address": None,
            "from": original_transfer[0],
            "to": original_transfer[1],
            "amount": original_value / (10 ** 18),
            "amount_raw": abs(original_value),
            "token": "ETH",
            "token_addr": "ETH",
            "decimals": 18,
            "source_pcs": None,
            "source_steps": None,
        })

    def _change_step(change):
        step = (
            change.get("step")
            if change.get("type") == "ETH_TRANSFER"
            else change.get("SSTORE_step")
        )
        parsed = _to_int_or_none(step)
        return parsed if parsed is not None else 10**30

    # 以真实执行 step 为唯一时序；相同/缺失 step 保留原始顺序。
    ordered_changes = [
        change for _, change in sorted(
            enumerate(all_changes),
            key=lambda item: (_change_step(item[1]), item[0]),
        )
    ]

    for c in ordered_changes:
        # -------- ETH --------
        if c["type"] == "ETH_TRANSFER":
            raw_eth_value = abs(int(c["eth_value"]))
            formatted_val = raw_eth_value / (10 ** 18)

            # unwrap 的 WETH 减少先于 ETH 转出。只在已经执行的
            # token 变化中找最近一笔，然后将它从普通 transfer 队列转为 burn。
            token_addr = c.get("from_address")
            eth_step = _to_int_or_none(c.get("step"))
            unwrap_candidates = [
                change for change in token_queues.get(token_addr, [])
                if change.get("user") == c.get("to_address")
                and change.get("value") == -raw_eth_value
                and (
                    eth_step is None
                    or _to_int_or_none(change.get("source_steps", [None, None])[1]) is None
                    or _to_int_or_none(change.get("source_steps", [None, None])[1]) <= eth_step
                )
            ]
            unwrap_change = max(
                unwrap_candidates,
                key=lambda change: _to_int_or_none(
                    change.get("source_steps", [None, None])[1]
                ) or -1,
                default=None,
            )
            if unwrap_change is not None:
                token_queues[token_addr].remove(unwrap_change)
                pending_erc20.append(unwrap_change)

            order_counter += 1
            paired.append({
                "order": order_counter,
                "codecontract_address": c["codecontract_address"],
                "from": c["from_address"],
                "to": c["to_address"],
                "amount": formatted_val,
                "amount_raw": abs(int(c["eth_value"])),
                "token": "ETH",
                "token_addr": "ETH",
                "decimals": 18,
                "source_pcs": [c["pc"]],
                "source_steps": [c["step"]],
            })
            if unwrap_change is None:
                eth_mirror_pool.append({
                    "from": c.get("from_address"),
                    "to": c.get("to_address"),
                    "value": raw_eth_value,
                    "step": c.get("step"),
                    "used": False,
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
        mirror_idx = (
            _match_prior_wrap(token_addr, user, val, c.get("SSTORE_step"))
            if val > 0
            else None
        )
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
                    "amount_raw": abs(curr["value"]),
                    "token": token_name,
                    "token_addr": token_addr,
                    "decimals": decimals,
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

    # order=0 只属于非零顶层 ETH 转移；不存在该边时保留 0 号空位。
    next_order = 0 if original_value != 0 else 1
    for _, _, item, _ in combined:
        item["order"] = next_order
        next_order += 1

    paired.sort(key=lambda x: x["order"])
    pending_erc20.sort(key=lambda x: x["order"])
    return paired, node_annotations, pending_erc20


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


def build_balance_timeline(paired: list, pending_erc20: list = None) -> dict:
    """Build the canonical per-transfer balance deltas used by TFG playback.

    Raw integer amounts are serialized as decimal strings so the browser can
    accumulate them with ``BigInt`` without losing ERC-20 precision. Mint and
    burn events only change the holder balance; the token contract is a supply
    boundary, not the counterparty of an ordinary transfer.
    """
    events = []

    def normalize_address(value):
        return value.lower() if isinstance(value, str) else value

    def normalize_token_address(value):
        if not isinstance(value, str):
            return value
        return "eth" if value.lower() == "eth" else value.lower()

    for transfer in paired or []:
        raw_amount = abs(int(transfer.get("amount_raw", 0)))
        from_address = normalize_address(transfer.get("from"))
        to_address = normalize_address(transfer.get("to"))
        deltas = []
        if from_address:
            deltas.append({"address": from_address, "amount_raw": str(-raw_amount)})
        if to_address:
            deltas.append({"address": to_address, "amount_raw": str(raw_amount)})
        events.append({
            "order": int(transfer.get("order", 0)),
            "kind": "transfer",
            "token_address": normalize_token_address(transfer.get("token_addr")),
            "token_name": transfer.get("token") or "Unknown token",
            "decimals": int(transfer.get("decimals", 18)),
            "amount_raw": str(raw_amount),
            "deltas": deltas,
        })

    for change in pending_erc20 or []:
        raw_delta = int(change.get("value", 0))
        user_address = normalize_address(change.get("user"))
        events.append({
            "order": int(change.get("order", 0)),
            "kind": "mint" if raw_delta > 0 else "burn",
            "token_address": normalize_token_address(change.get("token_addr")),
            "token_name": change.get("token") or "Unknown token",
            "decimals": int(change.get("decimals", 18)),
            "amount_raw": str(abs(raw_delta)),
            "deltas": (
                [{"address": user_address, "amount_raw": str(raw_delta)}]
                if user_address else []
            ),
        })

    events.sort(key=lambda event: event["order"])
    return {"schema_version": 1, "events": events}


def collect_asset_flow_addresses(paired, pending_erc20):
    """Collect the exact node addresses represented in the asset-flow graph."""
    addresses = set()
    for transfer in paired:
        for field in ("from", "to"):
            address = transfer.get(field)
            if address:
                addresses.add(address)
    for change in pending_erc20:
        for field in ("user", "token_addr"):
            address = change.get(field)
            if address:
                addresses.add(address)
    return addresses


def filter_asset_flow_user_addresses(users_addresses, paired, pending_erc20):
    """Keep only standardized users that are actual asset-flow graph nodes."""
    asset_flow_addresses = {
        address.lower()
        for address in collect_asset_flow_addresses(paired, pending_erc20)
        if isinstance(address, str)
    }
    return [
        address
        for address in users_addresses
        if isinstance(address, str) and address.lower() in asset_flow_addresses
    ]


def render_asset_flow(paired, node_annotations, users_addresses,
                      full_address_name_map, pending_erc20, addr_color_map,
                      output_file="asset_flow.dot",
                      arb_edge_orders: set = None,
                      erc20_token_map: Optional[Dict[str, Any]] = None):
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
    erc20_addresses = {
        str(address).lower() for address in (erc20_token_map or {})
    }
    user_alias_map = {addr: full_address_name_map.get(addr) for addr in users_set}

    # 和 legend 共用同一套 TFG 节点收集语义。
    addresses = collect_asset_flow_addresses(paired, pending_erc20)

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
        elif main_addr.lower() in erc20_addresses:
            shape = "ellipse"
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
        edge_fill = addr_color_map.get(p["token_addr"], "#E2E2E2")
        edge_color = "#000000" if p["token"] == "ETH" else dark_accent(edge_fill, "#6B7280")
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
        is_arb = arb_edge_orders and v["order"] in arb_edge_orders
        dot.edge(
            src,
            tgt,
            label="<" + edge_label + ">",
            color=edge_color,
            fontcolor=edge_color,
            # Keep the supply-boundary dash while adding the same accessible
            # width emphasis used by ordinary candidate-path transfers.
            style="dashed, bold" if is_arb else "dashed",
            penwidth="3.1" if is_arb else "1.5",
            arrowsize="1.02" if is_arb else "0.78",
        )

    dot.save(output_file)
    return dot


FoldedStepIndex = List[Tuple[int, int, Any]]


def build_folded_step_index(folded_cfg: CFG) -> FoldedStepIndex:
    """Return inclusive execution intervals that point directly to final folded nodes."""
    index: FoldedStepIndex = []
    for node in folded_cfg.nodes:
        fold_info = getattr(node, "fold_info", {})
        for step_range in fold_info.get("step_ranges", []):
            if not isinstance(step_range, dict):
                continue
            try:
                start = int(step_range["start_step"])
                end = int(step_range["end_step"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= end:
                index.append((start, end, node.id))
    index.sort(key=lambda item: (item[0], item[1], str(item[2])))
    return index


def find_folded_node_by_step(
    step_index: FoldedStepIndex,
    step: Any,
) -> Tuple[Optional[Any], str, Optional[int]]:
    try:
        step_value = int(step)
    except (TypeError, ValueError):
        return None, "unmatched", None

    matches = list(dict.fromkeys(
        node_id
        for start, end, node_id in step_index
        if start <= step_value <= end
    ))
    if len(matches) == 1:
        return matches[0], "matched", step_value
    if len(matches) > 1:
        return None, "ambiguous", step_value
    return None, "unmatched", step_value


def _build_folded_evidence(
    step_index: FoldedStepIndex,
    role: str,
    step: Any,
) -> Dict[str, Any]:
    block_id, status, source_step = find_folded_node_by_step(step_index, step)
    return {
        "role": role,
        "source_step": source_step,
        "block_id": block_id,
        "status": status,
    }


def _mapping_status(evidence: List[Dict[str, Any]]) -> str:
    statuses = [entry["status"] for entry in evidence]
    if "ambiguous" in statuses:
        return "ambiguous"
    matched_count = statuses.count("matched")
    if matched_count == len(statuses) and statuses:
        return "complete"
    if matched_count > 0:
        return "partial"
    return "unmatched"


def _unique_matched_blocks(evidence: List[Dict[str, Any]]) -> List[Any]:
    return list(dict.fromkeys(
        entry["block_id"]
        for entry in evidence
        if entry.get("status") == "matched" and entry.get("block_id") is not None
    ))


def afg_to_fcfg(paired, pending_erc20, folded_cfg: CFG):
    step_index = build_folded_step_index(folded_cfg)
    edge_link = []
    for p in paired:
        if p["order"] == 0:
            continue
        if p["token"] == "ETH":
            source_steps = p.get("source_steps") or []
            evidence = [_build_folded_evidence(
                step_index,
                "eth_transfer",
                source_steps[0] if source_steps else None,
            )]
            matched = _unique_matched_blocks(evidence)
            edge_link.append({
                "schema_version": 2,
                "edge_id": p["order"],
                "type": "ETH_TRANSFER",
                "mapping_status": _mapping_status(evidence),
                "matched_blocks": matched[0] if len(matched) == 1 else matched,
                "evidence": evidence,
            })
        else:
            source_steps = p.get("source_steps") or {}
            sender_evidence = [
                _build_folded_evidence(step_index, "sender_sload", source_steps.get("sender_sload_step")),
                _build_folded_evidence(step_index, "sender_sstore", source_steps.get("sender_sstore_step")),
            ]
            receiver_evidence = [
                _build_folded_evidence(step_index, "receiver_sload", source_steps.get("receiver_sload_step")),
                _build_folded_evidence(step_index, "receiver_sstore", source_steps.get("receiver_sstore_step")),
            ]
            evidence = sender_evidence + receiver_evidence
            edge_link.append({
                "schema_version": 2,
                "edge_id": p["order"],
                "type": "ERC20_TOKEN_TRANSFER",
                "mapping_status": _mapping_status(evidence),
                "matched_blocks": {
                    "sender": _unique_matched_blocks(sender_evidence),
                    "receiver": _unique_matched_blocks(receiver_evidence),
                },
                "evidence": evidence,
            })

    for v in pending_erc20:
        source_steps = v.get("source_steps") or []
        evidence = [
            _build_folded_evidence(step_index, "balance_sload", source_steps[0] if len(source_steps) > 0 else None),
            _build_folded_evidence(step_index, "balance_sstore", source_steps[1] if len(source_steps) > 1 else None),
        ]
        edge_link.append({
            "schema_version": 2,
            "edge_id": v["order"],
            "type": "ERC20_BALANCE_CHANGE",
            "mapping_status": _mapping_status(evidence),
            "matched_blocks": _unique_matched_blocks(evidence),
            "evidence": evidence,
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


def _build_call_tree_step_index(trace_tree: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """Build the same preorder call IDs used by ``build_call_tree_payload``."""
    root_address = str(trace_tree.get("contract") or "").strip()
    frames: List[Dict[str, Any]] = []

    def visit(node: Dict[str, Any], depth: int) -> None:
        call_id = len(frames) + 1
        try:
            entry_step = int(node.get("entry_step", 0))
            exit_step = int(node.get("exit_step", 0))
        except (TypeError, ValueError):
            entry_step = 0
            exit_step = -1
        frames.append({
            "call_id": call_id,
            "depth": depth,
            "entry_step": entry_step,
            "exit_step": exit_step,
            "contract_address": str(node.get("contract") or "").strip(),
        })
        children = node.get("calls", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child, depth + 1)

    children = trace_tree.get("calls", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                visit(child, 1)
    return root_address, frames


def _build_call_tree_evidence(
    root_address: str,
    frames: List[Dict[str, Any]],
    role: str,
    step: Any,
) -> Dict[str, Any]:
    try:
        source_step = int(step)
    except (TypeError, ValueError):
        return {
            "role": role,
            "source_step": None,
            "call_id": None,
            "contract_address": None,
            "status": "unmatched",
        }

    containing = [
        frame for frame in frames
        if frame["entry_step"] <= source_step <= frame["exit_step"]
    ]
    if containing:
        deepest = max(frame["depth"] for frame in containing)
        matches = [frame for frame in containing if frame["depth"] == deepest]
        if len(matches) != 1:
            return {
                "role": role,
                "source_step": source_step,
                "call_id": None,
                "contract_address": None,
                "status": "ambiguous",
            }
        match = matches[0]
        return {
            "role": role,
            "source_step": source_step,
            "call_id": match["call_id"],
            "contract_address": match["contract_address"],
            "status": "matched",
        }

    # Steps not enclosed by a child call execute in the transaction root contract.
    if root_address:
        return {
            "role": role,
            "source_step": source_step,
            "call_id": None,
            "contract_address": root_address,
            "status": "matched",
        }
    return {
        "role": role,
        "source_step": source_step,
        "call_id": None,
        "contract_address": None,
        "status": "unmatched",
    }


def _unique_matched_calls(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    matched: List[Dict[str, Any]] = []
    seen = set()
    for entry in evidence:
        if entry.get("status") != "matched" or not entry.get("contract_address"):
            continue
        key = (entry.get("call_id"), str(entry["contract_address"]).lower())
        if key in seen:
            continue
        seen.add(key)
        matched.append({
            "call_id": entry.get("call_id"),
            "contract_address": entry["contract_address"],
        })
    return matched


def afg_to_call_tree(paired, pending_erc20, trace_tree: Dict[str, Any]):
    """Map each TFG transfer edge's source steps to its deepest call frames."""
    root_address, frames = _build_call_tree_step_index(trace_tree)
    edge_links = []

    def append_link(edge_id: int, edge_type: str, role_steps: List[Tuple[str, Any]]) -> None:
        evidence = [
            _build_call_tree_evidence(root_address, frames, role, step)
            for role, step in role_steps
        ]
        matched_calls = _unique_matched_calls(evidence)
        matched_contracts = list(dict.fromkeys(
            str(call["contract_address"]).lower() for call in matched_calls
        ))
        edge_links.append({
            "schema_version": 1,
            "edge_id": edge_id,
            "type": edge_type,
            "mapping_status": _mapping_status(evidence),
            "matched_calls": matched_calls,
            "matched_contracts": matched_contracts,
            "evidence": evidence,
        })

    for transfer in paired:
        if transfer["order"] == 0:
            continue
        if transfer["token"] == "ETH":
            steps = transfer.get("source_steps") or []
            append_link(
                transfer["order"],
                "ETH_TRANSFER",
                [("eth_transfer", steps[0] if steps else None)],
            )
        else:
            steps = transfer.get("source_steps") or {}
            append_link(transfer["order"], "ERC20_TOKEN_TRANSFER", [
                ("sender_sload", steps.get("sender_sload_step")),
                ("sender_sstore", steps.get("sender_sstore_step")),
                ("receiver_sload", steps.get("receiver_sload_step")),
                ("receiver_sstore", steps.get("receiver_sstore_step")),
            ])

    for change in pending_erc20:
        steps = change.get("source_steps") or []
        append_link(change["order"], "ERC20_BALANCE_CHANGE", [
            ("balance_sload", steps[0] if len(steps) > 0 else None),
            ("balance_sstore", steps[1] if len(steps) > 1 else None),
        ])

    edge_links.sort(key=lambda item: item["edge_id"])
    return edge_links

LINK_ARTIFACT_SCHEMA_VERSION = 1


def build_link_artifact(folded_links, plain_links, call_tree_links=None):
    """Build the single persisted TFG-to-CFG/call-tree linkage contract."""
    return {
        "schema_version": LINK_ARTIFACT_SCHEMA_VERSION,
        "edge_links": {
            "folded": folded_links,
            "plain": plain_links,
            "call_tree": call_tree_links or [],
        },
    }
