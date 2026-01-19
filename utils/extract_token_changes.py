import os
import json
from graphviz import Digraph

def normalize_hex_addr(val):
    """标准化地址格式：0x + 40位十六进制"""
    if not val: return "unknown"
    addr = val.lower().replace("0x", "")
    return "0x" + addr.zfill(40)[-40:]

def normalize_hex_32(val):
    """将 hex 字符串统一为 64 位长度，用于 slot 匹配"""
    if not val: return ""
    v = val.lower().replace("0x", "")
    return v.zfill(64)

def render_balance_flow(all_changes, result_dir):
    """
    新增功能：生成可视化资产流向图
    包含统一编号系统，区分 ETH 转移、配对 ERC20 转账和孤立变动。
    输出为 DOT 文件（.dot），不生成 PNG。
    不同 ERC20 token 使用不同颜色的边（ETH 保持 gold）。
    """
    dot = Digraph(comment='Asset Flow')
    dot.attr(rankdir='LR', size='20,20')
    
    # 1. 收集所有节点
    nodes = set()
    for change in all_changes:
        if change["type"] == "ETH_TRANSFER":
            nodes.add(change["from_address"])
            nodes.add(change["to_address"])
        elif change["type"] == "ERC20_BALANCE_CHANGE":
            nodes.add(change["user_address"])
    
    node_annotations = {node: [] for node in nodes}
    global_counter = 1
    processed_erc20_indices = {}  # 存储被判定为 pair 的索引信息

    # 2. 预扫描：识别 ERC20 配对 (Pair)
    token_groups = {}
    for i, change in enumerate(all_changes):
        if change["type"] == "ERC20_BALANCE_CHANGE":
            t_addr = change["erc20_token_address"]
            if t_addr not in token_groups:
                token_groups[t_addr] = []
            token_groups[t_addr].append((i, change))

    for t_addr, group in token_groups.items():
        matched_in_group = set()
        for i in range(len(group)):
            if i in matched_in_group:
                continue
            idx_a, change_a = group[i]
            val_a = int(change_a["changed_balance"])
            if val_a == 0:
                continue

            for j in range(i + 1, len(group)):
                if j in matched_in_group:
                    continue
                idx_b, change_b = group[j]
                val_b = int(change_b["changed_balance"])

                # 如果同一代币组内两项 balance 之和为 0，视为转账
                if val_a + val_b == 0:
                    sender = change_a["user_address"] if val_a < 0 else change_b["user_address"]
                    receiver = change_b["user_address"] if val_a < 0 else change_a["user_address"]
                    # 保存更多信息以便绘制时区分 token 颜色
                    processed_erc20_indices[idx_a] = {
                        "type": "pair",
                        "peer": idx_b,
                        "from": sender,
                        "to": receiver,
                        "token_name": change_a.get("token_name"),
                        "token_addr": t_addr
                    }
                    processed_erc20_indices[idx_b] = {
                        "type": "pair",
                        "peer": idx_a,
                        "token_name": change_b.get("token_name"),
                        "token_addr": t_addr
                    }
                    matched_in_group.add(i)
                    matched_in_group.add(j)
                    break

    # 颜色调色板（可扩展）
    color_palette = [
        "blue", "green", "red", "purple", "orange", "teal", "brown", "magenta", "cyan", "darkgreen"
    ]
    token_color_map = {}

    # 3. 按顺序生成图表元素（边与标注）
    for idx, change in enumerate(all_changes):
        if change["type"] == "ETH_TRANSFER":
            label = f"({global_counter}) ETH: {int(change['eth_value'])}"
            dot.edge(change["from_address"], change["to_address"], label=label, color="gold", fontcolor="darkgoldenrod")
            global_counter += 1

        elif change["type"] == "ERC20_BALANCE_CHANGE":
            if idx in processed_erc20_indices:
                p_info = processed_erc20_indices[idx]
                if "from" in p_info:  # 仅处理 Pair 的发起方以画边
                    amount = abs(int(change["changed_balance"]))
                    label = f"({global_counter}) {change['token_name']}: {amount}"
                    token_key = p_info.get("token_addr") or p_info.get("token_name")
                    if token_key not in token_color_map:
                        token_color_map[token_key] = color_palette[len(token_color_map) % len(color_palette)]
                    color = token_color_map[token_key]
                    dot.edge(p_info["from"], p_info["to"], label=label, color=color, fontcolor=color)
                    global_counter += 1
            else:
                # 孤立变动：记录到节点的标注列表中
                user = change["user_address"]
                token = change["token_name"]
                val = int(change["changed_balance"])
                sign = "+" if val > 0 else ""
                node_annotations[user].append(f"({global_counter}) {token}: {sign}{val}")
                global_counter += 1

    # 4. 绘制节点
    for node in nodes:
        addr_short = node[:10] + "..." + node[-8:]
        label = addr_short
        if node_annotations[node]:
            label += "\n" + "\n".join(node_annotations[node])
        dot.node(node, label=label, shape="box", style="rounded")

    # 5. 输出为 DOT 文件
    output_path = os.path.join(result_dir, "asset_flow_chart.dot")
    dot.save(output_path)
    print(f"🎨 资产流向图 DOT 文件已保存至: {output_path}")

def extract_token_changes(standardized_trace, erc20_token_map, slot_map, result_dir):
    """
    保留原有逻辑并集成绘图功能
    """
    steps = standardized_trace.get('steps', [])
    simplified_steps = []
    normalized_slot_map = {normalize_hex_32(k): v for k, v in slot_map.items()}
    
    # --- 第一阶段：原有简化 Trace 提取 ---
    for i, step in enumerate(steps):
        opcode = step.get('opcode', '').upper()
        if opcode in ["CALL", "STATICCALL", "DELEGATECALL", "CALLCODE", "SSTORE", "SLOAD"]:
            contract_addr = step.get('address', '').lower()
            pc = step.get('pc')
            stack = step.get('stack', []) or []
            info = {"opcode": opcode, "contract_address": contract_addr, "pc": pc}
            
            if opcode in ["CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"]:
                if len(stack) >= 2:
                    info["call_addr"] = normalize_hex_addr(stack[-2])
                    if opcode in ["CALL", "CALLCODE"] and len(stack) >= 3:
                        try: info["call_value"] = int(stack[-3], 16)
                        except: info["call_value"] = 0
                    else: info["call_value"] = 0
                else:
                    info["call_addr"] = "unknown"; info["call_value"] = 0

            elif opcode in ["SSTORE", "SLOAD"]:
                if len(stack) >= 1:
                    slot_norm = normalize_hex_32(stack[-1])
                    user_addr = normalized_slot_map.get(slot_norm, "unknown")
                    info["user_address"] = user_addr
                    info["slot"] = "0x" + slot_norm
                    if opcode == "SSTORE":
                        info["balance"] = stack[-2] if len(stack) >= 2 else "0x0"
                    else:
                        if i + 1 < len(steps):
                            next_stack = steps[i+1].get('stack', []) or []
                            info["balance"] = next_stack[-1] if next_stack else "0x0"
                        else: info["balance"] = "0x0"
            simplified_steps.append(info)

    with open(os.path.join(result_dir, "simplified_trace.json"), "w", encoding="utf-8") as f:
        json.dump(simplified_steps, f, indent=2)

    # --- 第二阶段：原有余额逻辑提取 ---
    all_changes = []
    idx = 0
    total = len(simplified_steps)
    while idx < total:
        current_step = simplified_steps[idx]
        current_contract = current_step['contract_address']
        
        if current_step.get("call_value", 0) > 0:
            all_changes.append({
                "type": "ETH_TRANSFER", "from_address": current_contract,
                "to_address": current_step["call_addr"], "eth_value": str(current_step["call_value"]),
                "pc": current_step["pc"]
            })

        if current_contract in erc20_token_map:
            group = []
            j = idx
            while j < total and simplified_steps[j]['contract_address'] == current_contract:
                if j > idx and simplified_steps[j].get("call_value", 0) > 0:
                    all_changes.append({
                        "type": "ETH_TRANSFER", "from_address": current_contract,
                        "to_address": simplified_steps[j]["call_addr"],
                        "eth_value": str(simplified_steps[j]["call_value"]), "pc": simplified_steps[j]["pc"]
                    })
                group.append(simplified_steps[j])
                j += 1
            
            token_name = erc20_token_map[current_contract]
            for s_idx, step in enumerate(group):
                if step['opcode'] == "SSTORE" and step.get("user_address") != "unknown":
                    target_user = step["user_address"]
                    sstore_val = int(step.get("balance", "0x0"), 16)
                    sload_val = None
                    for prev_idx in range(s_idx - 1, -1, -1):
                        prev = group[prev_idx]
                        if prev['opcode'] == "SLOAD" and prev.get("user_address") == target_user:
                            sload_val = int(prev.get("balance", "0x0"), 16); break
                    if sload_val is not None:
                        diff = sstore_val - sload_val
                        all_changes.append({
                            "type": "ERC20_BALANCE_CHANGE", "erc20_token_address": current_contract,
                            "token_name": token_name, "user_address": target_user,
                            "changed_balance": str(diff), "pc": step["pc"]
                        })
            idx = j
        else: idx += 1

    # 保存 JSON 结果
    output_path = os.path.join(result_dir, "balance_and_eth_changes.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_changes, f, indent=4, ensure_ascii=False)

    # --- 第三阶段：新增绘图功能调用 ---
    render_balance_flow(all_changes, result_dir)
    
    return all_changes