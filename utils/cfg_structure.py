# cfg_structures.py负责定义CFG图的核心数据结构

from typing import List, Optional, Dict, Any
from utils.basic_block import Block


class BlockNode:
    """CFG 中的节点（对应唯一的 basic_block）。
    简洁版：ERC20event 为列表项，顶层键为 "read" 或 "write"，balance 必须为十六进制字符串 (e.g. "0x1f4")。
    ETHevent 为单个字典，包含 "from", "to", "value"（value 保留为整数）。"""

    def __init__(self, base_block: Block):
        self.base_block = base_block
        self.address = base_block.address
        self.start_pc = base_block.start_pc
        self.end_pc = base_block.end_pc
        self.terminator = base_block.terminator
        self.total_gas: int = 0
        # actions: 每个 action 包含 action_type, ERC20event, ETHevent, send_eth
        # ERC20event: List[{"read":  {"address": "0x...", "balance": "0x..."}},
        #               {"write": {"address": "0x...", "balance": "0x..."}}]
        # ETHevent: {"from": "0x...", "to": "0x...", "value": 12345}
        self.actions: List[Dict[str, Any]] = []
        self.instructions = base_block.instructions


    def __repr__(self) -> str:
        return (f"BlockNode(addr={self.address[:8]}..., start_pc={self.start_pc}, "
                f"instr_count={len(self.instructions)}, total_gas={self.total_gas}, "
                f"actions={len(self.actions)})")

    def get_instructions_str(self) -> str:
        return "\n".join([f"{pc}: {opcode}" for pc, opcode in self.instructions])

    def add_action(
        self,
        action_type: str,
        ERC20event: Optional[List[Dict[str, Any]]] = None,
        send_eth: str = "NO",
        ETHevent: Optional[Dict[str, Any]] = None,
        
    ) -> None:
        """
        添加 action（不做额外归一化，假定调用方保证格式正确）。

        ERC20event 示例:
          [{"read":  {"address": "0xAa...", "balance": "0x1f4"}},
           {"write": {"address": "0xBb...", "balance": "0x0"}}]

        ETHevent 示例:
          {"from": "0xSender...", "to": "0xReceiver...", "value": 1000000000000000000}
        """
        if ERC20event is None:
            ERC20event = []

        action = {
            "action_type": action_type,
            "ERC20event": ERC20event,
            "send_eth": send_eth,
            "ETHevent": ETHevent or {},
            
        }
        if action["send_eth"] not in ("YES", "NO"):
            action["send_eth"] = "NO"
        self.actions.append(action)

    def get_actions(self) -> List[Dict[str, Any]]:
        return self.actions

    def get_actions_str(self) -> str:
        parts = []
        for i, a in enumerate(self.actions, 1):
            # 1. 处理ERC20事件（过滤空值，无有效信息则为空）
            erc_events = a.get("ERC20event", []) or []
            erc_strs = []
            if erc_events:
                for e in erc_events:
                    k = list(e.keys())[0]
                    inner = e[k]
                    addr = inner.get("address", "")
                    bal = inner.get("balance", "")
                    if addr or bal:  # 仅保留有有效信息的ERC20事件
                        erc_strs.append(f"[{k} addr={addr} balance={bal}]")
            erc_summary = ", ".join(erc_strs) if erc_strs else ""

            # 2. 处理ETH事件（过滤空值，无有效信息则为空）
            eth = a.get("ETHevent") or {}
            eth_summary = ""
            if eth:
                fr = eth.get("from", "")
                to = eth.get("to", "")
                val = eth.get("value", "")
                val_str = hex(val) if isinstance(val, int) else str(val)
                if fr or to or val:  # 仅保留有有效信息的ETH事件
                    eth_summary = f"[from={fr} to={to} value={val_str}]"

            # 3. 构建单条Action（只保留有效字段，过滤none/空）
            action_parts = []
            if a.get("action_type") != "none":  # 过滤none类型
                action_parts.append(f"type={a.get('action_type')}")
            if erc_summary:  # 过滤空ERC20事件
                action_parts.append(f"ERC20event={erc_summary}")
            if eth_summary:  # 过滤空ETH事件
                action_parts.append(f"ETHevent={eth_summary}")
            if a.get("send_eth") == "YES":  # 仅显示YES，NO不展示
                action_parts.append(f"send_eth=YES")

            # 4. 仅当有有效内容时，添加这条Action
            if action_parts:
                parts.append(f"Action[{i}]: {' '.join(action_parts)}")

        # 无有效Action则返回空字符串（而非none）
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
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


    def __repr__(self) -> str:
        return f"CFG(tx_hash={self.tx_hash}, nodes={len(self.nodes)}, edges={len(self.edges)})"
