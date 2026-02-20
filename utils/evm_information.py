# evm_information.py 负责从节点上获取所有必要信息；
# 这些信息包括交易的标准化trace、涉及的contract address以及对应的bytecode；
# 包含对trace的结构定义；
# 包含获取每个step对应的contract address的逻辑；
# 包含获取contracts addresses, users addresses和slot_map的逻辑；
# 所有结果都结构化存在StandardizedTrace中

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
    def _normalize_stack(self, raw_stack: List[str]) -> List[str]:
        normalized = []
        for item in raw_stack or []:
            if not item:
                normalized.append("0x")
                continue
            str_item = str(item)
            if str_item.startswith("0x"):
                normalized.append(str_item)
            else:
                normalized.append(f"0x{str_item}")
        return normalized

    # 获取交易初始目标地址
    def _get_initial_address(self, tx_hash: str) -> str:
        tx = self.web3.eth.get_transaction(tx_hash)
        return tx.get("to", "")

    # 缓存 get_code 查询，减少 RPC 调用（基于地址）
    @lru_cache(maxsize=1024)
    def _get_code_cached(self, addr_checksum: str) -> bytes:
        '''
        使用缓存获取合约字节码
        '''
        try:
            return self.web3.eth.get_code(Web3.to_checksum_address(addr_checksum))
        except Exception as e:
            logger.debug(f"获取字节码 RPC 失败: {addr_checksum} - {e}")
            return b""

    # 替换原有 _check_if_erc20_and_get_name 函数
    @lru_cache(maxsize=1024)
    def _check_if_erc20_and_get_name(self, contract_address: str) -> Tuple[bool, str]:
        """
        极简版ERC20检查：仅判断能否成功调用name()并获取非空名称（symbol不算）
        返回: (是否是代币, token名称)
        逻辑：
        1. 合约有非空字节码
        2. 能成功调用name()方法且返回非空字符串
        3. 排除名称含uniswap/v2/v3等关键词的合约（避免误判）
        """
        try:
            # 1. 标准化地址
            norm_addr = self._normalize_address(contract_address)
            if not norm_addr:
                return (False, "")
            checksum_addr = Web3.to_checksum_address(norm_addr)
            
            # 2. 检查是否有字节码（空字节码不是合约）
            bytecode = self._get_code_cached(norm_addr)
            if not bytecode:
                logger.debug(f"[{norm_addr}] 无字节码，不是代币")
                return (False, "")
            
            # 3. 仅定义name()方法的ABI（symbol不算）
            ONLY_NAME_ABI = [
                {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"payable":False,"stateMutability":"view","type":"function"}
            ]
            name_contract = self.web3.eth.contract(address=checksum_addr, abi=ONLY_NAME_ABI)
            
            # 4. 调用name()方法，仅保留非空结果（symbol不算）
            token_name = ""
            try:
                # 调用name()并去除首尾空格
                token_name = name_contract.functions.name().call().strip()
                # 确保名称非空（空字符串不算）
                if not token_name:
                    logger.debug(f"[{norm_addr}] name()返回空字符串，不是代币")
                    return (False, "")
            except Exception as e:
                # 调用失败（无name()方法），直接判定不是代币
                logger.debug(f"[{norm_addr}] 无name()方法或调用失败: {str(e)}")
                return (False, "")
            
            # 5. 排除常见合约（避免误判）
            keywords = ["swap", "pair", "router", "transfer","order"]
            if any(keyword in token_name.lower() for keyword in keywords):
                logger.debug(f"[{norm_addr}] 名称包含关键词，排除: {token_name}")
                return (False, "")
            
            logger.info(f"[{norm_addr}] 识别为代币，名称: {token_name}")
            return (True, token_name)
        
        except Exception as e:
            logger.debug(f"[{contract_address}] 代币检查失败: {str(e)}")
            return (False, "") 
    
    @lru_cache(maxsize=1024)
    def _get_contract_identity(self, contract_address: str) -> Optional[str]:
        """
        改进版身份识别：
        1. 针对 Uniswap V2 等有 name() 的池子，保留其原名。
        2. 针对 Uniswap V3 等无 name() 的池子，通过 token0/token1 自动生成名称。
        3. 增加 V3 Fee 手续费显示。
        """
        try:
            norm_addr = self._normalize_address(contract_address)
            if not norm_addr: return None
            checksum_addr = Web3.to_checksum_address(norm_addr)
            
            bytecode = self._get_code_cached(norm_addr)
            if not bytecode or bytecode in [b'\x00', '0x']: return None

            # 扩展 ABI 以支持 token0/1 和 fee
            COMBINED_ABI = [
                {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
                {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
                {"constant":True,"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"type":"function"},
                {"constant":True,"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"type":"function"},
                {"constant":True,"inputs":[],"name":"fee","outputs":[{"name":"","type":"uint24"}],"type":"function"}
            ]
            contract = self.web3.eth.contract(address=checksum_addr, abi=COMBINED_ABI)

            # 1. 尝试获取显示名称
            display_name = ""
            try:
                display_name = contract.functions.name().call().strip()
            except:
                try:
                    display_name = contract.functions.symbol().call().strip()
                except:
                    display_name = ""

            # 2. 识别是否为 Pool 并提取基础信息
            t0_addr, t1_addr, fee_str = None, None, ""
            is_pool = False
            try:
                t0_addr = contract.functions.token0().call()
                t1_addr = contract.functions.token1().call()
                is_pool = True
                # 尝试获取 V3 Fee (3000 = 0.3%)
                try:
                    f_val = contract.functions.fee().call()
                    fee_str = f" ({f_val/10000}%)"
                except:
                    pass
            except:
                pass

            # 3. 逻辑合并
            if is_pool:
                # 如果是 Pool 且没有名字（典型 V3），则构造名称
                if not display_name:
                    s0 = self._get_token_symbol(t0_addr)
                    s1 = self._get_token_symbol(t1_addr)
                    display_name = f"{s0}/{s1}{fee_str}"
                
                return f"Pool: {display_name}"
            
            elif display_name:
                return display_name # 纯代币名或普通合约名
            
            return None
        except:
            return None
        
    # 获取代币精度
    @lru_cache(maxsize=1024)
    def get_token_decimals(self, token_address: str) -> int:
        """
        获取 ERC20 代币的精度（decimals），失败时返回 18
        """
        try:
            norm_addr = self._normalize_address(token_address)
            if not norm_addr:
                return 18
            checksum_addr = Web3.to_checksum_address(norm_addr)

            # 只包含 decimals() 的 ABI
            DECIMALS_ABI = [{"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
            contract = self.web3.eth.contract(address=checksum_addr, abi=DECIMALS_ABI)

            decimals = contract.functions.decimals().call()
            return int(decimals)
        except Exception as e:
            logger.debug(f"获取 {token_address} 精度失败: {e}，使用默认 18")
            return 18

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
        - erc20_token_map: dict（ERC20合约地址 -> token名称）
        - slot_map: slot -> normalized address 映射（通过 steps 计算）
        - users_addresses: 最终用户地址集合（由 addresses_from_slots 与中间的 users_addresses_from_CALL 合并去重并减去contracts_addresses）
        说明：
        - users_addresses_from_CALL 仍在函数内部作为中间结果计算，但不会写入返回值
        """
        try:
            cmd = [
                "cast", "rpc",
                "debug_traceTransaction",
                tx_hash,
                '{"enableMemory":true,"disableStack":false,"disableStorage":false,"enableReturnData":true}',
                "-r", self.provider_url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            raw_trace = json.loads(result.stdout)
            logger.info(f"成功获取 trace: {tx_hash}")

            struct_logs = raw_trace.get("structLogs", [])
            steps: List[StandardizedStep] = []

            # initial addresses and call stack (原有逻辑)
            initial_address = self._normalize_address(self._get_initial_address(tx_hash))
            current_address = initial_address
            next_address = initial_address
            call_stack = [initial_address] if initial_address else []

            # 新增：在遍历时收集 contracts_addresses 和 users_addresses_from_CALL（后者为中间变量，不返回）
            contracts_addresses: Set[str] = set()
            users_addresses_from_CALL: Set[str] = set()

            for i, step in enumerate(struct_logs):
                pc = step.get("pc", 0)
                opcode = step.get("op", "").upper()
                raw_stack = step.get("stack", [])
                # gascost是该step的gasleft减去下一step的gasleft, 除非遇到终止指令
                if i < len(struct_logs) - 1:
                    gasleft = step.get("gas", 0)
                    next_gasleft = struct_logs[i + 1].get("gas", 0)
                    gascost = gasleft - next_gasleft
                else:
                    gascost = 0  # 最后一步一定是终止指令，gascost固定是0
                # gas计算补丁
                if opcode in {"STOP", "RETURN", "REVERT"}:
                    gascost = 0  # 终止指令的gascost固定为0

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

                                # 新逻辑：如果下一步 pc 是 0x0，则视为合约地址；否则在 hex_len 在 10-40 时视为用户地址
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
                        else:
                            next_address = current_address
                    else:
                        next_address = current_address

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
                            else:
                                next_address = current_address
                        else:
                            next_address = current_address
                    else:
                        next_address = current_address

                # 终止指令
                elif opcode in {"STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"}:
                    if len(call_stack) > 1:
                        next_address = call_stack.pop()
                    else:
                        next_address = current_address

                # 记录当前步骤（保持原来格式）
                steps.append({
                    "address": current_address,
                    "pc": self._normalize_pc(pc),
                    "opcode": opcode,
                    "gascost": gascost,
                    "stack": self._normalize_stack(raw_stack)
                })

                current_address = next_address

            # 中间过程 users_addresses_from_CALL 已收集完毕（但不返回）
            print(f"通过 CALL 类指令识别到合约地址数量: {len(contracts_addresses)}，用户地址数量: {len(users_addresses_from_CALL)}")
            
            # ========== 新增：检查ERC20代币并建立地址-名称映射 ==========
            erc20_token_map: Dict[str, str] = {}
            for contract_addr in contracts_addresses:
                is_erc20, token_name = self._check_if_erc20_and_get_name(contract_addr)
                if is_erc20:
                    erc20_token_map[contract_addr] = token_name or "未知ERC20代币"
            print(f"识别出ERC20代币数量: {len(erc20_token_map)}")\
            
            # 识别合约名称
            contract_name_map = {}
            contract_name_map.update(erc20_token_map)
            # 识别 Pool
            logger.info("正在识别合约身份(Token/Pool)...")
            for addr in contracts_addresses:
                # 如果不是 ERC20 (不在映射里)，尝试识别是否为 Pool
                if addr not in contract_name_map:
                    contract_identity = self._get_contract_identity(addr)
                    if contract_identity:
                        contract_name_map[addr] = contract_identity      

            # ========== 原有逻辑继续 ==========
            # final_users_addresses = （addresses_from_slots ∪ users_addresses_from_CALL \\ contracts_addresses）
            slot_map = self.extract_slot_address_map({"steps": steps})
            addresses_from_slots: Set[str] = set(slot_map.values())
            print(f"通过 slot_map 识别到地址数量: {len(addresses_from_slots)}")
            final_users_addresses_set: Set[str] = (addresses_from_slots.union(users_addresses_from_CALL)) - contracts_addresses

            # 返回时新增 erc20_token_map 字段
            return {
                "tx_hash": tx_hash,
                "steps": steps,
                "contracts_addresses": sorted(list(contracts_addresses)),
                "erc20_token_map": erc20_token_map,  # 新增：ERC20地址->名称映射
                "slot_map": slot_map,
                "users_addresses": sorted(list(final_users_addresses_set)),
                "contract_name_map": contract_name_map
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
