from typing import List, Dict, TypedDict, Set, Tuple, Optional
import logging
import json
from web3 import Web3
import subprocess
from functools import lru_cache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 标准化数据结构定义
class StandardizedStep(TypedDict):
    address: str  # 0x开头的十六进制字符串
    pc: str       # 0x开头的十六进制字符串
    opcode: str   # 操作码名称
    gascost: int  # gas消耗
    stack: List[str]  # 0x开头的十六进制字符串

class StandardizedTrace(TypedDict):
    tx_hash: str
    steps: List[StandardizedStep]

class ContractBytecode(TypedDict):
    address: str
    bytecode: str

# ERC20核心ABI片段（仅包含必要的检查方法和名称/符号获取方法）
ERC20_ABI_FRAGMENT = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

class TraceFormatter:
    def __init__(self, provider_url: str):
        self.provider_url = provider_url
        self.web3 = Web3(Web3.HTTPProvider(provider_url))
        if not self.web3.is_connected():
            raise ConnectionError("无法连接到以太坊节点，请检查provider URL是否正确")

    # 地址标准化（增加补0逻辑）
    def _normalize_address(self, address: str) -> str:
        """
        标准化以太坊地址格式，确保在0x后、数字前补0以满足42字符长度
        返回: 标准42字符地址(0x+40字符)或空字符串
        """
        if not address:
            return ""
        try:
            address_str = str(address).strip().lower().replace("0x0x", "0x")
            if address_str.startswith("0x"):
                prefix = "0x"
                body = address_str[2:]
            else:
                prefix = "0x"
                body = address_str

            # 处理32字节地址（64字符）转20字节（40字符）
            if len(body) > 40:
                body = body[-40:]

            if len(body) < 40:
                padding = "0" * (40 - len(body))
                body = padding + body

            full_address = f"{prefix}{body}"

            if len(full_address) != 42:
                raise ValueError(f"地址长度异常: {len(full_address)}字符（预期42）")

            checksum_addr = Web3.to_checksum_address(full_address)
            return checksum_addr.lower()

        except Exception as e:
            logger.debug(f"地址标准化失败: {address} - {str(e)}")
            return ""

    # PC标准化
    def _normalize_pc(self, pc: int) -> str:
        return self.web3.to_hex(pc)

    # 栈数据标准化
    def _normalize_stack(self, raw: List[str]) -> List[str]:
        normalized = []
        for item in raw or []:
            if not item:
                normalized.append("0x")
                continue
            str_item = str(item)
            if str_item.startswith("0x"):
                normalized.append(str_item)
            else:
                normalized.append(f"0x{str_item}")
        return normalized

    # 获取当前交易所属区块的矿工地址
    def get_miner_by_tx_hash(self, tx_hash: str) -> Optional[str]:
            tx = self.web3.eth.get_transaction(tx_hash)
            block_number = tx.blockNumber
            
            # 3. 根据区块号获取区块详情，提取miner地址
            block = self.web3.eth.get_block(block_number)
            miner_address = self.web3.to_checksum_address(block.miner)
            return miner_address

    # 获取交易发起者用户地址和根合约合约地址
    def _get_tx_from_to(self, tx_hash: str) -> str:
        try:
            tx = self.web3.eth.get_transaction(tx_hash)
            from_addr = tx.get("from", "")
            to_addr = tx.get("to", "")
            return self._normalize_address(from_addr), self._normalize_address(to_addr)
        except Exception as e:
            logger.error(f"获取交易发起者地址失败: {e}")
            return ""

    # 缓存 get_code 查询，减少 RPC 调用（基于地址）
    @lru_cache(maxsize=1024)
    def _get_code_cached(self, addr_checksum: str) -> bytes:
        try:
            return self.web3.eth.get_code(Web3.to_checksum_address(addr_checksum))
        except Exception as e:
            logger.debug(f"获取字节码 RPC 失败: {addr_checksum} - {e}")
            return b""


    # 识别ERC20 token合约，包括逻辑合约识别
    def identify_erc20_contracts(self, initial_contracts: Set[str], steps: List[StandardizedStep]) -> Tuple[Dict[str, str], Set[str]]:
        """
        参数:
            initial_contracts: 初始识别的合约地址集合
            steps: 标准化的trace步骤列表
        返回:
            erc20_token_map: 所有ERC20地址(代理+逻辑)->代币名称的映射
            all_contracts: 更新后的完整合约地址集合（含逻辑合约）
        """
        erc20_token_map = {}
        all_contracts = set(initial_contracts)  # 复制初始集合避免遍历异常
        
        # 1. 遍历初始合约，识别ERC20代理合约
        for contract_addr in all_contracts:
            norm_addr = self._normalize_address(contract_addr)
            if not norm_addr:
                continue

            try:
                # 检查字节码
                bytecode = self._get_code_cached(norm_addr)
                if not bytecode:
                    continue

                # 调用name()方法判断是否为ERC20
                ONLY_NAME_ABI = [{"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"payable":False,"stateMutability":"view","type":"function"}]
                name_contract = self.web3.eth.contract(address=Web3.to_checksum_address(norm_addr), abi=ONLY_NAME_ABI)
                raw_token_name = name_contract.functions.name().call()
                token_name = str(raw_token_name).strip()
                existing_name = str(erc20_token_map.get(norm_addr, "")).strip()
                if not token_name:
                    if existing_name:
                        token_name = existing_name
                    else:
                        token_name = f"ERC20_{norm_addr[2:10]}"
                        logger.info(f"[{norm_addr}] ERC20名称为空，使用兜底名称: {token_name}")

                # 2. 识别成功：添加代理合约到映射
                erc20_token_map[norm_addr] = token_name
                logger.info(f"[{norm_addr}] 识别为ERC20代理合约: {token_name}")

                # 3. 查找该代理合约的DELEGATECALL目标（逻辑合约）
                for step in steps:
                    if step["opcode"] == "DELEGATECALL" and step["address"] == norm_addr and len(step["stack"]) >= 7:
                        logic_addr = self._normalize_address(step["stack"][-2])
                        existing_logic_name = str(erc20_token_map.get(logic_addr, "")).strip() if logic_addr else ""
                        if logic_addr and not existing_logic_name:
                            erc20_token_map[logic_addr] = f"{token_name}_logic"
                            all_contracts.add(logic_addr)  # 逻辑合约加入总合约集合
                            logger.info(f"[{norm_addr}] 关联逻辑合约: {logic_addr} (名称: {token_name})")

            except Exception as e:
                logger.debug(f"[{contract_addr}] ERC20检查失败: {str(e)}")
                continue

        return erc20_token_map, all_contracts
        
    # 获取代币精度
    @lru_cache(maxsize=1024)
    def get_token_decimals(self, token_address: str) -> int:
        """
        获取代币精度：
        - ERC20代币：返回实际decimals，失败返回18
        - NFT（ERC721/ERC1155）：返回1（NFT无精度概念，兜底值）
        - 其他合约/无效地址：ERC20逻辑失败后返回18
        """
        try:
            # 步骤1：地址格式化校验
            norm_addr = self._normalize_address(token_address)
            if not norm_addr:
                logger.debug(f"代币地址 {token_address} 格式无效，ERC20兜底返回18")
                return 18
            checksum_addr = Web3.to_checksum_address(norm_addr)

            # 步骤2：先判断是否是NFT合约（核心逻辑）
            is_nft, nft_type = self._is_nft_contract(checksum_addr)
            if is_nft:
                logger.debug(f"{token_address} 是{nft_type} NFT，返回精度1")
                return 0

            # 步骤3：非NFT，按ERC20逻辑查询decimals
            DECIMALS_ABI = [{"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
            contract = self.web3.eth.contract(address=checksum_addr, abi=DECIMALS_ABI)
            
            decimals = contract.functions.decimals().call()
            decimals_int = int(decimals)
            # 额外校验：ERC20精度应在1-18之间，避免异常值
            if 1 <= decimals_int <= 18:
                logger.debug(f"获取 {token_address} ERC20精度成功: {decimals_int}")
                return decimals_int
            else:
                logger.warning(f"{token_address} ERC20精度异常({decimals_int})，返回默认18")
                return 18

        except Exception as e:
            # 异常分支：非NFT+ERC20查询失败 → 返回18；NFT判断失败仍走ERC20兜底
            logger.debug(f"获取 {token_address} 精度失败: {e}，非NFT则返回ERC20默认18")
            return 18

    def _is_nft_contract(self, checksum_addr: str) -> tuple[bool, str]:
        """
        内部辅助函数：判断是否是NFT合约（ERC721/ERC1155）
        返回：(是否是NFT, NFT类型/空字符串)
        """
        try:
            # 先检查是否是合约地址（非合约直接排除）
            code = self.web3.eth.get_code(checksum_addr)
            if len(code) == 0:
                return (False, "")

            # 定义supportsInterface ABI（NFT判断核心）
            INTERFACE_ABI = [
                {
                    "constant": True,
                    "inputs": [{"name": "interfaceId", "type": "bytes4"}],
                    "name": "supportsInterface",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]
            contract = self.web3.eth.contract(address=checksum_addr, abi=INTERFACE_ABI)

            # 检查ERC721接口（标准NFT）
            ERC721_INTERFACE_ID = "0x80ac58cd"
            if contract.functions.supportsInterface(ERC721_INTERFACE_ID).call():
                return (True, "ERC721")

            # 检查ERC1155接口（多类型NFT）
            ERC1155_INTERFACE_ID = "0xd9b67a26"
            if contract.functions.supportsInterface(ERC1155_INTERFACE_ID).call():
                return (True, "ERC1155")

            # 兼容非标NFT：检查ERC721核心方法ownerOf
            ERC721_OWNEROF_ABI = [
                {"constant":True,"inputs":[{"name":"tokenId","type":"uint256"}],
                "name":"ownerOf","outputs":[{"name":"","type":"address"}],"type":"function"}
            ]
            erc721_contract = self.web3.eth.contract(address=checksum_addr, abi=ERC721_OWNEROF_ABI)
            try:
                # 仅测试方法是否存在，传入任意tokenId（0）
                erc721_contract.functions.ownerOf(0).call()
                return (True, "ERC721（非标）")
            except:
                pass

            # 非NFT合约
            return (False, "")

        except Exception as e:
            logger.debug(f"判断 {checksum_addr} 是否为NFT失败: {e}，按ERC20处理")
            return (False, "")

    def _strip_0x(self, s: str) -> str:
        '''
        去掉字符串前的 0x 或 0X 前缀
        '''
        if not s:
            return ""
        s2 = str(s)
        if s2.startswith("0x") or s2.startswith("0X"):
            return s2[2:]
        return s2

    def _significant_hex_length(self, raw: str) -> int:
        """
        计算去掉 0x 前缀并去除前导零后的十六进制字符长度
        """
        if not raw:
            return 0
        s = self._strip_0x(raw).lower()
        # 去除前导零
        s = s.lstrip("0")
        return len(s)

    # 获取并标准化trace,计算contract address，并在遍历 CALL 时分类 addresses
    # 改用 foundry 的 cast 方法
    def get_standardized_trace(self, tx_hash: str) -> Dict:
        """
        返回一个 dict，包含至少以下字段：
        - tx_hash
        - steps: 标准化的 steps 列表（保持原来格式）
        - contracts_addresses: list（在遍历 CALL 时识别到的合约地址）
        - erc20_token_map: dict（ERC20合约地址 -> token名称，包含代理和逻辑合约）
        - slot_map: slot -> normalized address 映射（通过 steps 计算）
        - users_addresses: 最终用户地址集合（由 addresses_from_slots 与中间的 users_addresses_from_CALL 合并去重并减去contracts_addresses）
        - tx_sender_address: 交易发起者（from）地址
        说明：
        - users_addresses_from_CALL 仍在函数内部作为中间结果计算，但不会写入返回值
        """
        try:
            # ========== 新增：先获取交易发起者地址 ==========
            tx_sender_address, initial_address = self._get_tx_from_to(tx_hash)
            logger.info(f"交易 {tx_hash} 的发起者地址: {tx_sender_address}")

            cmd = [
                "cast", "rpc",
                "debug_traceTransaction",
                tx_hash,
                '{"enableStack":true,"enableMemory":true,"enableStorage":false,"enableReturnData":false}',
                "-r", self.provider_url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            raw_trace = json.loads(result.stdout)
            logger.info(f"成功获取 trace: {tx_hash}")

            struct_logs = raw_trace.get("structLogs", [])
            steps: List[StandardizedStep] = []

            # 获取矿工地址
            miner_address = self._normalize_address(self.get_miner_by_tx_hash(tx_hash))

            # 记录执行的代码所属合约地址
            current_address = initial_address
            next_address = initial_address
            call_stack = [initial_address] if initial_address else []

            # 记录当前上下文的读写地址
            RW_address = initial_address  
            next_RW_address = initial_address
            RW_stack = [initial_address] if initial_address else []

            # 在遍历时收集 contracts_addresses 和 users_addresses_from_CALL
            contracts_addresses: Set[str] = set()

            contracts_addresses.add(initial_address)

            users_addresses_from_CALL: Set[str] = set()

            for i, step in enumerate(struct_logs):
                pc = step.get("pc", 0)
                opcode = step.get("op", "").upper()
                raw_stack = step.get("stack", [])
                raw_memory = step.get("memory",[])
                depth = step.get("depth",[])
                # 单独处理CALL合约时的gascost计算
                # 执行CALL时会向合约预支付一笔gas，在trace中记录为CALL的gasCost
                # CALL本身的gascost是预支付的gasCost减去CALL下一步剩下的gasleft。
                if opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"} and struct_logs[i + 1].get("address","") != step.get("address",""):
                    next_gasleft = struct_logs[i + 1].get("gas", 0)
                    gasCost = step.get("gasCost", 0)
                    gascost = gasCost - next_gasleft
                else:
                    if i < len(struct_logs) - 1:
                        gascost = step.get("gasCost", 0)
                    else:
                        gascost = 0  # 最后一步一定是终止指令，gascost固定是0

                # CALL 类指令,增加地址分类逻辑
                if opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}:
                    if len(raw_stack) >= 7:
                        # 1. 从 raw_stack[-2] 解析出地址（保持原变量名/索引）
                        to_address_raw = raw_stack[-2]

                        # 先判断 hex 位数是否大于 2（按去 0x 并去前导 0 的长度）
                        hex_len = self._significant_hex_length(to_address_raw)

                        # 默认不认为是有效地址，只有经过标准化才认为有效（is_valid_address 用于上下文切换）
                        to_address = ""
                        is_valid_address = False

                        # 预先判断下一步 pc 是否为 0x0（用于新的合约/用户分类）
                        has_next_step = i < len(struct_logs) - 1
                        next_step_pc = None
                        if has_next_step:
                            next_step_pc = self._normalize_pc(struct_logs[i + 1].get("pc", 0))
                        is_next_pc_zero = has_next_step and next_step_pc == "0x0"

                        # 只有当 hex_len > 2 时才进行标准化与分类（不再通过 bytecode 查询判断）
                        if hex_len > 2:
                            # 先标准化
                            norm_addr = self._normalize_address(to_address_raw)
                            if norm_addr:
                                to_address = norm_addr
                                is_valid_address = True

                                # 如果下一步 pc 是 0x0，则视为合约地址；否则在 hex_len 在 10-40 时视为用户地址
                                if is_next_pc_zero:
                                    contracts_addresses.add(to_address)
                                else:
                                    if 10 <= hex_len <= 40:
                                        users_addresses_from_CALL.add(to_address)
                            else:
                                # 标准化失败，保持 to_address 为空，is_valid_address=False
                                pass
                        else:
                            # hex_len <= 2：被视为预编译合约或特殊地址，忽略（不标准化、不分类）
                            pass

                        # 保持原来根据 is_valid_address & is_next_pc_zero 切换上下文的逻辑不变
                        if is_valid_address and is_next_pc_zero:
                            call_stack.append(current_address)
                            next_address = to_address
                            if opcode in {"CALL", "CALLCODE", "STATICCALL"}:
                                RW_stack.append(RW_address)
                                next_RW_address = to_address
                        else:
                            next_address = current_address
                            next_RW_address = RW_address
                    else:
                        next_address = current_address
                        next_RW_address = RW_address

                # CREATE 类指令
                elif opcode in ["CREATE", "CREATE2"]:
                    new_address = ""
                    if new_address:
                        new_address = self._normalize_address(new_address)
                        has_next_step = i < len(struct_logs) - 1
                        if has_next_step:
                            next_step_pc = self._normalize_pc(struct_logs[i + 1].get("pc", 0))
                            if next_step_pc == "0x0" and new_address:
                                call_stack.append(current_address)
                                next_address = new_address
                                RW_stack.append(current_address)
                                next_RW_address = new_address
                            else:
                                next_address = current_address
                                next_RW_address = RW_address
                        else:
                            next_address = current_address
                            next_RW_address = RW_address
                    else:
                        next_address = current_address
                        next_RW_address = RW_address

                # 终止指令
                elif opcode in {"STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"}:
                    if len(call_stack) > 1:
                        next_address = call_stack.pop()
                    else:
                        next_address = current_address
                    if len(RW_stack) > 1:
                        next_RW_address = RW_stack.pop()
                    else:
                        next_RW_address = RW_address

                # 记录当前步骤（保持原来格式）
                steps.append({
                    "address": current_address,
                    "RW_address": RW_address,
                    "depth": depth,
                    "pc": self._normalize_pc(pc),
                    "opcode": opcode,
                    "gascost": gascost,
                    "stack": self._normalize_stack(raw_stack),
                    "memory": raw_memory
                })

                current_address = next_address
                RW_address = next_RW_address

            # 中间过程 users_addresses_from_CALL 已收集完毕（但不返回）
            print(f"通过 CALL 类指令识别到合约地址数量: {len(contracts_addresses)}，用户地址数量: {len(users_addresses_from_CALL)}")
            
            # 识别所有ERC20合约（代理合约+逻辑合约）
            erc20_token_map, contracts_addresses = self.identify_erc20_contracts(contracts_addresses, steps)
            # 可选：打印识别结果
            print(f"识别出ERC20代理+逻辑合约总数: {len(erc20_token_map)}，更新后合约地址总数: {len(contracts_addresses)}") 

            # final_users_addresses = （addresses_from_slots ∪ users_addresses_from_CALL \\ contracts_addresses）
            slot_map = self.extract_slot_address_map({"steps": steps})
            addresses_from_slots: Set[str] = set(slot_map.values())
            print(f"通过 slot_map 识别到地址数量: {len(addresses_from_slots)}")
            final_users_addresses_set: Set[str] = (addresses_from_slots.union(users_addresses_from_CALL)) - contracts_addresses

            # ========== 新增：将交易发起者加入用户地址集合 ==========
            if tx_sender_address and tx_sender_address not in contracts_addresses:
                final_users_addresses_set.add(tx_sender_address)
                logger.info(f"已将交易发起者 {tx_sender_address} 加入用户地址集合")

            # 给所有地址命名，构建全地址-名称映射表（包含合约地址和用户地址）
            # ========== 修改：传入交易发起者地址 ==========
            full_address_name_map = self._build_full_address_name_map(
                contracts_addresses=contracts_addresses, 
                erc20_token_map=erc20_token_map, 
                users_addresses=final_users_addresses_set,
                tx_sender_address=tx_sender_address,
                initial_address=initial_address,
                miner_address=miner_address
            )

            # 返回时新增 erc20_token_map 和 tx_sender_address 字段
            return {
                "tx_hash": tx_hash,
                "steps": steps,
                "contracts_addresses": sorted(list(contracts_addresses)),
                "erc20_token_map": erc20_token_map,  # 新增：ERC20地址->名称映射（包含代理和逻辑合约）
                "slot_map": slot_map,
                "users_addresses": sorted(list(final_users_addresses_set)),
                "full_address_name_map": full_address_name_map,
                "tx_sender_address": tx_sender_address  # 新增：交易发起者地址
            }

        except Exception as e:
            logger.error(f"处理trace失败: {e}")
            raise

    # 提取合约地址（保留原有简单实现）
    def extract_contracts_from_trace(self, standardized_trace: StandardizedTrace) -> Set[str]:
        return {step["address"] for step in standardized_trace["steps"] if step["address"]}

    # 从 trace 中为 slot 尝试寻找对应地址（按照你提供的算法，尊重原栈位置）
    def extract_slot_address_map(self, standardized_trace: Dict) -> Dict[str, str]:
        """
        算法（精简版）：
        - 收集所有 SSTORE/SLOAD 的 slot（取栈顶 st[-1]）
        - 找到首次将该 slot 用于 keccak 的 SHA3 指令（SHA3 的下一 step 的栈顶等于 slot）
        - 向前找最多两个 MSTORE（靠近 SHA3 的优先），并把每个 MSTORE 的 stack[-2] 作为地址候选
        - 筛选候选：只保留 significant hex length 在 [20,40] 的候选
        - 选择策略：
            * 若只有一个候选，选它
            * 若两个候选，比较它们各自的 top（stack[-1]）解析后的整数值，优先选能解析且值更小者；若都无法解析，选靠近SHA3的那个
        - 返回 slot -> normalized address 映射（仅添加能被 normalize 成合法地址的项）
        说明：日志降级为 debug 以减少噪音，只有最终添加到 slot_map 时会以 info 记录。
        """
        steps = standardized_trace.get("steps", []) if isinstance(standardized_trace, dict) else standardized_trace["steps"]

        slot_set: Set[str] = set()
        # 收集所有 slot（从 SSTORE/SLOAD 的栈顶 st[-1]）
        for step in steps:
            if step["opcode"] in {"SSTORE", "SLOAD"}:
                st = step.get("stack", []) or []
                if len(st) >= 1:
                    slot_set.add(st[-1].lower())
        logger.debug(f"[slot_map]待处理 slot 列表: {slot_set}")

        def hex_to_int_inner(s: str) -> Optional[int]:
            # 期望 s 为带 0x 的 hex 字符串或其他可解析的 hex 表示
            if not s:
                return None
            try:
                s2 = str(s).strip().lower()
                if s2.startswith("0x"):
                    s2 = s2[2:]
                if s2 == "":
                    return 0
                return int(s2, 16)
            except Exception:
                return None

        def significant_hex_length_raw(s: str) -> int:
            """去掉 0x 前缀并去除前导零后返回十六进制字符长度"""
            if not s:
                return 0
            s2 = str(s).lower()
            if s2.startswith("0x"):
                s2 = s2[2:]
            s2 = s2.lstrip("0")
            return len(s2)

        slot_map: Dict[str, str] = {}

        for slot in slot_set:
            slot_int = hex_to_int_inner(slot)
            if slot_int is None:
                logger.debug(f"[slot_map] skip slot (cannot parse to int): {slot}")
                continue

            # 找到首次将 slot 写入 keccak 的 SHA3 指令索引（SHA3 的下一 step 的栈顶等于 slot）
            sha3_index = None
            for i, step in enumerate(steps):
                if step["opcode"] in {"SHA3", "KECCAK256", "KECCAK"}:
                    if i + 1 < len(steps):
                        next_stack = steps[i + 1].get("stack", []) or []
                        if len(next_stack) >= 1:
                            top_val = hex_to_int_inner(next_stack[-1])
                            if top_val is not None and top_val == slot_int:
                                sha3_index = i
                                break

            if sha3_index is None:
                logger.debug(f"[slot_map] no SHA3 usage found for slot {slot}")
                continue

            # 向前找最多两个 MSTORE（靠近 SHA3 的先找到）
            mstore_candidates = []
            j = sha3_index - 1
            while j >= 0 and len(mstore_candidates) < 2:
                if steps[j]["opcode"] == "MSTORE":
                    mstore_candidates.append((j, steps[j]))
                j -= 1

            if not mstore_candidates:
                logger.debug(f"[slot_map] no MSTORE found before SHA3 for slot {slot}")
                continue

            # 从每个 MSTORE 提取 stack[-2] 作为候选地址，并记录其 stack[-1]（用于比较）
            parsed_candidates = []
            for idx, mstep in mstore_candidates:
                mstack = mstep.get("stack", []) or []
                if len(mstack) >= 2:
                    cand_raw = mstack[-2]  # 地址候选来自 MSTORE 的栈顶第二个元素
                    cand_top = mstack[-1]
                    parsed_candidates.append({
                        "raw": cand_raw,
                        "top": cand_top,
                        "mstep_idx": idx
                    })

            if not parsed_candidates:
                logger.debug(f"[slot_map] no parsed candidates for slot {slot}")
                continue

            # 筛选满足 20-40 hex 位（去 0x 并去前导零）的候选
            valid_candidates = [c for c in parsed_candidates if 20 <= significant_hex_length_raw(c["raw"]) <= 40]

            chosen_addr = ""

            if len(valid_candidates) == 1:
                normalized_candidate = self._normalize_address(valid_candidates[0]["raw"])
                if normalized_candidate:
                    chosen_addr = normalized_candidate
            elif len(valid_candidates) == 2:
                # 两个候选都满足长度限制，比较各自 top 值
                def top_int(c):
                    return hex_to_int_inner(c["top"])

                t0 = top_int(valid_candidates[0])
                t1 = top_int(valid_candidates[1])

                # 选择逻辑精简：优先选择能解析且值较小者；若只有一个可解析则选它；若都不可解析则选靠近 SHA3 的（列表顺序保证）
                if t0 is None and t1 is not None:
                    pick = valid_candidates[1]
                elif t0 is not None and t1 is None:
                    pick = valid_candidates[0]
                elif t0 is not None and t1 is not None:
                    pick = valid_candidates[0] if t0 <= t1 else valid_candidates[1]
                else:
                    pick = valid_candidates[0]

                normalized_candidate = self._normalize_address(pick["raw"])
                if normalized_candidate:
                    chosen_addr = normalized_candidate

            # 若选到地址则加入映射（并做一次标准化校验）
            if chosen_addr:
                norm_chosen = self._normalize_address(chosen_addr)
                if norm_chosen:
                    slot_map[slot] = norm_chosen

        return slot_map

    # 获取单个合约字节码（使用缓存）
    def get_contract_bytecode(self, contract_address: str) -> ContractBytecode:
        normalized_addr = self._normalize_address(contract_address)
        if not normalized_addr or not self.web3.is_address(normalized_addr):
            raise ValueError(f"无效地址（需0x开头的十六进制）: {contract_address}")

        try:
            bytecode = self._get_code_cached(normalized_addr)
            return {
                "address": normalized_addr,
                "bytecode": self.web3.to_hex(bytecode)
            }
        except Exception as e:
            logger.error(f"获取合约字节码失败: {e}")
            raise

    # 获取所有涉及的合约字节码
    def get_all_contracts_bytecode(self, all_contracts) -> List[ContractBytecode]:
        return [self.get_contract_bytecode(addr) for addr in all_contracts if addr]

    #  tx_sender_address 参数，优先命名为 User_From
    def _build_full_address_name_map(
        self,
        contracts_addresses: Set[str],
        erc20_token_map: Dict[str, str],
        users_addresses: Set[str],
        tx_sender_address: str = "",    #交易发起者地址
        initial_address: str = "", # 根合约地址
        miner_address: str = ""  # 矿工地址
    ) -> Dict[str, str]:
        """
        构建全地址-名称映射表：
        - 交易发起者：优先命名为 User_From
        - 根合约： 优先命名为 contract_to
        - ERC20合约：使用token名称
        - 非ERC20合约：contract_a、contract_b...
        - 用户地址：User_A、User_B...
        """
        full_name_map = {}
        
        # 1. 处理ERC20合约（优先级最高）
        for addr, name in erc20_token_map.items():
            cleaned_name = str(name).strip() if name is not None else ""
            if not cleaned_name:
                addr_text = str(addr)
                short = addr_text[2:10] if addr_text.startswith("0x") else addr_text[:8]
                cleaned_name = f"ERC20_{short}"
            full_name_map[addr] = cleaned_name
        
        # 2. 处理非ERC20合约
        non_erc20_contracts = [addr for addr in contracts_addresses if addr not in full_name_map]
        for idx, addr in enumerate(non_erc20_contracts):
            if addr == initial_address: # 优先处理根合约
                full_name_map[addr] = "contract_to"
            else: # 生成contract_a、contract_b...
                contract_suffix = chr(ord('a') + idx)
                full_name_map[addr] = f"contract_{contract_suffix}"
        
        # 3. 处理用户地址
        sorted_users = sorted(list(users_addresses))
        
        # 3.1 先处理交易发起者，命名为 User_From
        if tx_sender_address and tx_sender_address in sorted_users:
            full_name_map[tx_sender_address] = "User_From"
            # 从排序后的列表中移除，避免重复命名
            sorted_users.remove(tx_sender_address)

        # 3.2 处理矿工地址，命名为 Miner
        if miner_address and miner_address in sorted_users:
            full_name_map[miner_address] = "Miner"
            sorted_users.remove(miner_address)

        # 3.2 处理剩余用户地址：User_A、User_B...
        for idx, addr in enumerate(sorted_users):
            # 生成User_A、User_B...（A=0, B=1...）
            user_suffix = chr(ord('A') + idx)
            full_name_map[addr] = f"User_{user_suffix}"
        
        return full_name_map
