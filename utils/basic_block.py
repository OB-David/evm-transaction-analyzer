from typing import List, Dict
from pyevmasm import disassemble_all # 用于反汇编EVM字节码
from utils.evm_information import ContractBytecode


class Block:
    """基本块数据结构（仅保留PC和指令信息）"""
    def __init__(self, start_pc: str, address: str):
        self.address = address          # 合约地址
        self.start_pc = start_pc        # 起始PC（0x开头16进制字符串）
        self.end_pc = None              # 结束PC（0x开头16进制字符串）
        self.instructions = []          # 块内指令：[(pc_hex, opcode_str), ...]
        self.terminator = None          # 终止指令（字符串）
# 当你写 Block(...) 时，Python自动调用这个函数。self是“即将创建的对象自己”，start_pc和address是你提供的参数
    def __repr__(self) -> str:
        return f"Block(start_pc={self.start_pc}, end_pc={self.end_pc}, terminator={self.terminator})"


class BasicBlockProcessor:
    """分块处理器（支持特殊结尾和JUMPDEST开头分块）"""
    def __init__(self):
        # 特殊结尾指令：遇到这些指令时，当前块结束
        self.split_triggers = {
            "JUMP", "JUMPI", "CALL", "CALLCODE", "DELEGATECALL", "STATICCALL",
            "CREATE", "CREATE2", "STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"
        }
        # 特殊开头指令：遇到这些指令时，新块开始（JUMPDEST是跳转目标，必须作为块起点）
        self.start_triggers = {"JUMPDEST"}

    def bytecode_to_opcodes(self, bytecode: str) -> List[Dict]:
        """字节码转指令列表（逻辑不变）"""
        if not bytecode or bytecode == "0x":
            return []
        
        try:
            bytecode_bytes = bytes.fromhex(bytecode[2:]) # 去掉"0x"前缀
            original_instructions = list(disassemble_all(bytecode_bytes)) # 反汇编
            
            adjusted_instructions = []
            for instr in original_instructions:
                pc_hex = f"0x{instr.pc:x}" # 将PC转换为16进制字符串
                adjusted_instructions.append({
                    "pc": pc_hex,
                    "opcode": instr.name
                }) 
            
            return adjusted_instructions
        except Exception as e:
            raise ValueError(f"解析字节码失败: {str(e)}") # 提示错误信息
# 上面这一部分把bytecode去掉0x，然后反汇编，把pc也转成16进制字符串，最后返回一个包含pc和opcode的列表
    def split_into_blocks(self, address: str, instructions: List[Dict]) -> List[Block]:
        """分块逻辑（调整JUMPDEST处理逻辑）"""
        if not instructions:
            return []

        blocks = []
        # 初始化第一个块（使用第一条指令的PC作为起始点）
        current_block = Block(start_pc=instructions[0]["pc"], address=address)

        for idx, instr in enumerate(instructions): # 遍历指令列表
            pc_hex = instr["pc"] 
            opcode_str = instr["opcode"]

            # --------------- 调整后的逻辑：处理JUMPDEST作为新块起点 ---------------
            # 若当前指令是JUMPDEST，且不是当前块的第一条指令，则需要分割
            if opcode_str in self.start_triggers and len(current_block.instructions) > 0:
                # 1. 保存当前块（截止到上一条指令）
                current_block.end_pc = instructions[idx-1]["pc"]  # 上一条指令的PC作为结束点
                current_block.terminator = "JUMPDEST_PREV"  # 标记为被JUMPDEST截断
                blocks.append(current_block)

                # 2. 初始化新块（以当前JUMPDEST的PC为起点）
                current_block = Block(start_pc=pc_hex, address=address)

            # 将当前指令加入当前块
            current_block.instructions.append((pc_hex, opcode_str))

            # --------------- 原有逻辑：处理特殊结尾指令 ---------------
            if opcode_str in self.split_triggers:
                current_block.terminator = opcode_str
                current_block.end_pc = pc_hex  # 当前指令的PC作为结束点
                blocks.append(current_block)

                # 准备下一个块（若有后续指令）
                if idx + 1 < len(instructions):
                    current_block = Block(start_pc=instructions[idx+1]["pc"], address=address)

        # 处理最后一个未被添加的块
        if current_block.instructions and current_block not in blocks: # 检查当前块是否为空且未添加到blocks列表中
            current_block.terminator = "NORMAL_END"
            current_block.end_pc = current_block.instructions[-1][0]
            blocks.append(current_block)

        return blocks
# 上面这一段代码先是定义了一个名为`split_into_blocks`的方法，该方法接收两个参数：`address`和`instructions`。`address`是合约地址，`instructions`是一个包含合约指令的列表。这个方法的主要目的是将合约指令分割成多个基本块（BasicBlock）。
# 接着, 代码遍历指令列表，检查每条指令是否是`JUMPDEST`。如果是，并且当前块不是第一条指令，则会将当前块保存到`blocks`列表中，并初始化一个新的块。然后，将当前指令添加到当前块中。
# 接下来，代码检查当前指令是否是`JUMP`或`JUMPI`，如果是，则将当前块标记为终止块，并保存到`blocks`列表中。然后，根据跳转目标创建一个新的块。
    def process_contract(self, contract: ContractBytecode) -> List[Block]:
        """处理单个合约，返回基本块列表"""
        instructions = self.bytecode_to_opcodes(contract["bytecode"])
        return self.split_into_blocks(contract["address"], instructions)

    def process_multiple_contracts(self, contracts: List[ContractBytecode]) -> List[Block]:
        """批量处理合约"""
        all_blocks = []
        for contract in contracts:
            try:
                blocks = self.process_contract(contract)
                all_blocks.extend(blocks)
                print(f"合约 {contract['address']} 分块完成，共 {len(blocks)} 个基本块")
            except Exception as e:
                print(f"合约 {contract['address']} 处理失败: {str(e)}")
        return all_blocks
