# transaction.py负责构建整个transaction的CFG图
# 包含连接逻辑
# 包含Transaction Execution CFG渲染

from typing import List, Dict, Tuple, Optional, Set
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

    def construct_cfg(self, trace: StandardizedTrace) -> CFG:
        cfg = CFG(tx_hash=trace["tx_hash"])
        steps = trace["steps"]
        if not steps:
            return cfg

        processed_nodes: Dict[Tuple[str, str], BlockNode] = {}  # 复用节点
        current_step_idx = 0  # 当前处理的 step 索引

        # 处理第一个块
        first_step = steps[current_step_idx]
        try:
            current_base_block = self._find_base_block(
                address=first_step["address"],
                pc=first_step["pc"]
            )
        except ValueError as e:
            raise RuntimeError(f"初始化第一个块失败：{e}")

        # 创建第一个节点（自动包含完整指令列表）
        current_node_key = (current_base_block.address, current_base_block.start_pc)
        current_node = BlockNode(current_base_block)
        processed_nodes[current_node_key] = current_node
        cfg.add_node(current_node)

        # 遍历 trace，按块处理
        while current_step_idx < len(steps):
            current_step = steps[current_step_idx]
            current_opcode = current_step["opcode"]

            # 处理JUMPDEST：检查上一步是否为JUMP/JUMPI
            if current_opcode == "JUMPDEST":
                # 确保不是第一步，有上一个step可以检查
                if current_step_idx > 0:
                    prev_step = steps[current_step_idx - 1]
                    prev_opcode = prev_step["opcode"]
                    
                    # 如果上一步不是JUMP/JUMPI，需要添加连接
                    if prev_opcode not in self.jump_opcodes:
                        # 查找上一步所在块（以上一步pc为结束pc的块）
                        prev_block = self._find_block_by_end_pc(
                            address=prev_step["address"],
                            end_pc=prev_step["pc"]
                        )
                        
                        # 查找当前JUMPDEST所在的块
                        try:
                            current_jumpdest_block = self._find_base_block(
                                address=current_step["address"],
                                pc=current_step["pc"]
                            )
                        except ValueError as e:
                            print(f"警告：JUMPDEST对应的块未找到：{e}")
                            current_step_idx += 1
                            continue
                        
                        if prev_block:
                            # 获取或创建前后节点
                            prev_node_key = (prev_block.address, prev_block.start_pc)
                            if prev_node_key in processed_nodes:
                                prev_node = processed_nodes[prev_node_key]
                            else:
                                prev_node = BlockNode(prev_block)
                                processed_nodes[prev_node_key] = prev_node
                                cfg.add_node(prev_node)
                            
                            current_jumpdest_node_key = (current_jumpdest_block.address, current_jumpdest_block.start_pc)
                            if current_jumpdest_node_key in processed_nodes:
                                jumpdest_node = processed_nodes[current_jumpdest_node_key]
                            else:
                                jumpdest_node = BlockNode(current_jumpdest_block)
                                processed_nodes[current_jumpdest_node_key] = jumpdest_node
                                cfg.add_node(jumpdest_node)
                            
                            # 创建连接边，使用自动编号的add_edge方法
                            cfg.add_edge(
                                source=prev_node,
                                target=jumpdest_node,
                                edge_type="NOTJUMP" # 这两个块之间的连接不是由JUMP造成
                            )
                            
                            # 更新当前节点为JUMPDEST所在的节点，保持控制流连续性
                            current_node = jumpdest_node

            # 遇到分块触发指令时，切换到下一个块
            if current_opcode in self.split_opcodes:
                if current_step_idx + 1 >= len(steps):
                    break  # 已到 trace 末尾

                next_step = steps[current_step_idx + 1]
                try:
                    next_base_block = self._find_base_block(
                        address=next_step["address"],
                        pc=next_step["pc"]
                    )
                except ValueError as e:
                    print(f"警告：步骤 {current_step_idx + 1} 对应的下一个块未找到：{e}")
                    current_step_idx += 1
                    continue

                # 复用或创建下一个节点（包含完整指令列表）
                next_node_key = (next_base_block.address, next_base_block.start_pc)
                if next_node_key in processed_nodes:
                    next_node = processed_nodes[next_node_key]
                else:
                    next_node = BlockNode(next_base_block)  # 指令列表自动包含
                    processed_nodes[next_node_key] = next_node
                    cfg.add_node(next_node)

                # 创建边，使用自动编号的add_edge方法
                edge_type = self._get_edge_type(current_opcode)
                cfg.add_edge(
                    source=current_node,
                    target=next_node,
                    edge_type=edge_type
                )

                current_node = next_node

            current_step_idx += 1

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


# 渲染CFG为DOT文件（显示所有指令并按合约染色）
def render_transaction(cfg: CFG, output_path: str, rankdir: str = "TB") -> None:
    """
    将CFG渲染为DOT文件，显示所有指令，并为不同合约的块自动分配不同颜色
    包含MUL或DIV指令的块会有加粗边框，同时为不同类型的边添加颜色
    """
    # 合约颜色映射
    contract_colors = [
        "#FF9E9E", "#81C784", "#64B5F6", "#FFF176", "#BA68C8",
        "#4DD0E1", "#FFB74D", "#F48FB1", "#AED581", "#7986CB",
        "#FF8A65", "#4DB6AC", "#DCE775", "#9575CD", "#FFD54F"
    ]
    
    # 边类型颜色映射
    edge_color_map = {
        "JUMP": "#ff9800",          
        "JUMPI": "#eaff00",         
        "CALL": "#037dff",          
        "STATICCALL": "#2196F3",    
        "OTHERCALL": "#7b61ff",     
        "RETURN": "#04f4fd",        
        "REVERT": "#ff6b6b",        
        "DESTRUCT": "#012F0B",      
        "TERMINATE": "#d104ff",     
        "CREATE": "#8bc34a",        
        "NOTJUMP": "#533203",       
        "UNKNOWN": "#bdbdbd"        
    }
    
    unique_addresses: Set[str] = {node.address for node in cfg.nodes}
    address_color_map: Dict[str, str] = {}
    
    for i, address in enumerate(unique_addresses):
        color_index = i % len(contract_colors)
        address_color_map[address] = contract_colors[color_index]
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('digraph CFG {\n')
        f.write(f'    rankdir={rankdir};\n')
        f.write('    node [shape=box, style="filled, rounded", '
                'fontname="Arial", fontsize=8, margin=0.1];\n')
        f.write('    edge [fontname="Arial", fontsize=8, penwidth=1.2];\n\n')
        
        for node in cfg.nodes:
            node_id = f"node_{node.address.replace('0x', '')}_{node.start_pc.replace('0x', '')}"
            node_color = address_color_map.get(node.address, "#e0e0e0")
            
            has_mul_or_div = any(inst[1] in {"MUL", "DIV"} for inst in node.instructions)
            base_style = "filled, rounded"
            node_style = f"{base_style}, bold" if has_mul_or_div else base_style  # 调整了边框粗细
            
            node_label = (f"{node.address[:8]}...\n"
                         f"start: {node.start_pc} | end: {node.end_pc}\n"
                         f"terminator: {node.terminator}\n"
                         f"---------\n"
                         f"{node.get_instructions_str()}")
            node_label = node_label.replace('"', '\\"')
            f.write(f'    "{node_id}" [label="{node_label}", fillcolor="{node_color}", style="{node_style}"];\n')
        
        f.write('\n')
        
        for edge in cfg.edges:
            source_id = f"node_{edge.source.address.replace('0x', '')}_{edge.source.start_pc.replace('0x', '')}"
            target_id = f"node_{edge.target.address.replace('0x', '')}_{edge.target.start_pc.replace('0x', '')}"
            edge_color = edge_color_map.get(edge.edge_type, "#bdbdbd")
            edge_label = f"id: {edge.edge_id} ({edge.edge_type})"
            f.write(f'    "{source_id}" -> "{target_id}" [label="{edge_label}", color="{edge_color}"];\n')
        
        f.write('}')
    print(f"CFG已渲染为DOT文件：{output_path}")
