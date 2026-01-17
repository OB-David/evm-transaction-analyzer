# cfg_structures.py负责定义CFG图的核心数据结构

from typing import List
from utils.basic_block import Block


class BlockNode:
    """CFG中的节点（对应唯一的basic_block）"""
    def __init__(self, base_block: Block):
        self.base_block = base_block  # 关联的基础块（保留完整引用）
        # 基础块标识信息
        self.address = base_block.address
        self.start_pc = base_block.start_pc
        self.end_pc = base_block.end_pc
        self.terminator = base_block.terminator
        self.instructions = base_block.instructions

    def __repr__(self) -> str:
        return (f"BlockNode(addr={self.address[:8]}..., start_pc={self.start_pc}, "
                f"instr_count={len(self.instructions)})") 
# 上面这段代码定义了一个名为`BlockNode`的类，它表示CFG图中的一个节点。这个类有一个构造函数`__init__`，它接收一个`base_block`参数，表示与这个节点关联的基础块。
# 这个类还有一个`__repr__`方法，它返回一个字符串，表示这个节点的地址、起始PC和指令数量。
    def get_instructions_str(self) -> str:
        """将所有指令转换为字符串"""
        return "\n".join([f"{pc}: {opcode}" for pc, opcode in self.instructions]) # 上面这段代码定义了一个名为`get_instructions_str`的方法，它返回一个字符串，表示这个节点中所有指令的PC和操作码。


class Edge:
    """CFG中的边（带编号和类型）"""
    def __init__(self, edge_id: int, source: BlockNode, target: BlockNode, edge_type: str):
        self.edge_id = edge_id        # 边的唯一编号（按顺序递增）
        self.source = source          # 源节点
        self.target = target          # 目标节点
        self.edge_type = edge_type    # 边类型（由终止指令决定）

    def __repr__(self) -> str:
        return f"Edge(id={self.edge_id}, {self.source.start_pc} -> {self.target.start_pc}, {self.edge_type})"


class CFG:
    """控制流图（包含唯一节点和带编号的边，节点包含完整指令列表）"""
    def __init__(self, tx_hash: str):
        self.tx_hash = tx_hash                # 关联的交易哈希
        self.nodes: List[BlockNode] = []      # 所有节点
        self.edges: List[Edge] = []          # 所有边
        self._next_edge_id = 1                # 下一条边的编号（从1开始）

    def add_node(self, node: BlockNode) -> None:
        """添加节点（仅保留唯一节点，通过address和start_pc判断）"""
        node_key = (node.address, node.start_pc)
        for existing_node in self.nodes:
            if (existing_node.address, existing_node.start_pc) == node_key:
                return 
        self.nodes.append(node)

    def add_edge(self, source: BlockNode, target: BlockNode, edge_type: str) -> None:
        """添加边并自动分配编号"""
        edge = Edge(
            edge_id=self._next_edge_id,
            source=source,
            target=target,
            edge_type=edge_type
        )
        self.edges.append(edge)
        self._next_edge_id += 1  # 编号递增

    def get_node_by_key(self, address: str, start_pc: str) -> BlockNode:
        """通过address和start_pc查找节点"""
        for node in self.nodes:
            if node.address == address and node.start_pc == start_pc:
                return node
        raise ValueError(f"未找到节点: address={address}, start_pc={start_pc}")

    def remove_node(self, node: BlockNode) -> None:
        """移除节点"""
        self.nodes.remove(node)

    def __repr__(self) -> str:
        return f"CFG(tx_hash={self.tx_hash}, nodes={len(self.nodes)}, edges={len(self.edges)})"
