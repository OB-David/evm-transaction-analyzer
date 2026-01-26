# cfg_transaction.py负责构建整个transaction的CFG图
# 包含连接逻辑
# 包含TransactionCFG渲染
# 包含根据SLOAD/SSTORE/CALL生成余额变化表格

from typing import List, Dict, Tuple, Optional, Set, Any
from utils.evm_information import StandardizedTrace, StandardizedStep
from utils.basic_block import Block, BasicBlockProcessor
from utils.cfg_structure import CFG, BlockNode, Edge

def normalize_address(address: str) -> str:
    address_str = str(address).strip().lower().replace("0x0x", "0x")
    body = address_str[2:] if address_str.startswith("0x") else address_str

    if len(body) > 40:
        body = body[-40:]
    if len(body) < 40:
        body = body.zfill(40)

    full_address = "0x" + body
    return full_address


class CFGConstructor:
    def __init__(self, all_base_blocks: List[Block]):
        # 基础块索引：(address, start_pc) -> 基础块（包含完整指令列表）
        self.base_block_map: Dict[Tuple[str, str], Block] = {}
        for block in all_base_blocks:
            key = (block.address, block.start_pc)
            self.base_block_map[key] = block

        # 触发广义跳转的 opcode（与 basic_block.py 保持一致）
        self.split_opcodes = {
            "JUMP", "JUMPI", "CALL", "CALLCODE", "DELEGATECALL", "STATICCALL",
            "CREATE", "CREATE2", "STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"
        }
        
        # 跳转相关的opcode
        self.jump_opcodes = {"JUMP", "JUMPI"}

        # 用于存储token表格数据
        self.table = []  #  pc, opcode, from, to, token_name, token_address, balance/amount

    def _find_base_block(self, address: str, pc: str) -> Block:
        """通过 address 和 start_pc 查找基础块（确保返回包含完整指令的块）"""
        key = (address, pc)
        if key in self.base_block_map:
            return self.base_block_map[key]
        raise ValueError(f"未找到 address={address} 且 start_pc={pc} 的基础块")
    
    def _find_block_by_end_pc(self, address: str, end_pc: str) -> Optional[Block]:
        """通过地址和结束pc查找对应的块"""
        for block in self.base_block_map.values():
            if block.address == address and block.end_pc == end_pc:
                return block
        return None

    def _get_step_gas_decimal(self, step: StandardizedStep) -> int:
        """
        从 step 中读取十进制 gas 值（只支持十进制 int 或十进制字符串）。
        如果不存在或无法解析，返回 0。
        仅支持字段名：'gascost'
        """
        raw = step.get("gascost")
        if raw is None:
            return 0

        if isinstance(raw, int):
            return raw

        return 0

    def _normalize_hex_value(self, val: str) -> str:
        """标准化十六进制值（统一小写、补0x前缀）"""
        if not val:
            return "0x0"
        val_str = str(val).lower()
        if not val_str.startswith("0x"):
            return f"0x{val_str}"
        return val_str

    def _hex_to_int_safe(self, hex_str: str) -> Optional[int]:
        """安全将十六进制字符串转为整数（失败返回None）"""
        try:
            return int(self._normalize_hex_value(hex_str).lstrip("0x"), 16)
        except (ValueError, TypeError):
            return None
    
    def _get_token_name_by_address(self, address: str, erc20_token_map: Dict[str, str]) -> str:
        """
        根据合约地址从erc20_token_map匹配token名称（仅SLOAD/SSTORE使用）
        :param address: 合约地址（标准化）
        :param erc20_token_map: 地址->token名称映射
        :return: token名称（未匹配到返回空字符串）
        """
        if not address or not erc20_token_map:
            return ""
        norm_addr = address.lower()
        return erc20_token_map.get(norm_addr, "")

    def construct_cfg(self, trace: Dict[str, Any], slot_map: Dict[str, str], erc20_token_map: Dict[str, str]) -> CFG:
        """
        构建CFG（仅新增erc20_token_map参数，用于SLOAD/SSTORE的token匹配）
        :param trace: 标准化trace
        :param slot_map: slot->address映射
        :param erc20_token_map: ERC20地址->token名称映射（仅SLOAD/SSTORE使用）
        :return: CFG
        """
        cfg = CFG(tx_hash=trace["tx_hash"])
        steps = trace["steps"]
        if not steps:
            return cfg

        processed_nodes: Dict[Tuple[str, str], BlockNode] = {}  # 复用节点
        current_step_idx = 0  # 当前处理的 step 索引

        # 块级临时存储
        block_temp_data: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # 处理第一个块
        first_step = steps[current_step_idx]
        try:
            current_base_block = self._find_base_block(
                address=first_step["address"],
                pc=first_step["pc"]
            )
        except ValueError as e:
            raise RuntimeError(f"初始化第一个块失败：{e}")

        # 初始化第一个节点和临时数据（ ）
        current_node_key = (current_base_block.address, current_base_block.start_pc)
        current_node = BlockNode(current_base_block)
        processed_nodes[current_node_key] = current_node
        cfg.add_node(current_node)
        block_temp_data[current_node_key] = {
            "action_type": set(),  # 存储read/write
            "send_eth": "no",      # 默认no
            "eth_events": [],      # ETH事件列表
            "erc20_events": []     # ERC20事件列表
        }

        # 遍历 trace，按块处理（ ，仅修改SLOAD/SSTORE部分）
        while current_step_idx < len(steps):
            current_step = steps[current_step_idx]
            current_pc = current_step.get("pc", "")
            current_opcode = current_step["opcode"]
            current_stack = current_step.get("stack", [])
            current_address = current_step["address"]

            # 1. 特殊处理JUMPDEST
            if current_opcode == "JUMPDEST":
                try:
                    current_jumpdest_block = self._find_base_block(
                        address=current_step["address"],
                        pc=current_step["pc"]
                    )
                except ValueError as e:
                    print(f"警告：JUMPDEST 对应的块未找到：{e}")
                    current_jumpdest_block = None

                if current_jumpdest_block:
                    current_jumpdest_node_key = (current_jumpdest_block.address, current_jumpdest_block.start_pc)
                    # 初始化新块的临时数据（ ）
                    if current_jumpdest_node_key not in processed_nodes:
                        jumpdest_node = BlockNode(current_jumpdest_block)
                        processed_nodes[current_jumpdest_node_key] = jumpdest_node
                        cfg.add_node(jumpdest_node)
                        block_temp_data[current_jumpdest_node_key] = {
                            "action_type": set(),
                            "send_eth": "no",
                            "eth_events": [],
                            "erc20_events": []
                        }
                    else:
                        jumpdest_node = processed_nodes[current_jumpdest_node_key]

                    # 原有NOTJUMP边逻辑
                    if current_step_idx > 0:
                        prev_step = steps[current_step_idx - 1]
                        prev_opcode = prev_step["opcode"]
                        if prev_opcode not in self.jump_opcodes:
                            prev_block = self._find_block_by_end_pc(
                                address=prev_step["address"],
                                end_pc=prev_step["pc"]
                            )
                            if prev_block:
                                prev_node_key = (prev_block.address, prev_block.start_pc)
                                if prev_node_key not in processed_nodes:
                                    prev_node = BlockNode(prev_block)
                                    processed_nodes[prev_node_key] = prev_node
                                    cfg.add_node(prev_node)
                                    block_temp_data[prev_node_key] = {
                                        "action_type": set(),
                                        "send_eth": "no",
                                        "eth_events": [],
                                        "erc20_events": []
                                    }
                                else:
                                    prev_node = processed_nodes[prev_node_key]

                                cfg.add_edge(
                                    source=prev_node,
                                    target=jumpdest_node,
                                    edge_type="NOTJUMP"
                                )

                    # 切换当前节点
                    current_node = jumpdest_node
                    current_node_key = current_jumpdest_node_key

            # 2. 处理CALL指令（原有逻辑完全不变，token仍为ETH）
            if current_opcode in {"CALL"}:  # 仅处理CALL
                # CALL栈参数（栈底→栈顶）：gas, to, value...
                    # 提取栈顶第三个值（value）：对应stack[-3]（按用户说的“栈顶第三个”确认索引，若不符可调整）
                    value_hex = current_stack[-3]
                    
                    # 提取接收地址（to）：stack[-1]
                    to_addr_raw = current_stack[-2]
                    to_addr = normalize_address(to_addr_raw)
                    
                    # 判断value是否非0x0
                    if value_hex != "0x0":
                        # 更新当前块send_eth为yes
                        block_temp_data[current_node_key]["send_eth"] = "yes"
                        # 记录ETHevent
                        eth_event = {
                            "sender": current_address,  # 当前块所属合约
                            "to": to_addr,              # 栈顶的合约地址
                            "value": value_hex         #    栈顶第三个值（ETH数量）
                        }
                        block_temp_data[current_node_key]["eth_events"].append(eth_event)

                        # 维护表格数据
                        self.table.append({
                            "pc": current_pc,
                            "op": "CALL",
                            "from": current_address, 
                            "to": to_addr, 
                            "token_name": "ETH",  
                            "token_address": "ETH",
                            "balance/amount": value_hex
                        })

            # 3. 处理SSTORE
            if current_opcode == "SSTORE":
                block_temp_data[current_node_key]["action_type"].add("write")

                # 解析栈顶（slot）：SSTORE栈顶是slot（stack[-1]），第二个值是value（stack[-2]）
                if len(current_stack) >= 2:
                    slot_hex = current_stack[-1].lower()
                    balance_hex = current_stack[-2]
                    
                    # 查slot_map找对应地址
                    if slot_hex in slot_map:
                        to_addr = slot_map[slot_hex]
                        token_name = self._get_token_name_by_address(current_address, erc20_token_map)
                        # 未匹配到则用地址兜底
                        if token_name:
                            balance_normalized = self._normalize_hex_value(balance_hex)
                            # 记录ERC20event
                            erc20_event = {
                                "type": "write",
                                "address": to_addr,
                                "token": token_name, 
                                "balance": balance_normalized
                            }
                            block_temp_data[current_node_key]["erc20_events"].append(erc20_event)

                            # 维护表格数据（token字段改为匹配的名称）
                            self.table.append({
                                "pc": current_pc,
                                "op": "SSTORE",
                                "from": None, 
                                "to": to_addr,  
                                "token_name": token_name,
                                "token_address": current_address,
                                "balance/amount": balance_normalized  
                            })

            # 4. 处理SLOAD（仅此处添加token名称匹配）
            if current_opcode == "SLOAD":
                block_temp_data[current_node_key]["action_type"].add("read")
                
                # 解析栈顶（slot）：SLOAD栈顶是slot（stack[-1]）
                if len(current_stack) >= 1:
                    slot_hex = current_stack[-1].lower()
                    
                    # 查slot_map找对应地址
                    if slot_hex in slot_map:
                        to_addr = slot_map[slot_hex]
                        token_name = self._get_token_name_by_address(current_address, erc20_token_map)
                        # 未匹配到则用地址兜底
                        if token_name:
                        
                            # 提取下一个step的栈顶作为balance
                            balance_hex = "0x0"
                            if current_step_idx + 1 < len(steps):
                                next_step = steps[current_step_idx + 1]
                                next_stack = next_step.get("stack", [])
                                if len(next_stack) >= 1:
                                    balance_hex = next_stack[-1]
                            balance_normalized = self._normalize_hex_value(balance_hex)
                            # 记录ERC20event
                            erc20_event = {
                                "type": "read",
                                "address": to_addr,
                                "token_name": token_name,
                                "token_address": current_address,
                                "balance": balance_normalized
                            }
                            block_temp_data[current_node_key]["erc20_events"].append(erc20_event)

                            # 维护表格数据
                            self.table.append({
                                "pc": current_pc,
                                "op": "SLOAD",
                                "from": to_addr, 
                                "to": None, 
                                "token_name": token_name,
                                "token_address": current_address,  
                                "balance/amount": balance_normalized
                            })

            # 5. 累加gas（ ）
            step_gas = 0
            try:
                step_gas = self._get_step_gas_decimal(current_step)
            except Exception:
                step_gas = 0
            if current_node is not None:
                current_node.total_gas += step_gas

            # 6. 处理分块触发指令（ ）
            if current_opcode in self.split_opcodes:
                if current_step_idx + 1 >= len(steps):
                    break

                next_step = steps[current_step_idx + 1]
                try:
                    next_base_block = self._find_base_block(
                        address=next_step["address"],
                        pc=next_step["pc"]
                    )
                except ValueError:
                    current_step_idx += 1
                    continue

                next_node_key = (next_base_block.address, next_base_block.start_pc)
                if next_node_key not in processed_nodes:
                    next_node = BlockNode(next_base_block)
                    processed_nodes[next_node_key] = next_node
                    cfg.add_node(next_node)
                    # 初始化新块临时数据（ ）
                    block_temp_data[next_node_key] = {
                        "action_type": set(),
                        "erc20_events": [],
                        "send_eth": "no",
                        "eth_events": []
                    }
                else:
                    next_node = processed_nodes[next_node_key]

                # 原有边类型逻辑
                edge_type = "NORMAL"
                if current_opcode in self.jump_opcodes:
                    edge_type = "JUMP"
                elif current_opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}:
                    edge_type = "CALL"
                elif current_opcode in {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}:
                    edge_type = "TERMINATE"

                cfg.add_edge(
                    source=current_node,
                    target=next_node,
                    edge_type=edge_type
                )

                # 切换当前节点
                current_node = next_node
                current_node_key = next_node_key

            current_step_idx += 1

        # 7. 将temp_data写入原有actions列表
        for node_key, temp_data in block_temp_data.items():
            node = processed_nodes[node_key]
            
            #  处理action_type（ ）
            action_type_set = temp_data["action_type"]
            if not action_type_set:
                action_type = "none"
            elif len(action_type_set) == 2:
                action_type = "read&write"
            else:
                action_type = next(iter(action_type_set))  # 取唯一值
            
            #  提取ERC20event
            erc20_events = temp_data.get("erc20_events", []) or []
            valid_erc20 = []
            for e in erc20_events:
                if not e or "type" not in e or e["type"] not in ("read", "write"):
                    continue
                action_type_erc20 = e["type"]
                addr = e.get("address", "")
                token = e.get("token", "")
                bal = e.get("balance", "")
                valid_erc20.append({
                    action_type_erc20: {
                        "address": addr,
                        "token": token, 
                        "balance": bal
                    }
                })
            
            #  提取ETHevent
            eth_events = temp_data.get("eth_events", []) or []
            eth_event = eth_events[0] if (eth_events and eth_events[0]) else None
            
            #  处理send_eth
            send_eth = temp_data.get("send_eth", "NO").upper()
            send_eth = "YES" if send_eth == "YES" else "NO"
            
            # 调用add_action
            node.add_action(
                action_type=action_type,
                ERC20event=valid_erc20,
                ETHevent=eth_event,
                send_eth=send_eth
            )

        return cfg

    def _get_edge_type(self, opcode: str) -> str:
        """根据终止 opcode 确定边类型）"""
        if opcode in {"JUMP"}:
            return "JUMP"
        elif opcode in {"JUMPI"}:
            return "JUMPI"
        elif opcode in {"CALL"}:
            return "CALL"
        elif opcode in {"STATICCALL"}:
            return "STATICCALL"
        elif opcode in {"CALLCODE,DELEFATECALL"}:
            return "OTHERCALL"
        elif opcode in {"RETURN"}:
            return "RETURN"
        elif opcode in {"REVERT"}:
            return "REVERT"
        elif opcode == "SELFDESTRUCT":
            return "DESTRUCT"
        elif opcode in {"STOP", "INVALID"}:
            return "TERMINATE"
        elif opcode in {"CREATE", "CREATE2"}:
            return "CREATE"
        else:
            return "UNKNOWN"


# 渲染CFG为DOT文件
def render_transaction(cfg: CFG, output_path: str, rankdir: str = "TB") -> None:
    """
    将CFG渲染为DOT文件（仅在Action中显示SLOAD/SSTORE的token名称）
    """
    # 合约颜色映射
    contract_colors = [
        "#FF9E9E", "#81C784", "#64B5F6", "#FFF176", "#BA68C8",
        "#4DD0E1", "#FFB74D", "#F48FB1", "#AED581", "#7986CB",
    ]
    
    # 边类型到颜色的映射
    edge_color_map = {
        "NORMAL": "#000000",
        "JUMP": "#FF5722",
        "NOTJUMP": "#FFC107",
        "CALL": "#2196F3",
        "TERMINATE": "#F44336",
        "JUMPI": "#9C27B0",
        "STATICCALL": "#00BCD4",
        "OTHERCALL": "#FF9800",
        "RETURN": "#4CAF50",
        "REVERT": "#FFEB3B",
        "DESTRUCT": "#9E9E9E",
        "CREATE": "#8BC34A",
        "UNKNOWN": "#607D8B"
    }

    # 生成DOT文件内容
    dot_content = [
        "digraph CFG {",
        f"  rankdir={rankdir};",
        "  node [shape=box, style=\"rounded,filled\", fontname=\"Monospace\"];",
        "  edge [fontname=\"Monospace\"];"
    ]

    # 为每个合约分配颜色
    contract_addresses = list({node.base_block.address for node in cfg.nodes})
    contract_color_assignment = {
        addr: contract_colors[i % len(contract_colors)] 
        for i, addr in enumerate(contract_addresses)
    }

    # 添加节点
    for node in cfg.nodes:
        block = node.base_block
        # 检查是否包含MUL/DIV指令（ ）
        has_mul_div = any(item[1] in {"MUL", "DIV"} for item in block.instructions)
        style = "rounded,filled"
        if has_mul_div:
            style += ",bold"
        
        # 构建标签
        label_lines = [
            f"Address: {node.address}",
            f"Start PC: {node.start_pc}",
            f"Total BlockGas: {node.total_gas}",
        ]

        # 获取Action字符串
        actions_str = node.get_actions_str()
        if actions_str:
            label_lines += [
                "--- Actions ---",
                actions_str
            ]

        # 添加指令
        instructions_lines = [f"{pc}: {opcode}" for pc, opcode in node.instructions]
        if instructions_lines:
            label_lines += [
                "--- Instructions ---"
            ] + instructions_lines
        
        label = "\\n".join(label_lines)
        color = contract_color_assignment[block.address]
        node_id = f"node_{id(node)}"
        dot_content.append(
            f'  {node_id} [label="{label}", style="{style}", fillcolor="{color}"];'
        )

    # 添加边
    for edge in cfg.edges:
        source_id = f"node_{id(edge.source)}"
        target_id = f"node_{id(edge.target)}"
        edge_color = edge_color_map.get(edge.edge_type, "#607D8B")
        dot_content.append(
            f'  {source_id} -> {target_id} [label="{edge.edge_type}", color="{edge_color}"];'
        )

    dot_content.append("}")

    # 写入文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(dot_content))
    
    print(f"CFG已渲染至: {output_path}")