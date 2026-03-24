from typing import List, Dict, Tuple, Optional, Set, Any, Iterable
from utils.evm_information import StandardizedStep
from utils.basic_block import Block
from utils.cfg_structure import CFG, BlockNode, Edge
from collections import defaultdict
import copy
import json

# 全局辅助函数：标准化地址（确保地址格式唯一）
def normalize_address(address: str) -> str:
    address_str = str(address).strip().lower().replace("0x0x", "0x")
    body = address_str[2:] if address_str.startswith("0x") else address_str
    if len(body) > 40:
        body = body[-40:]
    if len(body) < 40:
        body = body.zfill(40)
    return f"0x{body}"

# 扩展BlockNode，支持线性折叠+双重去重Gas计算
class FoldableBlockNode(BlockNode):
    """支持线性折叠的BlockNode，按「合约地址+PC」双重去重计算Gas"""
    def __init__(self, base_block: Block):
        super().__init__(base_block)
        # 初始状态即代表“未折叠”状态，无需 is_folded 标签
        # 新增：记录该节点包含的所有原始块对象（物理双轨制核心）
        self.folded_blocks: List[BlockNode] = [self]
        self.fold_info = {
            "blocks_number": 1,
            "total_gas": 0.0,
            "actions": self.actions.copy(),
            "start_step":0,
            "end_step":-1

        }
        self.processed_addr_pc = set()

    def add_addr_pc_gas(self, contract_addr: str, pc: str, gas_value: float):
        addr_str = normalize_address(contract_addr)
        pc_str = str(pc).strip().lower()
        unique_key = (addr_str, pc_str)
        
        if unique_key not in self.processed_addr_pc:
            self.total_gas += gas_value
            self.processed_addr_pc.add(unique_key)
            # 实时同步，保证无论是否折叠，fold_info 始终有效
            self.fold_info["total_gas"] = self.total_gas

    def merge_fold_info(self, other_nodes: List["FoldableBlockNode"]):
        if not other_nodes: return

        current_total_gas = self.fold_info.get("total_gas", self.total_gas)
        merged_total_gas = current_total_gas + sum(
            n.fold_info.get("total_gas", n.total_gas) for n in other_nodes
        )

        for node in other_nodes:
            if node not in self.folded_blocks:
                self.folded_blocks.append(node)
            self.fold_info["actions"].extend(node.actions)

        self.fold_info["blocks_number"] = len(self.folded_blocks)
        self.fold_info["total_gas"] = merged_total_gas

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

    # ========== 核心工具函数（修复进制转换） ==========
    def _safe_hex_to_int(self, value: Any) -> int:
        """安全转换任意类型的Gas值为整数（处理十六进制/空值）"""
        if value is None or value == "" or str(value).lower() == "none":
            return 0
        
        try:
            # 处理十六进制字符串
            val_str = str(value).strip().lower()
            if val_str.startswith("0x"):
                return int(val_str, 16)
            # 处理普通数字字符串/整数
            return int(val_str)
        except (ValueError, TypeError):
            # 转换失败返回0（避免崩溃）
            return 0

    def _get_step_gas_decimal(self, step: StandardizedStep) -> float:
        """获取Step的Gas消耗（修复进制转换）"""
        raw = step.get("gascost")
        return self._safe_hex_to_int(raw)

    # ========== 图处理 ==========
    def _get_unique_parents(self, cfg: CFG, node: FoldableBlockNode) -> Set[FoldableBlockNode]:
        """获取节点的唯一父节点集合"""
        return {e.source for e in cfg.edges if e.target == node and isinstance(e.source, FoldableBlockNode)}

    def _get_unique_children(self, cfg: CFG, node: FoldableBlockNode) -> Set[FoldableBlockNode]:
        """获取节点的唯一子节点集合"""
        return {e.target for e in cfg.edges if e.source == node and isinstance(e.target, FoldableBlockNode)}
    

    # ========== 基础工具方法 ==========
    def _find_base_block(self, address: str, pc: str) -> Optional[Block]:
        key = (address, pc)
        if key in self.base_block_map:
            return self.base_block_map[key]
        return None
    
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
            if s.startswith("0x"):
                return int(s, 16)
            return int(s)
        except Exception:
            return None
    
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
        if pc_int is None: return None

        for node in cfg.nodes:
            if isinstance(node, FoldableBlockNode) and node.address == address:
                start_pc_int = self._pc_to_int(node.start_pc)
                end_pc_int = self._pc_to_int(node.end_pc)
                
                if start_pc_int <= pc_int <= end_pc_int:
                    return node
        return None

    # ========== 语义信息填充 ==========
    def _fill_actions_from_table(self, cfg: CFG, match_mode: str = "addr_pc"):
        """
        从table填充语义信息（ETH/ERC20事件）
        :param match_mode: 匹配模式
            - "addr_pc"  : 按 address + pc 匹配（原逻辑）
            - "step"     : 按 step 落在块 [start_step, end_step] 区间匹配
        """
        from collections import defaultdict
        node_table_map = defaultdict(list)

        # ======================
        # 1. 按不同模式匹配节点
        # ======================
        for item in self.table:
            addr = item.get("codecontract_address")
            pc = item.get("pc")
            step = item.get("step")

            # ----------------------
            # 模式 A：addr + pc 匹配（原有逻辑）
            # ----------------------
            if match_mode == "addr_pc":
                if not addr or not pc:
                    continue
                node = self.find_node_by_pc_address(cfg, addr, pc)
                if node:
                    node_table_map[node].append(item)

            # ----------------------
            # 模式 B：step 区间匹配（新增）
            # ----------------------
            elif match_mode == "step":
                if step is None:
                    continue
                step_val = int(step)
                # 遍历所有块，找到 step 落在 [start_step, end_step] 区间内的节点
                matched_node = None
                for node in cfg.nodes:
                    if not hasattr(node, "fold_info"):
                        continue
                    start = node.fold_info.get("start_step", 0)
                    end = node.fold_info.get("end_step", -1)

                    # 区间匹配规则：start ≤ step ≤ end
                    if end == -1:  # 末尾块（无出边）
                        if step_val >= start:
                            matched_node = node
                            break
                    else:
                        if start <= step_val <= end:
                            matched_node = node
                            break

                if matched_node:
                    node_table_map[matched_node].append(item)

        # ======================
        # 2. 统一覆写写入 actions
        # ======================
        for node, table_items in node_table_map.items():
            # 【覆写核心】先清空节点原有 actions
            node.actions = []
            node.fold_info["actions"] = []

            eth_table_items = [
                item for item in table_items
                if item.get("token_name") == "ETH" and item.get("op") == "CALL"
            ]
            erc20_table_items = [
                item for item in table_items
                if item.get("op") in {"SLOAD", "SSTORE"}
            ]

            # 处理 ERC20 读写
            if erc20_table_items:
                for item in erc20_table_items:
                    op = item.get("op")
                    action_type = "read" if op == "SLOAD" else "write"
                    erc20_event = {
                        "tokenname": item.get("token_name", ""),
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
                    except Exception:
                        pass

            # 处理 ETH 转账
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
                    except Exception:
                        pass

            # 最终覆写 fold_info
            node.fold_info["actions"] = node.actions.copy()


    def _identify_linear_chain(self, cfg: CFG, start_node: FoldableBlockNode) -> List[FoldableBlockNode]:
        """识别线性链路：遇到回环或入度/出度变化时立即截断并返回已识别部分"""
        chain = [start_node]
        current_node = start_node
        contract_addr = start_node.address
        visited_nodes = {start_node} 


        stop_ops = {'CALL', 'DELEGATECALL', 'STATICCALL', 'RETURN', 'REVERT', 'STOP'}
        while True:
            # --- 新增拦截：如果当前块已经执行了环境切换，停止合并 ---
            last_instr = current_node.instructions[-1]
            last_op = last_instr[1] if isinstance(last_instr, tuple) else last_instr.get("opcode")
            if last_op in stop_ops:
                break

            # 1. 获取唯一子节点
            unique_children = self._get_unique_children(cfg, current_node)
            unique_children = {n for n in unique_children}
            
            if len(unique_children) != 1:
                break
            
            next_node = next(iter(unique_children))

            # 2. 回环检测：如果下一个节点已经在链里了，说明这一段到此为止
            if next_node in visited_nodes:
                break

            # 3. 唯一父节点检测（入度安全性检查）
            unique_parents = self._get_unique_parents(cfg, next_node)
            if len(unique_parents) != 1 or next(iter(unique_parents)) != current_node:
                break
            
            # 4. 只有通过了所有检查，才加入链
            chain.append(next_node)
            visited_nodes.add(next_node)
            current_node = next_node

        return chain

    def _fold_linear_chains(self, cfg: CFG) -> Dict[str, List[str]]:
        """
        改进后的线性折叠：支持增量更新，不会覆盖已有的折叠记录。
        """
        # 注意：不再在这里执行 self.folded_node_map = {} 
        # 初始化的工作应该在 construct_cfg 的最开始或者构造函数中完成
        if not hasattr(self, 'folded_node_map') or self.folded_node_map is None:
            self.folded_node_map = {n.id: [n.id] for n in cfg.nodes}

        processed_nodes = set()
        nodes_to_remove = set()
        all_nodes = list(cfg.nodes)

        for node in all_nodes:
            if node in processed_nodes or node in nodes_to_remove:
                continue
            
            # 识别线性链路
            chain = self._identify_linear_chain(cfg, node)
            first_node = chain[0]
            
            if len(chain) > 1:
                other_nodes = chain[1:]
                last_node = chain[-1]
                
                # --- A. 指令与对象继承 (保持线性顺序) ---
                for m_node in other_nodes:
                    # 1. 指令合并
                    first_node.instructions.extend(m_node.instructions)
                    
                    # 2. 深度对象继承 (folded_blocks)
                    if hasattr(m_node, 'folded_blocks'):
                        ext_blocks = [b for b in m_node.folded_blocks if b != m_node]
                        first_node.folded_blocks.extend(ext_blocks)

                    # 3. 映射表更新：核心改动！
                    # 如果 m_node 之前被钻石折叠吞过，pop 出它代表的所有原始 IDs
                    # 如果它是原始块，pop 出来的就是 [m_node.id]
                    m_original_ids = self.folded_node_map.pop(m_node.id, [m_node.id])
                    self.folded_node_map[first_node.id].extend(m_original_ids)

                # --- B. 物理信息合并 (Gas, Actions) ---
                first_node.merge_fold_info(other_nodes)
                
                # --- C. 出边继承 ---
                for edge in cfg.edges:
                    if edge.source == last_node:
                        edge.source = first_node
                
                nodes_to_remove.update(other_nodes)
                processed_nodes.update(chain)
            else:
                # 单个节点如果没有记录，初始化它
                if first_node.id not in self.folded_node_map:
                    self.folded_node_map[first_node.id] = [first_node.id]
                processed_nodes.add(first_node)

        # 4. 物理清理
        cfg.nodes = [n for n in cfg.nodes if n not in nodes_to_remove]
        cfg.edges = [
            e for e in cfg.edges 
            if e.source not in nodes_to_remove and e.target not in nodes_to_remove
        ]
        
        # 5. ID 去重
        for rid in self.folded_node_map:
            self.folded_node_map[rid] = list(dict.fromkeys(self.folded_node_map[rid]))
            
        return self.folded_node_map
    

    def _is_context_stable(self, node: FoldableBlockNode) -> bool:
        """
        辅助函数：检查节点是否不含导致环境/上下文切换的指令。
        如果节点以这些指令结尾，说明它是一个边界，不能被当作普通的“中间块”折叠。
        """
        if not node.instructions:
            return True
        
        # 获取最后一条指令的 opcode
        last_instr = node.instructions[-1]
        opcode = last_instr[1] if isinstance(last_instr, tuple) else last_instr.get("opcode")
        
        # 定义会导致上下文切换或终止的指令
        stop_ops = {
            'CALL', 'DELEGATECALL', 'STATICCALL', 'CALLCODE', 
            'CREATE', 'CREATE2', 
            'RETURN', 'REVERT', 'STOP', 'INVALID', 'SELFDESTRUCT'
        }
        
        return opcode not in stop_ops

    def _identify_diamond_pattern(self, cfg: CFG, start_node: FoldableBlockNode) -> Optional[Dict[str, Any]]:
        """
        识别分叉收束结构（三角形/钻石形）：
        Layer 1 -> Layer 2 (mids) -> Layer 3 (end)
        安全性：不判断 address，而是确保中间路径没有跨越上下文边界（CALL/RETURN）。
        """
        layer1 = start_node
        
        # 规则 0：Layer 1 本身不能是环境切换的终点（如果是 CALL，逻辑已转出）
        if not self._is_context_stable(layer1):
            return None

        # 1. 获取 Layer 2：Layer 1 的直接子节点
        layer2_nodes = list(self._get_unique_children(cfg, layer1))
        
        # 规则 1：Layer 1 必须有分叉 (>= 2)
        if len(layer2_nodes) < 2:
            return None
            
        # 规则 2：Layer 2 的节点必须是上下文稳定的（即：不能直接在这一层就 RETURN 或 CALL 走了）
        if any(not self._is_context_stable(n) for n in layer2_nodes):
            return None

        # 2. 收集所有可能的 Layer 3（孙子节点）
        all_grandchildren = set()
        for l2 in layer2_nodes:
            children = self._get_unique_children(cfg, l2)
            for child in children:
                all_grandchildren.add(child)
        
        # 3. 判定唯一的收束点 (Layer 3)
        overlap_nodes = set(layer2_nodes) & all_grandchildren
        target_end_node = None
        
        if len(overlap_nodes) > 0:
            # 情况 A：存在重叠点（如 A-B-C, A-C，此时 C 是重叠点）
            if len(overlap_nodes) == 1:
                candidate = next(iter(overlap_nodes))
                # candidate 是收束点，它可以是环境切换指令（因为它是结构的边界）
                # 我们只需要检查它之前的“路径”是否稳定
                candidate_children = set(self._get_unique_children(cfg, candidate))
                
                # 检查除了这个 candidate 及其后继以外，是否还有其他的孙子节点
                remaining_grandchildren = all_grandchildren - {candidate} - candidate_children
                if not remaining_grandchildren:
                    target_end_node = candidate
            else:
                # 多个重叠点，逻辑复杂，跳过
                return None
        else:
            # 情况 B：标准的钻石形 A->B, A->C, B->D, C->D
            if len(all_grandchildren) == 1:
                target_end_node = next(iter(all_grandchildren))
            else:
                return None

        if not target_end_node:
            return None

        # 4. 确定中间层 mid_nodes (被吞掉的层)
        # mid_nodes 是 Layer 2 中除了最终收束点以外的所有节点
        mid_nodes = [n for n in layer2_nodes if n != target_end_node]

        # 5. 验证中间节点的纯净度 (入度/出度)
        for m in mid_nodes:
            # 规则 3：中间节点必须是上下文稳定的
            if not self._is_context_stable(m):
                return None
                
            # 中间节点只能从 Layer 1 来，且只能去 target_end_node
            m_parents = self._get_unique_parents(cfg, m)
            if len(m_parents) != 1 or next(iter(m_parents)) != layer1:
                return None
                
            m_children = self._get_unique_children(cfg, m)
            if len(m_children) != 1 or next(iter(m_children)) != target_end_node:
                return None

        # 6. 验证收束点的入度安全性
        # target_end_node 的所有父节点必须都在 {layer1} + mid_nodes 集合中
        l3_parents = self._get_unique_parents(cfg, target_end_node)
        allowed_parents = {layer1} | set(mid_nodes)
        if not l3_parents.issubset(allowed_parents):
            return None

        return {
            "root": layer1,
            "mids": mid_nodes,
            "end": target_end_node
        }

    def _fold_dianmond_patterns(self, cfg: CFG) -> Dict[str, List[str]]:
        """
        执行连续/嵌套的分歧收束折叠。
        通过 while 循环实现固定点迭代（Fixed-point Iteration），直到没有更多可折叠的钻石。
        """
        overall_changed = True

        if not hasattr(self, 'folded_node_map') or self.folded_node_map is None:
            self.folded_node_map = {n.id: [n.id] for n in cfg.nodes}
        
        while overall_changed:
            overall_changed = False
            nodes_to_remove = set()
            # 每一轮开始时，基于当前的 cfg.nodes 进行扫描
            # 注意：不能在遍历过程中直接删除 cfg.nodes，否则会引起迭代器错误
            current_scan_nodes = [n for n in cfg.nodes]

            for node in current_scan_nodes:
                # 如果该节点已经在这一轮被标记为删除了，跳过
                if node in nodes_to_remove:
                    continue

                # 识别模式 (A-B, A-C-B 或 A-B-D, A-C-D)
                pattern = self._identify_diamond_pattern(cfg, node)

                if pattern:
                    root, mids, end = pattern["root"], pattern["mids"], pattern["end"]
                    
                    # 安全检查：确保 mids 和 end 还没被本轮其他折叠吞掉
                    if end in nodes_to_remove or any(m in nodes_to_remove for m in mids):
                        continue

                    # --- 1. 指令线性化打平与边界标记 ---
                    for i, m_node in enumerate(mids):
                        root.instructions.append({
                            "pc": "---", 
                            "opcode": f"BRANCH_SEGMENT_{i+1}", 
                            "is_boundary": True,
                            "from_id": m_node.id
                        })
                        root.instructions.extend(m_node.instructions)
                    
                    root.instructions.append({
                        "pc": "---", 
                        "opcode": "MERGE_POINT_SEGMENT", 
                        "is_boundary": True,
                        "from_id": end.id
                    })
                    root.instructions.extend(end.instructions)

                    # --- 2. 深度对象继承 (folded_blocks) ---
                    to_absorb = mids + [end]
                    for node_to_fold in to_absorb:
                        if hasattr(node_to_fold, 'folded_blocks'):
                            # 继承该节点之前所有折叠过的原始块对象
                            ext_blocks = [b for b in node_to_fold.folded_blocks if b != node_to_fold]
                            root.folded_blocks.extend(ext_blocks)
                        
                        # --- 3. 映射表(folded_node_map) 扁平化更新 ---
                        # pop 掉被吞掉节点的 key，将其所有 original_ids 转移到 root.id 下
                        original_ids = self.folded_node_map.pop(node_to_fold.id, [node_to_fold.id])
                        self.folded_node_map[root.id].extend(original_ids)

                    # --- 4. 物理合并 (Gas, Actions) ---
                    root.merge_fold_info(to_absorb)

                    # --- 5. 出边继承 (这是连续折叠的关键) ---
                    # 让 root 继承收束点(end)的所有出边
                    # 这样在下一轮 while 中，root 可能会触发新的 identify_diamond
                    for edge in cfg.edges:
                        if edge.source == end:
                            edge.source = root

                    # 标记删除并触发下一轮迭代
                    nodes_to_remove.update(to_absorb)
                    overall_changed = True

            # --- 6. 物理清理 (每轮迭代结束后执行) ---
            if nodes_to_remove:
                cfg.nodes = [n for n in cfg.nodes if n not in nodes_to_remove]
                # 移除源或目标在删除列表中的边（内部边）
                cfg.edges = [
                    e for e in cfg.edges 
                    if e.source not in nodes_to_remove and e.target not in nodes_to_remove
                ]
        
        # 最终去重 root 中的原始 ID 列表（以防万一）
        for rid in self.folded_node_map:
            self.folded_node_map[rid] = list(dict.fromkeys(self.folded_node_map[rid]))

        return self.folded_node_map
    

    def _identify_feedback_pattern(self, cfg: CFG, start_node: FoldableBlockNode) -> Optional[Dict[str, Any]]:
        """
        识别自环反馈结构：
        1. 外部自环：A -> B -> A
        2. 自身自环：A -> A
        且满足 A -> Feedback 的边数 == A -> Others 的边数
        """
        layer1 = start_node
        contract_addr = layer1.address
        
        # 1. 统计 A 的所有出边分类
        edges_to_self = [e for e in cfg.edges if e.source == layer1 and e.target == layer1]
        
        # 获取除了 A 以外的所有直接子节点
        other_children = {n for n in self._get_unique_children(cfg, layer1) if n != layer1}
        
        # 2. 判定反馈模式
        feedback_node = None
        
        # 情况 A: 自身自环 (A -> A)
        if len(edges_to_self) > 0:
            # 这里的反馈“节点”实际上就是 A 内部的逻辑，我们记为 "SELF"
            feedback_node = "SELF" 
            edges_to_feedback = edges_to_self
            # 此时的 others 就是除了 A 以外的所有出边
            edges_to_others = [e for e in cfg.edges if e.source == layer1 and e.target != layer1]
            
        # 情况 B: 外部自环 (A -> B -> A)
        else:
            feedback_candidates = []
            for child in other_children:
                # 检查 child 是否是纯净的 B 节点 (唯一父 A, 唯一子 A)
                child_children = self._get_unique_children(cfg, child)
                child_parents = self._get_unique_parents(cfg, child)
                
                if (layer1 in child_children and len(child_children) == 1 and 
                    layer1 in child_parents and len(child_parents) == 1 and
                    child.address == contract_addr):
                    feedback_candidates.append(child)
            
            if len(feedback_candidates) == 1:
                feedback_node = feedback_candidates[0]
                edges_to_feedback = [e for e in cfg.edges if e.source == layer1 and e.target == feedback_node]
                edges_to_others = [e for e in cfg.edges if e.source == layer1 and e.target != feedback_node]

        # 3. 共同的规则校验
        if not feedback_node:
            return None

        # 数量平衡校验：去反馈的边 == 去其他的边
        if len(edges_to_feedback) != len(edges_to_others) or len(edges_to_others) == 0:
            return None

        return {
            "root": layer1,
            "feedback_node": feedback_node  # 可能是 FoldableBlockNode，也可能是 "SELF"
        }
    

    def _fold_feedback_patterns(self, cfg: CFG) -> Dict[str, List[str]]:
        overall_changed = True
        while overall_changed:
            overall_changed = False
            nodes_to_remove = set()
            current_scan_nodes = [n for n in cfg.nodes]

            for node in current_scan_nodes:
                if node in nodes_to_remove: continue
                
                pattern = self._identify_feedback_pattern(cfg, node)
                if pattern:
                    root = pattern["root"]
                    fb = pattern["feedback_node"]
                    
                    if fb == "SELF":
                        # 处理 A -> A 的情况：只加语义标记
                        root.instructions.append({
                            "pc": "---", "opcode": "SELF_LOOP_DETECTED", "is_boundary": True
                        })
                        # 清理掉指向自己的边，防止死循环
                        cfg.edges = [e for e in cfg.edges if not (e.source == root and e.target == root)]
                        overall_changed = True
                    else:
                        # 处理 A -> B -> A 的情况
                        if fb in nodes_to_remove: continue
                        
                        # 合并指令
                        root.instructions.append({
                            "pc": "---", "opcode": "FEEDBACK_LOOP_START", "is_boundary": True, "from_id": fb.id
                        })
                        root.instructions.extend(fb.instructions)
                        root.instructions.append({
                            "pc": "---", "opcode": "FEEDBACK_LOOP_END", "is_boundary": True
                        })

                        # 映射表与对象合并
                        if hasattr(fb, 'folded_blocks'):
                            root.folded_blocks.extend([b for b in fb.folded_blocks if b != fb])
                        
                        m_original_ids = self.folded_node_map.pop(fb.id, [fb.id])
                        self.folded_node_map[root.id].extend(m_original_ids)
                        
                        root.merge_fold_info([fb])
                        nodes_to_remove.add(fb)
                        overall_changed = True

            # 物理清理
            if nodes_to_remove:
                cfg.nodes = [n for n in cfg.nodes if n not in nodes_to_remove]
                cfg.edges = [e for e in cfg.edges if e.source not in nodes_to_remove and e.target not in nodes_to_remove]
        
        return self.folded_node_map
    

    def _identify_dispatch_pattern(self, cfg: CFG, mid_node: FoldableBlockNode) -> Optional[Dict[str, Any]]:
        """
        识别分发中转模式：Parent(A) -> Mid(M) -> Children(C1, C2...)
        条件：
        1. M 必须以 JUMPI 结尾（分发逻辑）
        2. M 只有一个父节点 A
        3. A 的入边数与出边数在本次分发中保持某种逻辑关联（此处采用你要求的入度/出度平衡触发）
        """
        m_node = mid_node
        contract_addr = m_node.address

        # 1. 验证 M 的入度安全性：必须有且仅有一个父节点
        m_parents = list(self._get_unique_parents(cfg, m_node))
        if len(m_parents) != 1:
            return None
        parent_a = m_parents[0]

        # 2. 验证 M 的出度：必须有分歧（JUMPI 产生两个或多个去向）
        m_children = list(self._get_unique_children(cfg, m_node))
        if len(m_children) < 2:
            return None

        # 3. 验证 M 的末尾指令是否为 JUMPI
        last_instr = m_node.instructions[-1]
        opcode = last_instr[1] if isinstance(last_instr, tuple) else last_instr.get("opcode")
        if opcode!= "JUMPI":
            return None

        # 4. 验证 M 的纯净度：确保其所有子节点都在当前合约内
        if any(c.address != contract_addr for c in m_children):
            return None

        # 5. 数量平衡校验 (根据你的要求：M的入边数 == M的出边数)
        # 注意：这里的入边/出边是指 cfg.edges 中的物理边数
        m_in_edges = [e for e in cfg.edges if e.target == m_node]
        m_out_edges = [e for e in cfg.edges if e.source == m_node]
        
        if len(m_in_edges) != len(m_out_edges):
            return None

        return {
            "parent": parent_a,
            "mid": m_node,
            "children": m_children
        }
    

    def _fold_dispatch_patterns(self, cfg: CFG) -> Dict[str, List[str]]:
        """
        执行分发块下沉折叠：
        将 M 的指令合并到每个子节点的头部，并让 A 直接连接到这些子节点。
        """
        overall_changed = True
        while overall_changed:
            overall_changed = False
            nodes_to_remove = set()
            current_scan_nodes = [n for n in cfg.nodes]

            for node in current_scan_nodes:
                if node in nodes_to_remove:
                    continue

                # 尝试以当前节点作为中间件 M 进行识别
                pattern = self._identify_dispatch_pattern(cfg, node)
                
                if pattern:
                    parent_a = pattern["parent"]
                    m_node = pattern["mid"]
                    children = pattern["children"]

                    # 安全检查：确保子节点没有被本轮其他操作标记删除
                    if any(c in nodes_to_remove for c in children):
                        continue

                    # --- 1. 指令下沉与合并 ---
                    # 将 M 的指令复制并插入到每个子节点指令序列的开头
                    for child in children:
                        new_instructions = []
                        # 插入语义边界标记
                        new_instructions.append({
                            "pc": "---", 
                            "opcode": "DISPATCH_LOGIC_SINK", 
                            "is_boundary": True,
                            "from_id": m_node.id
                        })
                        # 插入 M 的原始指令
                        new_instructions.extend(m_node.instructions)
                        # 拼接子块原有的指令
                        new_instructions.extend(child.instructions)
                        # 更新子块指令集
                        child.instructions = new_instructions

                        # --- 2. 深度对象继承 ---
                        # 子块继承 M 曾经折叠过的所有块对象
                        if hasattr(m_node, 'folded_blocks'):
                            ext_blocks = [b for b in m_node.folded_blocks if b != m_node]
                            child.folded_blocks.extend(ext_blocks)

                        # --- 3. 映射表更新 ---
                        # 因为 M 被分发到了多个子块，M 代表的原始 ID 需要被所有子块 ID 共享
                        m_original_ids = self.folded_node_map.get(m_node.id, [m_node.id])
                        for oid in m_original_ids:
                            if oid not in self.folded_node_map[child.id]:
                                self.folded_node_map[child.id].append(oid)
                        
                        # --- 4. 物理信息继承 (Gas, Actions) ---
                        child.merge_fold_info([m_node])

                    # --- 5. 重连边关系 (去重处理逻辑) ---
                    # 目标：对于 parent_a -> m_node 的所有入边，与 m_node -> children 的出边按序 1:1 匹配
                    
                    # 1. 准备：将入边和出边分别按 step 排序，确保 1:1 匹配的顺序正确
                    ins = [e for e in cfg.edges if e.source == parent_a and e.target == m_node]
                    outs = [e for e in cfg.edges if e.source == m_node]
                    ins.sort(key=lambda x: x.edge_step)
                    outs.sort(key=lambda x: x.edge_step)

                    new_edges = []
                    # 使用一个标记位，确保这一组 (parent_a, m_node) 的重连只执行一次
                    has_processed_this_dispatch = False

                    for edge in cfg.edges:
                        # 识别到目标入边
                        if edge.source == parent_a and edge.target == m_node:
                            if not has_processed_this_dispatch:
                                # 【核心：一次性按序配对所有边】
                                # 既然入边出边一样多，直接 zip 对应
                                for in_e, out_e in zip(ins, outs):
                                    new_edge = Edge(
                                        source=parent_a, 
                                        target=out_e.target, 
                                        edge_type=in_e.edge_type, 
                                        edge_step=out_e.edge_step # 使用后期 step
                                    )
                                    new_edges.append(new_edge)
                                
                                has_processed_this_dispatch = True
                            # 无论是否处理过，原有的 A -> M 边都不再加入 new_edges（达到过滤效果）
                            continue
                        
                        # 过滤掉 M 发出的所有旧边
                        elif edge.source == m_node:
                            continue
                            
                        # 其他无关边正常保留
                        else:
                            new_edges.append(edge)
                    
                    cfg.edges = new_edges

                    # 映射表中移除 M 的独立词条
                    if m_node.id in self.folded_node_map:
                        self.folded_node_map.pop(m_node.id)

                    nodes_to_remove.add(m_node)
                    overall_changed = True

            # 物理清理
            if nodes_to_remove:
                cfg.nodes = [n for n in cfg.nodes if n not in nodes_to_remove]
                # 边已经在重连逻辑中清理过了
        
        # 最终去重
        for rid in self.folded_node_map:
            self.folded_node_map[rid] = list(dict.fromkeys(self.folded_node_map[rid]))

        return self.folded_node_map


    def _get_fixed_node_ids(self, cfg: Any) -> Set[str]:
        """
        预计算不可折叠块：
        1. 包含敏感/终止指令 (CALL, REVERT 等)
        2. 拓扑枢纽：唯一父节点或唯一子节点数量过多 (>= 5)
        """
        fixed_ids = set()
        fixed_opcodes = {
            'SSTORE', 'SLOAD', 'LOG0', 'LOG1', 'LOG2', 'LOG3', 'LOG4', 'SELFDESTRUCT',
            'CALL', 'DELEGATECALL', 'STATICCALL', 'CREATE', 'CREATE2',
            'REVERT', 'INVALID', 'STOP', 'RETURN', 'KECCACK256'
        }

        for node in cfg.nodes:
            # A. 指令判定：检查块内是否有关键操作
            has_fixed_opcode = any(
                (instr[1] if isinstance(instr, tuple) else instr.get("opcode")) in fixed_opcodes 
                for instr in node.instructions
            )
            
            # B. 拓扑判定：使用你定义的工具函数
            # 统计物理上的“邻居”数量，不被循环执行的次数干扰
            unique_parents = self._get_unique_parents(cfg, node)
            unique_children = self._get_unique_children(cfg, node)
            du = max(len(unique_children), len(unique_parents))
            is_hub_node = (4 <= du <= 6)
            

            if has_fixed_opcode or is_hub_node:
                fixed_ids.add(node.id)
                
        return fixed_ids


    def _create_and_add_node_instance(self, stack: List[Any], step_label: Any, is_isolated: bool = False) -> Any:
        if not stack and not is_isolated:
            return None

        if is_isolated:
            # --- 处理隔离节点 ---
            source_node = stack  # 此时传入的是单个 node 对象
            new_node = copy.deepcopy(source_node)
            new_node.id = f"{source_node.id}_iso_s{step_label}"
            
            # 注册映射关系
            self.folded_node_map[new_node.id] = self.folded_node_map.get(source_node.id, [source_node.id])
            return new_node
        else:
            # --- 处理聚合节点（将 stack 揉成超级块） ---
            first_node = stack[0]
            merged_node = copy.deepcopy(first_node)
            merged_node.id = f"{first_node.id}_merged_s{step_label}"
            merged_node.instructions = []
            
            for i, node in enumerate(stack):
                # 语义边界标记
                merged_node.instructions.append({
                    "pc": "---", 
                    "opcode": f"TIMELINE_SEG_{i}", 
                    "is_boundary": True,
                    "from_id": node.id
                })
                merged_node.instructions.extend(node.instructions)
                
                # 维护映射
                if merged_node.id not in self.folded_node_map:
                    self.folded_node_map[merged_node.id] = []
                orig_ids = self.folded_node_map.get(node.id, [node.id])
                self.folded_node_map[merged_node.id].extend(orig_ids)
                
                # 物理信息合并 (Gas 等)
                if i > 0 and hasattr(merged_node, 'merge_fold_info'):
                    merged_node.merge_fold_info([node])
            
            return merged_node


    def _mode_fold(self, current_cfg: Any, mode: str = "plain") -> Any:
        fixed_node_ids = self._get_fixed_node_ids(current_cfg)
        sorted_edges = sorted(current_cfg.edges, key=lambda x: x.edge_step)

        new_cfg = copy.deepcopy(current_cfg)
        new_cfg.nodes = []
        new_cfg.edges = []

        self.folded_node_map = {}
        mergestack = []
        fixed_instances = {}
        last_node_in_new_graph = None
        pending_entry_edge = None

        if not sorted_edges:
            return current_cfg

        def _connect(src, tgt, orig_edge):
            if src and tgt and orig_edge:
                ne = copy.copy(orig_edge)
                ne.source, ne.target = src, tgt
                new_cfg.edges.append(ne)

        # 安全工具：确保 fold_info 存在
        def _ensure_fold_info(node):
            if not hasattr(node, 'fold_info'):
                node.fold_info = {}

        # ==========================
        # A. 处理起点
        # ==========================
        u_start = sorted_edges[0].source
        if u_start.id in fixed_node_ids:
            if mode == "plain":
                last_node_in_new_graph = self._create_and_add_node_instance(u_start, 0, is_isolated=True)
            else:
                last_node_in_new_graph = copy.deepcopy(u_start)
                fixed_instances[u_start.id] = last_node_in_new_graph

            _ensure_fold_info(last_node_in_new_graph)
            last_node_in_new_graph.fold_info["start_step"] = 0
            last_node_in_new_graph.fold_info["end_step"] = 0

            new_cfg.nodes.append(last_node_in_new_graph)
        else:
            mergestack.append(u_start)

        # ==========================
        # B. 遍历边（核心修复）
        # ==========================
        for edge in sorted_edges:
            v_old = edge.target
            edge_step = edge.edge_step

            if v_old.id not in fixed_node_ids:
                if not mergestack:
                    pending_entry_edge = edge
                mergestack.append(v_old)
            else:
                # ==========================
                # 1) 合并折叠块（安全处理 None）
                # ==========================
                if mergestack:
                    # ✅ 修复：pending_entry_edge 为 None 时安全赋值
                    if pending_entry_edge:
                        start_step = pending_entry_edge.edge_step + 1
                    else:
                        start_step = 0  # 无入口边 → 起始步 0
                    end_step = edge_step

                    label = pending_entry_edge.edge_step if pending_entry_edge else "START"
                    new_merged = self._create_and_add_node_instance(mergestack, label, is_isolated=False)

                    _ensure_fold_info(new_merged)
                    new_merged.fold_info["start_step"] = start_step
                    new_merged.fold_info["end_step"] = end_step

                    new_cfg.nodes.append(new_merged)
                    _connect(last_node_in_new_graph, new_merged, pending_entry_edge)

                    # 修正上一个节点 end_step
                    if last_node_in_new_graph:
                        _ensure_fold_info(last_node_in_new_graph)
                        if pending_entry_edge:
                            last_node_in_new_graph.fold_info["end_step"] = pending_entry_edge.edge_step
                        else:
                            last_node_in_new_graph.fold_info["end_step"] = 0

                    last_node_in_new_graph = new_merged
                    mergestack = []
                    pending_entry_edge = None

                # ==========================
                # 2) 处理固定节点
                # ==========================
                v_inst = None
                if mode == "plain":
                    v_inst = self._create_and_add_node_instance(v_old, edge_step + 1, is_isolated=True)
                else:
                    if v_old.id not in fixed_instances:
                        v_inst = copy.deepcopy(v_old)
                        fixed_instances[v_old.id] = v_inst
                    else:
                        v_inst = fixed_instances[v_old.id]

                _ensure_fold_info(v_inst)
                v_inst.fold_info["start_step"] = edge_step + 1
                v_inst.fold_info["end_step"] = -1


                if v_inst not in new_cfg.nodes:
                    new_cfg.nodes.append(v_inst)

                _connect(last_node_in_new_graph, v_inst, edge)

                if last_node_in_new_graph:
                    _ensure_fold_info(last_node_in_new_graph)
                    last_node_in_new_graph.fold_info["end_step"] = edge_step

                last_node_in_new_graph = v_inst
        return new_cfg


    # ========== CFG构建主逻辑 ==========
    def construct_cfg(
        self,
        trace: Dict[str, Any],
        slot_map: Dict[str, str],
        erc20_token_map: Dict[str, str],
    ) -> Tuple[CFG, CFG, List[Dict[str, Any]], Dict[str, List[str]], List[Dict[str, Any]]]:
        """构建CFG（核心入口）"""
        cfg = CFG(tx_hash=trace["tx_hash"])
        steps = trace["steps"]
        if not steps:
            return cfg, cfg, [], {}, []

        processed_nodes: Dict[Tuple[str, str], FoldableBlockNode] = {}
        current_step_idx = 0

        # 初始化第一个节点
        first_step = steps[current_step_idx]
        current_base_block = self._find_base_block(first_step["address"], first_step["pc"])
        if current_base_block is None:
            raise RuntimeError(
                f"初始化第一个块失败：未找到 address={first_step['address']} 且 start_pc={first_step['pc']} 的基础块"
            )
        
        current_node_key = (current_base_block.address, current_base_block.start_pc)
        current_node = FoldableBlockNode(current_base_block)
        processed_nodes[current_node_key] = current_node
        cfg.add_node(current_node)

        all_changes = []  # 存储所有余额变化事件
        balance_traces = defaultdict(lambda: {"SLOAD": None, "SLOAD_pc": None, "SSTORE": None, "SSTORE_pc": None})

        # 遍历trace构建结构 + 维护table
        while current_step_idx < len(steps):
            current_step = steps[current_step_idx]
            current_pc = current_step.get("pc", "")
            current_opcode = current_step["opcode"]
            current_stack = current_step.get("stack", [])
            current_address = current_step["address"]
            RW_address = current_step["RW_address"]

            # 处理JUMPDEST
            if current_opcode == "JUMPDEST":
                jumpdest_block = self._find_base_block(current_step["address"], current_step["pc"])
                if jumpdest_block is None:
                    current_step_idx += 1
                    continue

                jumpdest_node_key = (jumpdest_block.address, jumpdest_block.start_pc)
                if jumpdest_node_key not in processed_nodes:
                    jumpdest_node = FoldableBlockNode(jumpdest_block)
                    processed_nodes[jumpdest_node_key] = jumpdest_node
                    cfg.add_node(jumpdest_node)
                else:
                    jumpdest_node = processed_nodes[jumpdest_node_key]

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
                            # 保留 edge_step，移除 edge_id 相关逻辑
                            cfg.add_edge(prev_node, jumpdest_node, "NOTJUMP", current_step_idx)

                current_node = jumpdest_node
                current_node_key = jumpdest_node_key

            # 处理CALL指令（ETH转账）
            if current_opcode == "CALL" and len(current_stack) >= 3:
                value_hex = current_stack[-3]
                eth_value = self._hex_to_int_safe(value_hex)
                to_addr_raw = current_stack[-2]
                to_addr = normalize_address(to_addr_raw)
                if value_hex != "0x0":
                    self.table.append({
                        "pc": current_pc, "codecontract_address": current_address, "step": current_step_idx,
                        "op": "CALL", "from": RW_address, "to": to_addr,
                        "token_name": "ETH", "token_address": "ETH", "balance/amount": value_hex
                    })
                    all_changes.append({
                        "type": "ETH_TRANSFER", "codecontract_address": current_address,
                        "from_address": RW_address, "to_address": to_addr,
                        "eth_value": str(eth_value), "pc": current_pc, "step": current_step_idx
                    })

            # 处理SLOAD
            if current_opcode == "SLOAD" and len(current_stack) >= 1:
                slot_hex = current_stack[-1].lower()
                if slot_hex in slot_map:
                    from_addr = slot_map[slot_hex]
                    token_name = self._get_token_name_by_address(RW_address, erc20_token_map)
                    if token_name != "":
                        balance_hex = "0x0"
                        if current_step_idx + 1 < len(steps):
                            next_stack = steps[current_step_idx + 1].get("stack", [])
                            balance_hex = next_stack[-1] if next_stack else "0x0"
                        
                        self.table.append({
                            "pc": current_pc, "codecontract_address": current_address,"step": current_step_idx,
                            "op": "SLOAD", "from": from_addr, "to": None,
                            "token_name": token_name, "token_address": RW_address,
                            "balance/amount": self._normalize_hex_value(balance_hex)
                        })
                        balance_traces[(current_address, from_addr)]["SLOAD"] = self._normalize_hex_value(balance_hex)
                        balance_traces[(current_address, from_addr)]["SLOAD_pc"] = current_pc
                        balance_traces[(current_address, from_addr)]["SLOAD_step"] = current_step_idx


            # 处理SSTORE
            if current_opcode == "SSTORE" and len(current_stack) >= 2:
                slot_hex = current_stack[-1].lower()
                balance_hex = current_stack[-2]
                if slot_hex in slot_map:
                    to_addr = slot_map[slot_hex]
                    token_name = self._get_token_name_by_address(RW_address, erc20_token_map)
                    if token_name != "":
                        self.table.append({
                            "pc": current_pc, "codecontract_address": current_address,"step": current_step_idx,
                            "op": "SSTORE", "from": None, "to": to_addr,
                            "token_name": token_name, "token_address": RW_address,
                            "balance/amount": self._normalize_hex_value(balance_hex)
                        })
                        balance_traces[(current_address, to_addr)]["SSTORE"] = self._normalize_hex_value(balance_hex)
                        balance_traces[(current_address, to_addr)]["SSTORE_pc"] = current_pc
                        balance_traces[(current_address, to_addr)]["SSTORE_step"] = current_step_idx
                        sload_raw = balance_traces[(current_address, to_addr)]["SLOAD"]
                        if sload_raw is not None:
                            sload_val = self._hex_to_int_safe(sload_raw) or 0
                            sstore_val = self._hex_to_int_safe(self._normalize_hex_value(balance_hex)) or 0
                            diff = sstore_val - sload_val
                            if diff != 0:
                                all_changes.append({
                                    "type": "ERC20_BALANCE_CHANGE", "codecontract_address": current_address,
                                    "erc20_token_address": RW_address, "token_name": token_name,
                                    "user_address": to_addr, "changed_balance": str(diff),
                                    "SLOAD_pc": balance_traces[(current_address, to_addr)]["SLOAD_pc"],
                                    "SlOAD_step": balance_traces[(current_address, from_addr)]["SLOAD_step"] ,
                                    "SSTORE_pc": balance_traces[(current_address, to_addr)]["SSTORE_pc"],
                                    "SSTORE_step": balance_traces[(current_address, to_addr)]["SSTORE_step"]
                                    
                                })
                            balance_traces[(current_address, to_addr)]["SLOAD"] = None
                            balance_traces[(current_address, to_addr)]["SSTORE"] = None

            # Gas累加
            gas_value = self._get_step_gas_decimal(current_step)
            current_node.add_addr_pc_gas(current_address, current_pc, gas_value)

            # 处理分块
            if current_opcode in self.split_opcodes and current_step_idx + 1 < len(steps):
                next_step = steps[current_step_idx + 1]
                next_block = self._find_base_block(next_step["address"], next_step["pc"])
                if next_block is None:
                    current_step_idx += 1
                    continue
                next_node_key = (next_block.address, next_block.start_pc)
                next_node = processed_nodes.get(next_node_key) or FoldableBlockNode(next_block)
                if next_node_key not in processed_nodes:
                    processed_nodes[next_node_key] = next_node
                    cfg.add_node(next_node)
                edge_type = "NORMAL"
                if current_opcode in self.jump_opcodes: edge_type = "JUMP"
                elif current_opcode in {"CALL", "CALLCODE",  "STATICCALL","CREATE","CREATE2","CALLCODE"}: edge_type = "CALL"
                elif current_opcode in {"DELEGATECALL"}: edge_type = "DELEGATECALL"
                elif current_opcode in {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}: edge_type = "TERMINATE"
                # 保留 edge_step，移除 edge_id 相关逻辑
                cfg.add_edge(current_node, next_node, edge_type, current_step_idx)
                current_node = next_node
                current_node_key = next_node_key

            current_step_idx += 1

        # 基于pc填充cfg语义
        self._fill_actions_from_table(cfg, "addr_pc")
        
        # --- 核心改动：物理双轨制 ---
        # 1. 在折叠前，先克隆出一份完整的、原始的 CFG 结构用于查询（tx_cfg）
        original_cfg = copy.deepcopy(cfg)
        
        # 2. 对原有的 cfg 对象进行折叠（这会修改 cfg 内部的 nodes 和 edges，使其变成折叠版）
        folded_node_map = self._fold_linear_chains(cfg)
        folded_node_map = self._fold_feedback_patterns(cfg)
        folded_node_map = self._fold_dianmond_patterns(cfg)
        folded_node_map = self._fold_dispatch_patterns(cfg)
        current_cfg = copy.deepcopy(cfg)
        plain_cfg = self._mode_fold(current_cfg, "plain")
        folded_cfg = self._mode_fold(current_cfg, "folded") # 自动继承cfg语义

        # 基于step填充plain_cfg的语义
        self._fill_actions_from_table(folded_cfg, "step")

        return plain_cfg, folded_cfg, original_cfg, all_changes, folded_node_map, self.table
    
    def build_folded_blocks_information(self, cfg: Any) -> Dict[str, Any]:
        """构建折叠块信息：直接从折叠节点的 fold_info 读取 Step（极简版）"""
        block_inst_map = {}

        for node in cfg.nodes:
            if not isinstance(node, FoldableBlockNode):
                continue

            # 直接从折叠节点获取已存储的 step（核心简化点）
            start_step = node.fold_info.get("start_step", -1)
            end_step = node.fold_info.get("end_step", start_step)

            block_inst_map[node.id] = {
                "block_id": node.id,
                "address": node.address,
                "start_step": start_step,
                "end_step": end_step,
                "blocks_number": node.fold_info.get("blocks_number", len(node.folded_blocks)),
                "folded_blocks": [n.id for n in node.folded_blocks],
                "gas": node.fold_info.get("total_gas", node.total_gas),
                "actions": node.fold_info.get("actions", node.actions),
            }

        return block_inst_map
        
    # 导出blockid和内部信息映射
    def export_folded_blocks_information(self, cfg: CFG, output_path: str):
        block_inst_map = self.build_folded_blocks_information(cfg)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(block_inst_map, f, ensure_ascii=False, indent=2)
