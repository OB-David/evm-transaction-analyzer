from typing import List, Optional, Dict, Any
from utils.basic_block import Block

class BlockNode:
    """基础块节点类（确保初始化actions，具备全局递增ID）"""

    # 所有BlockNode实例共享此计数器
    _node_id_counter = 1
    
    def __init__(self, base_block: Block):
        self.base_block = base_block
        self.address = base_block.address
        self.start_pc = base_block.start_pc
        self.end_pc = base_block.end_pc
        self.instructions = base_block.instructions.copy()
        self.total_gas = 0
        self.actions = []
        self.id = BlockNode._node_id_counter
        BlockNode._node_id_counter += 1

    def add_action(
        self,
        action_type: str,
        erc20_events: Optional[List[Dict[str, Any]]] = None,
        send_eth: str = "NO",
        eth_event: Optional[Dict[str, Any]] = None,
    ) -> None:
        if erc20_events is None:
            erc20_events = []
        if eth_event is None:
            eth_event = {}
        if eth_event and "type" not in eth_event:
            eth_event["type"] = "ETH"

        action = {
            "action_type": action_type,
            "erc20_events": erc20_events,
            "send_eth": "YES" if send_eth == "YES" else "NO",
            "eth_event": eth_event
        }
        self.actions.append(action)

class Edge:
    def __init__(self, source: Any = None, target: Any = None, edge_type: str = "NORMAL", edge_step: int=0):
        self.source = source
        self.target = target
        self.edge_type = edge_type
        self.edge_step = edge_step

class CFG:
    def __init__(self, tx_hash: str):
        self.tx_hash = tx_hash
        self.nodes = []
        self.edges = []

    def add_node(self, node: BlockNode):
        if node not in self.nodes:
            self.nodes.append(node)

    def add_edge(self, source: BlockNode, target: BlockNode, edge_type: str, edge_step: int):

        edge = Edge(source=source, target=target, edge_type=edge_type, edge_step=edge_step)
        self.edges.append(edge)