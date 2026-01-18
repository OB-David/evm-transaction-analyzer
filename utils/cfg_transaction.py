# transaction.py负责构建整个transaction的CFG图
# 包含连接逻辑
# 包含Transaction Execution CFG渲染

from typing import List, Dict, Tuple, Optional, Set, Any
from utils.evm_information import StandardizedTrace, StandardizedStep
from utils.basic_block import Block, BasicBlockProcessor
from utils.cfg_structure import CFG, BlockNode, Edge


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

    def construct_cfg(self, trace: Dict[str, Any], slot_map: Dict[str, str]) -> CFG:
        """
        构建CFG（新增slot_map参数，用于解析SSTORE/SLOAD的ERC20事件）
        :param trace: 标准化trace（包含tx_hash、steps等，来自evm_information的get_standardized_trace）
        :param slot_map: slot->address映射（来自evm_information的extract_slot_address_map）
        :return: 填充完整字段的CFG
        """
        cfg = CFG(tx_hash=trace["tx_hash"])
        steps = trace["steps"]
        if not steps:
            return cfg

        processed_nodes: Dict[Tuple[str, str], BlockNode] = {}  # 复用节点
        current_step_idx = 0  # 当前处理的 step 索引

        # 块级临时存储（用于收集当前块的action_type、ETHevent、ERC20event等）
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

        # 初始化第一个节点和临时数据
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

        # 遍历 trace，按块处理
        while current_step_idx < len(steps):
            current_step = steps[current_step_idx]
            current_opcode = current_step["opcode"]
            current_stack = current_step.get("stack", [])
            current_address = current_step["address"]

            # 1. 特殊处理JUMPDEST（原有逻辑）
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
                    # 初始化新块的临时数据
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

            # 2. 处理CALL指令（ETH事件）
            if current_opcode in {"CALL"}:  # 仅处理CALL
                # CALL栈参数（栈底→栈顶）：gas, to, value...
                    # 提取栈顶第三个值（value）：对应stack[-3]（按用户说的“栈顶第三个”确认索引，若不符可调整）
                    value_hex = current_stack[-3]
                    
                    # 提取接收地址（to）：stack[-1]
                    to_addr_raw = current_stack[-1]
                    to_addr = self._normalize_hex_value(to_addr_raw)
                    
                    # 判断value是否非0x0
                    if value_hex != "0x0":
                        # 更新当前块send_eth为yes
                        block_temp_data[current_node_key]["send_eth"] = "yes"
                        # 记录ETHevent
                        eth_event = {
                            "sender": current_address,  # 当前块所属合约
                            "to": to_addr,              # 栈顶的合约地址
                            "value": value_hex         # 栈顶第三个值（ETH数量）
                        }
                        block_temp_data[current_node_key]["eth_events"].append(eth_event)

            # 3. 处理SSTORE（write/ERC20 write事件）
            if current_opcode == "SSTORE":
                block_temp_data[current_node_key]["action_type"].add("write")

                    
                # 解析栈顶（slot）：SSTORE栈顶是slot（stack[-1]），第二个值是value（stack[-2]）
                if len(current_stack) >= 2:
                    slot_hex = current_stack[-1].lower()
                    balance_hex = current_stack[-2]
                    
                    # 查slot_map找对应地址
                    if slot_hex in slot_map:
                        erc20_addr = slot_map[slot_hex]
                        # 记录ERC20event（write）
                        erc20_event = {
                            "type": "write",
                            "address": erc20_addr,
                            "balance": self._normalize_hex_value(balance_hex)
                        }
                        block_temp_data[current_node_key]["erc20_events"].append(erc20_event)

            # 4. 处理SLOAD（read/ERC20 read事件）
            if current_opcode == "SLOAD":
                block_temp_data[current_node_key]["action_type"].add("read")
                
                # 解析栈顶（slot）：SLOAD栈顶是slot（stack[-1]）
                if len(current_stack) >= 1:
                    slot_hex = current_stack[-1].lower()
                    
                    # 查slot_map找对应地址
                    if slot_hex in slot_map:
                        erc20_addr = slot_map[slot_hex]
                        # 提取下一个step的栈顶作为balance
                        balance_hex = "0x0"
                        if current_step_idx + 1 < len(steps):
                            next_step = steps[current_step_idx + 1]
                            next_stack = next_step.get("stack", [])
                            if len(next_stack) >= 1:
                                balance_hex = next_stack[-1]
                        
                        # 记录ERC20event（read）
                        erc20_event = {
                            "type": "read",
                            "address": erc20_addr,
                            "balance": self._normalize_hex_value(balance_hex)
                        }
                        block_temp_data[current_node_key]["erc20_events"].append(erc20_event)

            # 5. 累加gas（原有逻辑）
            step_gas = 0
            try:
                step_gas = self._get_step_gas_decimal(current_step)
            except Exception:
                step_gas = 0
            if current_node is not None:
                current_node.total_gas += step_gas

            # 6. 处理分块触发指令（原有逻辑）
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
                    # 初始化新块临时数据
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

        # 7. 将temp_data写入原有actions列表（复用add_action，兼容原有get_actions_str）
        for node_key, temp_data in block_temp_data.items():
            node = processed_nodes[node_key]
            
            #  处理action_type（合并为read/write/read&write/none）
            action_type_set = temp_data["action_type"]
            if not action_type_set:
                action_type = "none"
            elif len(action_type_set) == 2:
                action_type = "read&write"
            else:
                action_type = next(iter(action_type_set))  # 取唯一值
            
            #  提取ERC20event（适配add_action的格式要求）
            # 确保格式：List[{"read/write": {"address":..., "balance":...}}]
            erc20_events = temp_data.get("erc20_events", []) or []

            # 修正后的格式转换逻辑（适配原始数据结构）
            valid_erc20 = []
            for e in erc20_events:
                # 1. 过滤空字典 + 检查是否有'type'字段 + 'type'值为read/write
                if not e or "type" not in e or e["type"] not in ("read", "write"):
                    continue
                # 2. 提取type/address/balance字段（兼容空值）
                action_type = e["type"]
                addr = e.get("address", "")
                bal = e.get("balance", "")
                # 3. 转换为add_action要求的格式：[{"read/write": {"address":..., "balance":...}}]
                valid_erc20.append({
                    action_type: {
                        "address": addr,
                        "balance": bal
                    }
                })
            
            #  提取ETHevent（适配add_action的格式：单个字典，取第一个有效项）
            eth_events = temp_data.get("eth_events", []) or []
            eth_event = eth_events[0] if (eth_events and eth_events[0]) else None
            
            #  处理send_eth（强制YES/NO）
            send_eth = temp_data.get("send_eth", "NO").upper()
            send_eth = "YES" if send_eth == "YES" else "NO"
            
            # 5. 核心：调用add_action写入原有actions列表
            node.add_action(
                action_type=action_type,
                ERC20event=valid_erc20,
                ETHevent=eth_event,
                send_eth=send_eth
            )

        return cfg

    def _get_edge_type(self, opcode: str) -> str:
        """根据终止 opcode 确定边类型"""
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


# 渲染CFG为DOT文件（显示所有指令并按合约染色）【原有逻辑完全未改 + 新增Action渲染】
def render_transaction(cfg: CFG, output_path: str, rankdir: str = "TB") -> None:
    """
    将CFG渲染为DOT文件，显示所有指令，并为不同合约的块自动分配不同颜色
    包含MUL或DIV指令的块会有加粗边框，同时为不同类型的边添加颜色
    新增：渲染BlockNode的Action字段（action_type/ERC20event/ETHevent/send_eth）
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
        # 检查是否包含MUL/DIV指令
        # 若不想解构，用索引 [1] 取 opcode（元组的第二个元素）
        has_mul_div = any(item[1] in {"MUL", "DIV"} for item in block.instructions)
        # 节点样式
        style = "rounded,filled"
        if has_mul_div:
            style += ",bold"
        
        # ========== 核心修改：添加Action字段渲染 ==========
        # 1. 获取格式化的Action字符串（改造后，空则返回""）
        actions_str = node.get_actions_str()
        if actions_str:
            print(f"Rendering actions for node {node.address[:8]}...: \n{actions_str}")

        # 2. 初始化基础标签行
        label_lines = [
            f"Address: {node.address}",
            f"Start PC: {node.start_pc}",
            f"Total BlockGas: {node.total_gas}",
        ]

        # 3. 仅当Action有有效内容时，才添加Actions模块
        if actions_str:
            label_lines += [
                "--- Actions ---",  # Action标题
                actions_str         # Action内容（无none、无冗余）
            ]

        # 4. 添加指令模块（无指令则不显示）
        instructions_lines = [f"{pc}: {opcode}" for pc, opcode in node.instructions]
        if instructions_lines:
            label_lines += [
                "--- Instructions ---"  # 指令标题
            ] + instructions_lines
        # ========== 核心修改结束 ==========
        
        # 拼接label（DOT语言用\\n表示换行）
        label = "\\n".join(label_lines)
        # 节点颜色
        color = contract_color_assignment[block.address]
        # 添加节点定义
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