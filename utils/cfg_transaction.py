# cfg_transaction.py - 按父子节点数判断线性折叠（修复边序号+节点信息写入+新增节点序号）

from typing import List, Dict, Tuple, Optional, Set, Any, Iterable
from utils.evm_information import  StandardizedStep
from utils.basic_block import Block
from utils.cfg_structure import CFG, BlockNode, Edge
from collections import defaultdict

# 全局辅助函数：标准化地址
def normalize_address(address: str) -> str:
    address_str = str(address).strip().lower().replace("0x0x", "0x")
    body = address_str[2:] if address_str.startswith("0x") else address_str
    if len(body) > 40:
        body = body[-40:]
    if len(body) < 40:
        body = body.zfill(40)
    return f"0x{body}"

# 扩展BlockNode，支持存储折叠信息（严格继承你structure里的BlockNode）
class FoldableBlockNode(BlockNode):
    """支持线性折叠的BlockNode，存储双层信息（继承原有BlockNode）"""
    def __init__(self, base_block: Block):
        super().__init__(base_block)
        # 折叠层信息（语义层）
        self.fold_info = {
            "end_pc": self.end_pc,          # 折叠后最终end_pc
            "blocks_number": 1,             # 折叠的块数量
            "total_gas": self.total_gas,    # 折叠后总gas
            "actions": self.actions.copy(), # 折叠后合并的actions
            "is_folded": False              # 是否被折叠
        }
    
    def merge_fold_info(self, other_nodes: List["FoldableBlockNode"]):
        """合并线性链路中其他节点的信息到当前节点（折叠逻辑）"""
        if not other_nodes:
            return
        
        # 合并基础信息
        last_node = other_nodes[-1]
        self.fold_info["end_pc"] = last_node.end_pc
        self.fold_info["blocks_number"] = 1 + len(other_nodes)
        self.fold_info["total_gas"] = sum([self.total_gas] + [n.total_gas for n in other_nodes])
        self.fold_info["is_folded"] = True
        
        # 合并语义actions
        for node in other_nodes:
            self.fold_info["actions"].extend(node.actions)
        
        # 合并指令（基础层保留完整指令）
        all_instructions = self.instructions.copy()
        for node in other_nodes:
            all_instructions.extend(node.instructions)
        self.instructions = all_instructions

class CFGConstructor:
    def __init__(self, all_base_blocks: List[Block]):
        self.base_block_map: Dict[Tuple[str, str], Block] = {}
        for block in all_base_blocks:
            self.base_block_map[(block.address, block.start_pc)] = block

        self.split_opcodes = {
            "JUMP", "JUMPI", "CALL", "CALLCODE", "DELEGATECALL", "STATICCALL",
            "CREATE", "CREATE2", "STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"
        }
        self.jump_opcodes = {"JUMP", "JUMPI"}
        self.table = []  # 唯一语义数据来源

    # ========== 按唯一父子节点数判断线性链路 ==========
    def _get_unique_parents(self, cfg: CFG, node: FoldableBlockNode) -> Set[FoldableBlockNode]:
        """获取节点的唯一父节点集合）"""
        return {e.source for e in cfg.edges if e.target == node and isinstance(e.source, FoldableBlockNode)}

    def _get_unique_children(self, cfg: CFG, node: FoldableBlockNode) -> Set[FoldableBlockNode]:
        """获取节点的唯一子节点集合"""
        return {e.target for e in cfg.edges if e.source == node and isinstance(e.target, FoldableBlockNode)}

    def _identify_linear_chain(self, cfg: CFG, start_node: FoldableBlockNode) -> List[FoldableBlockNode]:
        """
        按唯一父子节点数识别线性链路（兼容反复执行场景）
        判定规则：
        1. 当前节点唯一子节点数 = 1
        2. 子节点的唯一父节点数 = 1
        3. 同合约地址
        """
        chain = [start_node]
        current_node = start_node
        contract_addr = start_node.address

        while True:
            # 1. 获取当前节点的唯一子节点（去重）
            unique_children = self._get_unique_children(cfg, current_node)
            # 仅保留同合约地址的子节点
            unique_children = {n for n in unique_children if n.address == contract_addr}
            
            # 唯一子节点数≠1 → 链路结束
            if len(unique_children) != 1:
                break
            
            next_node = next(iter(unique_children))
            # 2. 检查子节点的唯一父节点数是否=1（仅当前节点）
            unique_parents = self._get_unique_parents(cfg, next_node)
            if len(unique_parents) != 1 or next(iter(unique_parents)) != current_node:
                break
            
            # 3. 加入链路，继续遍历
            chain.append(next_node)
            current_node = next_node

        return chain

    def _fold_linear_chains(self, cfg: CFG):
        """折叠所有线性链路"""
        processed_nodes = set()
        nodes = [n for n in cfg.nodes if isinstance(n, FoldableBlockNode)]

        for node in nodes:
            if node in processed_nodes:
                continue
            
            # 识别线性链路（按唯一父子节点数）
            chain = self._identify_linear_chain(cfg, node)
            if len(chain) <= 1:  # 非线性链路，跳过
                processed_nodes.add(node)
                continue
            
            # 1. 合并链路信息到第一个节点
            first_node = chain[0]
            other_nodes = chain[1:]
            first_node.merge_fold_info(other_nodes)

            # 2. 继承最后一个节点的出边（核心修改：手动建边+复制原边编号）
            last_node = chain[-1]
            last_out_edges = [e for e in cfg.edges if e.source == last_node]
            for edge in last_out_edges:
                # 提取原边的edge_id（编号），完全继承
                original_edge_id = edge.edge_id  # 取structure中add_edge生成的原始编号
                # 手动新建Edge，使用原边的edge_id
                new_edge = Edge(
                    edge_id=original_edge_id,  
                    source=first_node,
                    target=edge.target,
                    edge_type=edge.edge_type
                )
                # 给新边添加自定义属性
                setattr(new_edge, "folded_edge", False)  
                setattr(new_edge, "visible", True)  
                # 手动append到cfg.edges，不调用add_edge（避免edge_counter递增）
                cfg.edges.append(new_edge)

            # 3. 标记中间节点和内部边为隐藏
            for n in other_nodes:
                # 给中间节点添加折叠标记和隐藏样式
                setattr(n, "folded", True)
                setattr(n, "visible", False)
                processed_nodes.add(n)
                
                # 标记指向/来自中间节点的所有边为隐藏
                internal_edges = [e for e in cfg.edges if e.source == n or e.target == n]
                for e in internal_edges:
                    setattr(e, "folded_edge", True)
                    setattr(e, "visible", False)
            
            processed_nodes.add(first_node)
            # 给第一个节点标记折叠状态
            setattr(first_node, "folded", True)
            setattr(first_node, "is_fold_root", True)

    # ========== 基础方法 ==========
    def _find_base_block(self, address: str, pc: str) -> Block:
        key = (address, pc)
        if key in self.base_block_map:
            return self.base_block_map[key]
        raise ValueError(f"未找到 address={address} 且 start_pc={pc} 的基础块")
    
    def _find_block_by_end_pc(self, address: str, end_pc: str) -> Optional[Block]:
        for block in self.base_block_map.values():
            if block.address == address and block.end_pc == end_pc:
                return block
        return None
    
    def _pc_to_int(self, v):
        if v is None:
            return None
        try:
            if isinstance(v, int):
                return v
            s = str(v)
            if s.startswith("0x") or s.startswith("0X"):
                return int(s, 16)
            return int(s)
        except Exception:
            return None

    def _get_step_gas_decimal(self, step: StandardizedStep) -> int:
        raw = step.get("gascost")
        return raw if isinstance(raw, int) else 0

    def _normalize_hex_value(self, val: str) -> str:
        if not val:
            return "0x0"
        val_str = str(val).lower()
        return f"0x{val_str}" if not val_str.startswith("0x") else val_str
    
    def _hex_to_int_safe(self, hex_str: str) -> Optional[int]:
        """安全将十六进制字符串转为整数（失败返回None）"""
        try:
            return int(self._normalize_hex_value(hex_str).lstrip("0x"), 16)
        except (ValueError, TypeError):
            return None

    def _get_token_name_by_address(self, address: str, erc20_token_map: Dict[str, str]) -> str:
        if not address or not erc20_token_map:
            return ""
        return erc20_token_map.get(address.lower(), "")
    
    def find_node_by_pc_address(self, cfg: CFG, address: str, pc: str) -> Optional[FoldableBlockNode]:
        pc_int = self._pc_to_int(pc)
        for node in cfg.nodes:
            start_pc_int = self._pc_to_int(node.start_pc)
            end_pc_int = self._pc_to_int(node.end_pc)
            if isinstance(node, FoldableBlockNode) and node.address == address and (start_pc_int <= pc_int <= end_pc_int):
                return node
        return None

    # ========== 语义信息填充 ==========
    def _fill_actions_from_table(self, cfg: CFG):
        """从table填充语义信息"""
        node_table_map: Dict[FoldableBlockNode, List[Dict[str, Any]]] = {}
        for item in self.table:
            addr = item.get("token_address") if item.get("token_address") != "ETH" else item.get("from")
            pc = item.get("pc")
            if not addr or not pc:
                continue
            
            node = self.find_node_by_pc_address(cfg, addr, pc)
            if node:
                if node not in node_table_map:
                    node_table_map[node] = []
                node_table_map[node].append(item)

        for node, table_items in node_table_map.items():
            # 1. 分离ETH事件和ERC20事件
            eth_table_items = [item for item in table_items if item.get("token_name") == "ETH" and item.get("op") == "CALL"]
            erc20_table_items = [item for item in table_items if item.get("op") in {"SLOAD", "SSTORE"}]

            # 2. 处理ERC20事件
            if erc20_table_items:
                for item in erc20_table_items:
                    op = item.get("op")
                    action_type = "read" if op == "SLOAD" else "write"

                    erc20_event = {
                        "tokenname": item.get("token_name", "") or item.get("token_address", ""),
                        "type": action_type,
                        "user": item.get("from") if action_type == "read" else item.get("to"),
                        "balance": self._normalize_hex_value(item.get("balance/amount", ""))
                    }

                    try:
                        node.add_action(
                            action_type=action_type,  
                            erc20_events=[erc20_event], 
                            send_eth="NO",
                            eth_event=None
                        )
                    except Exception as e:
                        print(f"ERC20({action_type}) add_action调用失败 ❌: {type(e).__name__} = {e}")
                        raise

            # 3. 处理ETH事件
            if eth_table_items:
                for eth_item in eth_table_items:
                    eth_event = {
                        "type": "ETH",
                        "from": eth_item.get("from", ""),
                        "to": eth_item.get("to", ""),
                        "amount": eth_item.get("balance/amount", "")
                    }
                    try:
                        node.add_action(
                            action_type="eth_transfer",
                            erc20_events=[],
                            send_eth="YES",
                            eth_event=eth_event
                        )
                    except Exception as e:
                        print(f"ETH add_action调用失败 ❌: {type(e).__name__} = {e}")
                        raise
            
            node.fold_info["actions"] = node.actions.copy()

    # ========== CFG构建主逻辑 ==========
    def construct_cfg(self, trace: Dict[str, Any], slot_map: Dict[str, str], erc20_token_map: Dict[str, str]) -> CFG:
        cfg = CFG(tx_hash=trace["tx_hash"])
        steps = trace["steps"]
        if not steps:
            return cfg, []

        processed_nodes: Dict[Tuple[str, str], FoldableBlockNode] = {}
        current_step_idx = 0

        # 初始化第一个节点
        first_step = steps[current_step_idx]
        try:
            current_base_block = self._find_base_block(first_step["address"], first_step["pc"])
        except ValueError as e:
            raise RuntimeError(f"初始化第一个块失败：{e}")
        
        current_node_key = (current_base_block.address, current_base_block.start_pc)
        current_node = FoldableBlockNode(current_base_block)
        processed_nodes[current_node_key] = current_node
        cfg.add_node(current_node)

        all_changes = []  # 存储所有余额变化事件
         # 余额变化追踪
        balance_traces = defaultdict(lambda: {"SLOAD": None, "SLOAD_pc": None, "SSTORE": None, "SSTORE_pc": None})

        # 遍历trace构建结构 + 维护table
        while current_step_idx < len(steps):
            current_step = steps[current_step_idx]
            current_pc = current_step.get("pc", "")
            current_opcode = current_step["opcode"]
            current_stack = current_step.get("stack", [])
            current_address = current_step["address"]

            # 处理JUMPDEST
            if current_opcode == "JUMPDEST":
                try:
                    jumpdest_block = self._find_base_block(current_step["address"], current_step["pc"])
                except ValueError as e:
                    current_step_idx += 1
                    continue

                jumpdest_node_key = (jumpdest_block.address, jumpdest_block.start_pc)
                if jumpdest_node_key not in processed_nodes:
                    jumpdest_node = FoldableBlockNode(jumpdest_block)
                    processed_nodes[jumpdest_node_key] = jumpdest_node
                    cfg.add_node(jumpdest_node)
                else:
                    jumpdest_node = processed_nodes[jumpdest_node_key]

                # 构建NOTJUMP边
                if current_step_idx > 0:
                    prev_step = steps[current_step_idx - 1]
                    if prev_step["opcode"] not in self.jump_opcodes:
                        prev_block = self._find_block_by_end_pc(prev_step["address"], prev_step["pc"])
                        if prev_block:
                            prev_node_key = (prev_block.address, prev_block.start_pc)
                            prev_node = processed_nodes.get(prev_node_key) or FoldableBlockNode(prev_block)
                            if prev_node_key not in processed_nodes:
                                processed_nodes[prev_node_key] = prev_node
                                cfg.add_node(prev_node)
                            # 调用structure的add_edge，自动生成递增编号
                            cfg.add_edge(prev_node, jumpdest_node, "NOTJUMP")

                current_node = jumpdest_node
                current_node_key = jumpdest_node_key

            # 处理CALL指令
            if current_opcode == "CALL" and len(current_stack) >= 3:
                value_hex = current_stack[-3]
                eth_value = self._hex_to_int_safe(value_hex)
                to_addr_raw = current_stack[-2]
                to_addr = normalize_address(to_addr_raw)
                if value_hex != "0x0":
                    self.table.append({
                        "pc": current_pc,
                        "op": "CALL",
                        "from": current_address,
                        "to": to_addr,
                        "token_name": "ETH",
                        "token_address": "ETH",
                        "balance/amount": value_hex
                    })

                    all_changes.append({
                        "type": "ETH_TRANSFER",
                        "from_address": current_address,
                        "to_address": to_addr,
                        "eth_value": str(eth_value),
                        "pc": current_pc
                    })

            # 处理SLOAD
            if current_opcode == "SLOAD" and len(current_stack) >= 1:
                slot_hex = current_stack[-1].lower()
                if slot_hex in slot_map:
                    from_addr = slot_map[slot_hex]
                    token_name = self._get_token_name_by_address(current_address, erc20_token_map)
                    balance_hex = "0x0"
                    if current_step_idx + 1 < len(steps):
                        next_stack = steps[current_step_idx + 1].get("stack", [])
                        balance_hex = next_stack[-1] if next_stack else "0x0"
                    
                    self.table.append({
                        "pc": current_pc,
                        "op": "SLOAD",
                        "from": from_addr,
                        "to": None,
                        "token_name": token_name or current_address,
                        "token_address": current_address,
                        "balance/amount": self._normalize_hex_value(balance_hex)
                    })

                    balance_traces[(current_address, from_addr)]["SLOAD"] = self._normalize_hex_value(balance_hex)
                    balance_traces[(current_address, from_addr)]["SLOAD_pc"] = current_pc

            # 处理SSTORE
            if current_opcode == "SSTORE" and len(current_stack) >= 2:
                slot_hex = current_stack[-1].lower()
                balance_hex = current_stack[-2]
                if slot_hex in slot_map:
                    to_addr = slot_map[slot_hex]
                    token_name = self._get_token_name_by_address(current_address, erc20_token_map)
                    self.table.append({
                        "pc": current_pc,
                        "op": "SSTORE",
                        "from": None,
                        "to": to_addr,
                        "token_name": token_name or current_address,
                        "token_address": current_address,
                        "balance/amount": self._normalize_hex_value(balance_hex)
                    })
                    balance_traces[(current_address, to_addr)]["SSTORE"] = self._normalize_hex_value(balance_hex)
                    balance_traces[(current_address, to_addr)]["SSTORE_pc"] = current_pc

                    # 计算差值并记录
                    sload_raw = balance_traces[(current_address, to_addr)]["SLOAD"]
                    if sload_raw is not None:
                        sload_val = self._hex_to_int_safe(sload_raw) or 0
                        sstore_val = self._hex_to_int_safe(self._normalize_hex_value(balance_hex)) or 0
                        diff = sstore_val - sload_val
                            
                        if diff != 0:
                            all_changes.append({
                                "type": "ERC20_BALANCE_CHANGE",
                                "erc20_token_address": current_address,
                                "token_name": token_name,
                                "user_address": to_addr,
                                "changed_balance": str(diff),
                                "SLOAD_pc": balance_traces[(current_address, to_addr)]["SLOAD_pc"],
                                "SSTORE_pc": balance_traces[(current_address, to_addr)]["SSTORE_pc"]
                            })
                        # 计算完重置
                        balance_traces[(current_address, to_addr)]["SLOAD"] = None
                        balance_traces[(current_address, to_addr)]["SSTORE"] = None

            # 累加gas
            current_node.total_gas += self._get_step_gas_decimal(current_step)
            current_node.fold_info["total_gas"] = current_node.total_gas

            # 处理分块指令，构建边
            if current_opcode in self.split_opcodes and current_step_idx + 1 < len(steps):
                next_step = steps[current_step_idx + 1]
                try:
                    next_block = self._find_base_block(next_step["address"], next_step["pc"])
                except ValueError:
                    current_step_idx += 1
                    continue

                next_node_key = (next_block.address, next_block.start_pc)
                next_node = processed_nodes.get(next_node_key) or FoldableBlockNode(next_block)
                if next_node_key not in processed_nodes:
                    processed_nodes[next_node_key] = next_node
                    cfg.add_node(next_node)

                # 确定边类型
                edge_type = "NORMAL"
                if current_opcode in self.jump_opcodes:
                    edge_type = "JUMP"
                elif current_opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}:
                    edge_type = "CALL"
                elif current_opcode in {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}:
                    edge_type = "TERMINATE"

                # 调用structure的add_edge，自动生成递增编号（edge_{edge_counter}_{id(source)}_{id(target)}_{edge_type}）
                cfg.add_edge(current_node, next_node, edge_type)
                current_node = next_node
                current_node_key = next_node_key

            current_step_idx += 1

        # 填充语义信息+折叠线性链路
        self._fill_actions_from_table(cfg)
        self._fold_linear_chains(cfg)

        return cfg, all_changes

    def _get_edge_type(self, opcode: str) -> str:
        type_map = {
            "JUMP": "JUMP", "JUMPI": "JUMPI", "CALL": "CALL",
            "STATICCALL": "STATICCALL", "CALLCODE": "OTHERCALL",
            "DELEGATECALL": "OTHERCALL", "RETURN": "RETURN",
            "REVERT": "REVERT", "SELFDESTRUCT": "DESTRUCT",
            "STOP": "TERMINATE", "INVALID": "TERMINATE",
            "CREATE": "CREATE", "CREATE2": "CREATE"
        }
        return type_map.get(opcode, "UNKNOWN")

