# cfg_structures.py负责定义CFG图的核心数据结构

from typing import List
from utils.basic_block import Block


# cfg_structures.py负责定义CFG图的核心数据结构

from typing import List, Optional, Dict
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
        self.total_gas: int = 0  # 节点内所有指令的总gas消耗
        self.instructions = base_block.instructions

        # 新增：actions 字段，用于记录该节点的操作（可能有多个）
        # 每个 action 是一个 Dict，包含 action_type, operation, participants, send_eth, from, to, value 等键
        self.actions: List[Dict[str, Any]] = []

    def __repr__(self) -> str:
        return (f"BlockNode(addr={self.address[:8]}..., start_pc={self.start_pc}, "
                f"instr_count={len(self.instructions)}, total_gas={self.total_gas}, "
                f"actions={len(self.actions)})")

    def get_instructions_str(self) -> str:
        """将所有指令转换为字符串"""
        return "\n".join([f"{pc}: {opcode}" for pc, opcode in self.instructions])

    # ---------- actions 相关辅助方法 ----------
    def add_action(
        self,
        action_type: str,
        participants: Optional[List[Dict[str, str]]] = None,
        send_eth: str = "NO",
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        value: Optional[int] = 0,
    ) -> None:
        """
        添加一个 action 到本节点。

        参数示例：
        action_type: "read" / "write" / "read&write"
        participants: [{ "address": "0x...", "balance": "0x...", "token_address": "0x..." }, ...]
        send_eth: "YES" / "NO"
        from_addr / to_addr: 发起/接收地址
        value: 转账金额（整数或0）
        """
        if participants is None:
            participants = []

        action = {
            "action_type": action_type,
            "participants": participants,
            "send_eth": send_eth,
            "from": from_addr or "",
            "to": to_addr or "",
            "value": value or 0,
        }
        # 简单验证（可根据需要扩展）
        if action["send_eth"] not in ("YES", "NO"):
            action["send_eth"] = "NO"
        self.actions.append(action)

    def get_actions(self) -> List[Dict[str, Any]]:
        """返回 actions 列表（引用）"""
        return self.actions

    def get_actions_str(self) -> str:
        """将 actions 格式化为可读的字符串（用于日志/调试）"""
        parts = []
        for i, a in enumerate(self.actions, 1):
            participants = ", ".join(
                [p.get("address", "") for p in a.get("participants", [])]
            ) or "none"
            parts.append(
                f"Action[{i}]: type={a.get('action_type')} op={a.get('operation')} "
                f"participants={participants} send_eth={a.get('send_eth')} "
                f"from={a.get('from')} to={a.get('to')} value={hex(a.get('value')) if isinstance(a.get('value'), int) else a.get('value')}"
            )
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """将节点序列化为字典，包含 actions"""
        return {
            "address": self.address,
            "start_pc": self.start_pc,
            "end_pc": self.end_pc,
            "terminator": self.terminator,
            "total_gas": self.total_gas,
            "instructions": list(self.instructions),
            "actions": self.actions,
        }

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
