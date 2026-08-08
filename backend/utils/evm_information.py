from typing import List, Dict, TypedDict, Set, Tuple, Optional
import logging
import json
import time
from web3 import Web3
import subprocess
from functools import lru_cache

from utils.drpc_trace import (
    DrpcTraceError,
    _memory_words,
    fetch_drpc_trace,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GETH_TRACE_START_BLOCK = 25676797
GETH_RPC_TIMEOUT_SECONDS = 600
GETH_TRACE_RETRIES = 2
GETH_RETRY_DELAY_SECONDS = 1.0
DRPC_CHUNK_GAS_THRESHOLD = 2000000

# Geth's native struct logger efficiently records opcode/stack data, but full
# memory and cumulative storage make its response enormous. This second,
# deliberately small tracer emits only memory snapshots at possible change
# boundaries and storage deltas at SLOAD/SSTORE steps. Python merges both
# traces by the stable step index.
GETH_STATE_DELTA_TRACER = (
    "{deltas:[],count:0,prevOp:'',prevDepth:0,faults:[],"
    "fault:function(log){this.faults.push([this.count,log.getPC(),"
    "String(log.getError())]);},"
    "step:function(log){var index=this.count++,op=log.op.toString(),"
    "depth=log.getDepth(),memory=null,slot=null,value=null;"
    "var memoryOps='|MLOAD|MSTORE|MSTORE8|SHA3|KECCAK256|CALLDATACOPY|"
    "CODECOPY|EXTCODECOPY|RETURNDATACOPY|MCOPY|LOG0|LOG1|LOG2|LOG3|LOG4|"
    "CREATE|CREATE2|CALL|CALLCODE|DELEGATECALL|STATICCALL|RETURN|REVERT|';"
    "if(index===0||memoryOps.indexOf('|'+this.prevOp+'|')>=0||"
    "depth!==this.prevDepth){memory=toHex(log.memory.slice(0,"
    "log.memory.length()));}"
    "if((op==='SLOAD'||op==='SSTORE')&&log.stack.length()>0){"
    "slot='0x'+log.stack.peek(0).toString(16);"
    "if(op==='SSTORE'&&log.stack.length()>1){"
    "value='0x'+log.stack.peek(1).toString(16);}}"
    "if(memory!==null||slot!==null){"
    "this.deltas.push([index,memory,slot,value]);}"
    "this.prevOp=op;this.prevDepth=depth;},"
    "result:function(){return {deltas:this.deltas,faults:this.faults,"
    "totalSteps:this.count};}}"
)


class TraceFetchError(RuntimeError):
    """A raw transaction trace could not be fetched or validated."""


class NoOpcodeTraceError(TraceFetchError):
    """The transaction completed without executing EVM opcodes."""

# 标准化数据结构定义
class StandardizedStep(TypedDict):
    address: str  # 0x开头的十六进制字符串
    pc: str       # 0x开头的十六进制字符串
    opcode: str   # 操作码名称
    gascost: int  # gas消耗
    stack: List[str]  # 0x开头的十六进制字符串
    memory: List[str]
    storage: Dict[str, str]

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
        最终通用版：识别真实ERC20代币，自动排除所有 DEX 流动性池（V2/V3/通用）
        无黑名单、全链兼容、不会误伤任何代币
        """
        erc20_token_map = {}
        all_contracts = set(initial_contracts)

        # 标准 ERC20 最小 ABI
        ERC20_MINI_ABI = [
            {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"},
            {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
        ]

        # 【通用】所有 AMM DEX 池一定会有的方法（普通代币绝对没有）
        AMM_POOL_DETECT_ABI = [
            {"constant":True,"inputs":[],"name":"factory","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
            {"constant":True,"inputs":[],"name":"token0","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
        ]

        for contract_addr in all_contracts:
            norm_addr = self._normalize_address(contract_addr)
            if not norm_addr:
                continue

            try:
                bytecode = self._get_code_cached(norm_addr)
                if not bytecode or len(bytecode) < 20:
                    continue

                checksum_addr = Web3.to_checksum_address(norm_addr)
                token_contract = self.web3.eth.contract(address=checksum_addr, abi=ERC20_MINI_ABI)

                # --------------------------
                # 1. 基础 ERC20 检查
                # --------------------------
                raw_name = token_contract.functions.name().call()
                decimals = token_contract.functions.decimals().call()

                # 必须是合法小数位
                if not isinstance(decimals, int) or not (0 <= decimals <= 18):
                    continue

                # 【修复空名称问题】严格过滤：名称为空/空白 → 不识别为代币
                token_name = str(raw_name).strip()
                if not token_name:
                    logger.debug(f"[{norm_addr}] 代币名称为空，跳过")
                    continue

                # --------------------------
                # 2. 【核心】通用排除所有 AMM 流动性池（V2/V3/所有DEX）
                # --------------------------
                pool_contract = self.web3.eth.contract(address=checksum_addr, abi=AMM_POOL_DETECT_ABI)
                is_amm_pool = False

                # 测试 factory()
                try:
                    pool_contract.functions.factory().call()
                    is_amm_pool = True
                except:
                    pass

                # 测试 token0()
                if not is_amm_pool:
                    try:
                        pool_contract.functions.token0().call()
                        is_amm_pool = True
                    except:
                        pass

                if is_amm_pool:
                    logger.debug(f"[{norm_addr}] 识别为 AMM 流动性池，排除")
                    continue

                # --------------------------
                # 3. 真正的 ERC20 代币
                # --------------------------
                erc20_token_map[norm_addr] = token_name
                logger.info(f"[{norm_addr}] 识别为 ERC20 代币: {token_name}")

                # 原有 DELEGATECALL 逻辑
                for step in steps:
                    if step["opcode"] == "DELEGATECALL" and step["address"] == norm_addr and len(step["stack"]) >= 7:
                        logic_addr = self._normalize_address(step["stack"][-2])
                        if logic_addr and logic_addr not in erc20_token_map:
                            erc20_token_map[logic_addr] = f"{token_name}_logic"
                            all_contracts.add(logic_addr)

            except Exception as e:
                logger.debug(f"[{contract_addr}] 不是标准ERC20: {str(e)}")
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

    @staticmethod
    def _validate_raw_trace(raw_trace: object, source: str) -> Dict:
        if not isinstance(raw_trace, dict):
            raise TraceFetchError(f"{source} trace 返回值不是对象")
        struct_logs = raw_trace.get("structLogs")
        if not isinstance(struct_logs, list):
            raise TraceFetchError(f"{source} trace 缺少有效 structLogs")
        if not struct_logs:
            raise NoOpcodeTraceError(f"{source} trace 不包含 opcode")
        return raw_trace

    def _run_geth_trace_request(
        self,
        tx_hash: str,
        trace_options: Dict,
        label: str,
    ) -> Dict:
        cmd = [
            "cast", "rpc",
            "--rpc-url", self.provider_url,
            "--rpc-timeout", str(GETH_RPC_TIMEOUT_SECONDS),
            "debug_traceTransaction",
            tx_hash,
            json.dumps(trace_options, separators=(",", ":")),
        ]
        for attempt in range(GETH_TRACE_RETRIES + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                response = json.loads(result.stdout)
                if not isinstance(response, dict):
                    raise TraceFetchError(f"Geth {label}返回值不是对象")
                return response
            except OSError as exc:
                raise TraceFetchError(f"Geth {label}请求失败: {exc}") from exc
            except (
                subprocess.SubprocessError,
                json.JSONDecodeError,
                TraceFetchError,
            ) as exc:
                stderr = getattr(exc, "stderr", "") or ""
                detail = str(stderr).strip() or str(exc)
                normalized_detail = detail.lower()
                historical_state_missing = any(
                    marker in normalized_detail
                    for marker in (
                        "historical state is not available",
                        "historical state unavailable",
                        "missing trie node",
                    )
                )
                if historical_state_missing or attempt >= GETH_TRACE_RETRIES:
                    raise TraceFetchError(
                        f"Geth {label}请求失败（尝试 {attempt + 1}/"
                        f"{GETH_TRACE_RETRIES + 1}）: {detail}"
                    ) from exc

                wait_seconds = min(
                    GETH_RETRY_DELAY_SECONDS * (attempt + 1),
                    3.0,
                )
                logger.warning(
                    "Geth %s临时失败，%s 秒后重试 (%d/%d): tx=%s error=%s",
                    label,
                    f"{wait_seconds:g}",
                    attempt + 1,
                    GETH_TRACE_RETRIES,
                    tx_hash,
                    detail,
                )
                time.sleep(wait_seconds)

        raise AssertionError("unreachable Geth request retry state")

    @staticmethod
    def _merge_geth_state_deltas(
        native_trace: Dict,
        delta_trace: Dict,
    ) -> Dict:
        native_logs = native_trace.get("structLogs")
        total_steps = delta_trace.get("totalSteps")
        raw_deltas = delta_trace.get("deltas")
        faults = delta_trace.get("faults", [])
        if not isinstance(native_logs, list):
            raise TraceFetchError("Geth 原生 trace 缺少 structLogs")
        if not native_logs:
            raise NoOpcodeTraceError("Geth trace 不包含 opcode")
        if total_steps != len(native_logs) or not isinstance(raw_deltas, list):
            raise TraceFetchError(
                "Geth 状态增量与原生 trace 步数不一致: "
                f"native={len(native_logs)}, delta={total_steps}"
            )
        if not isinstance(faults, list):
            raise TraceFetchError("Geth 状态增量 faults 元数据无效")
        if faults:
            logger.debug("Geth 状态增量观察到 EVM faults: %s", faults[:3])

        deltas_by_step: Dict[int, Tuple[object, object, object]] = {}
        for raw_delta in raw_deltas:
            if not isinstance(raw_delta, list) or len(raw_delta) != 4:
                raise TraceFetchError("Geth 状态增量记录格式无效")
            step_index, memory, slot, value = raw_delta
            if (
                not isinstance(step_index, int)
                or step_index < 0
                or step_index >= len(native_logs)
                or step_index in deltas_by_step
            ):
                raise TraceFetchError("Geth 状态增量 step 索引无效")
            if memory is not None and not isinstance(memory, str):
                raise TraceFetchError("Geth memory 增量不是十六进制字符串")
            if slot is not None and not isinstance(slot, str):
                raise TraceFetchError("Geth storage 增量 slot 无效")
            if value is not None and not isinstance(value, str):
                raise TraceFetchError("Geth storage 增量 value 无效")
            deltas_by_step[step_index] = (memory, slot, value)

        current_memory: List[str] = []
        merged_logs: List[Dict] = []
        for index, native_step in enumerate(native_logs):
            if not isinstance(native_step, dict):
                raise TraceFetchError(f"Geth 原生 trace step {index} 不是对象")
            memory, slot, value = deltas_by_step.get(index, (None, None, None))
            if isinstance(memory, str):
                try:
                    current_memory = _memory_words(memory)
                except DrpcTraceError as exc:
                    raise TraceFetchError(
                        f"Geth memory 增量 step {index} 无效: {exc}"
                    ) from exc

            storage_delta: Dict[str, str] = {}
            if isinstance(slot, str):
                resolved_value = value
                if resolved_value is None and index + 1 < len(native_logs):
                    next_step = native_logs[index + 1]
                    next_stack = (
                        next_step.get("stack")
                        if isinstance(next_step, dict)
                        else None
                    )
                    if isinstance(next_stack, list) and next_stack:
                        resolved_value = str(next_stack[-1])
                if isinstance(resolved_value, str):
                    storage_delta[slot] = resolved_value

            merged_step = dict(native_step)
            merged_step["memory"] = current_memory
            merged_step["storage"] = storage_delta
            merged_logs.append(merged_step)

        merged_trace = dict(native_trace)
        merged_trace["structLogs"] = merged_logs
        return merged_trace

    def _fetch_geth_trace(self, tx_hash: str) -> Dict:
        native_trace = self._run_geth_trace_request(
            tx_hash,
            {
                "enableMemory": False,
                "disableStack": False,
                "disableStorage": True,
                "enableReturnData": False,
            },
            "原生 trace",
        )
        self._validate_raw_trace(native_trace, "Geth")

        delta_trace = self._run_geth_trace_request(
            tx_hash,
            {
                "tracer": GETH_STATE_DELTA_TRACER,
                "timeout": f"{GETH_RPC_TIMEOUT_SECONDS}s",
            },
            "状态增量 trace",
        )
        merged_trace = self._merge_geth_state_deltas(native_trace, delta_trace)
        return self._validate_raw_trace(merged_trace, "Geth")

    def _fetch_raw_trace(
        self,
        tx_hash: str,
        block_number: int,
        gas_used: Optional[int] = None,
    ) -> Dict:
        prefer_drpc_chunks = (
            gas_used is not None and gas_used >= DRPC_CHUNK_GAS_THRESHOLD
        )
        if block_number < GETH_TRACE_START_BLOCK:
            logger.info(
                "交易 %s 位于区块 %d（早于 %d），直接使用 dRPC trace",
                tx_hash,
                block_number,
                GETH_TRACE_START_BLOCK,
            )
            try:
                drpc_trace = (
                    fetch_drpc_trace(tx_hash, prefer_chunks=True)
                    if prefer_drpc_chunks
                    else fetch_drpc_trace(tx_hash)
                )
                return self._validate_raw_trace(
                    drpc_trace,
                    "dRPC",
                )
            except NoOpcodeTraceError:
                raise
            except (DrpcTraceError, TraceFetchError) as exc:
                raise TraceFetchError(f"dRPC trace 请求失败: {exc}") from exc

        try:
            raw_trace = self._fetch_geth_trace(tx_hash)
            logger.info("成功从 Geth 获取 trace: %s", tx_hash)
            return raw_trace
        except NoOpcodeTraceError:
            raise
        except TraceFetchError as geth_error:
            logger.warning(
                "Geth trace 获取失败，回退 dRPC: tx=%s block=%d error=%s",
                tx_hash,
                block_number,
                geth_error,
            )
            try:
                drpc_trace = (
                    fetch_drpc_trace(tx_hash, prefer_chunks=True)
                    if prefer_drpc_chunks
                    else fetch_drpc_trace(tx_hash)
                )
                return self._validate_raw_trace(
                    drpc_trace,
                    "dRPC",
                )
            except (DrpcTraceError, TraceFetchError) as drpc_error:
                raise TraceFetchError(
                    f"Geth 和 dRPC trace 均失败；Geth: {geth_error}；"
                    f"dRPC: {drpc_error}"
                ) from drpc_error

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

            transaction = self.web3.eth.get_transaction(tx_hash)
            block_number = transaction.get("blockNumber")
            if block_number is None:
                raise TraceFetchError("交易尚未打包，无法获取 trace")
            gas_used: Optional[int] = None
            try:
                receipt = self.web3.eth.get_transaction_receipt(tx_hash)
                gas_used = int(receipt.get("gasUsed", 0))
            except Exception as exc:
                logger.warning("无法获取 gasUsed，将由 dRPC 自动选择请求方式: %s", exc)
            raw_trace = self._fetch_raw_trace(
                tx_hash,
                int(block_number),
                gas_used,
            )

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
                raw_storage = step.get("storage", {})
                depth = step.get("depth",[])
                # 单独处理CALL合约时的gascost计算
                # 执行CALL时会向合约预支付一笔gas，在trace中记录为CALL的gasCost
                # CALL本身的gascost是预支付的gasCost减去CALL下一步剩下的gasleft。
                entered_child_call = (
                    opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}
                    and i + 1 < len(struct_logs)
                    and int(struct_logs[i + 1].get("depth", depth)) > int(depth)
                )
                if entered_child_call:
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
                    "memory": raw_memory,
                    # Per-step SLOAD/SSTORE delta from the incremental tracer.
                    "storage": raw_storage if isinstance(raw_storage, dict) else {},
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

    # 从 KECCAK 的 memory 输入中提取 slot 对应的地址
    def extract_slot_address_map(self, standardized_trace: Dict) -> Dict[str, str]:
        """
        按 trace 顺序处理 KECCAK：
        - stack[-1] 是 memory offset，stack[-2] 是 memory size
        - 64 字节的标准 mapping 输入由 address word 和 base slot word 组成
        - 下一 step 的 stack[-1] 是 KECCAK 结果，即 storage slot
        - base slot 若已存在于 slot_addr_map，当前结果是二级 mapping，不写入
        - 最后只返回实际被 SLOAD/SSTORE 使用过的 slot
        """
        steps = standardized_trace.get("steps", []) if isinstance(standardized_trace, dict) else standardized_trace["steps"]

        def normalize_word(value: str) -> Optional[str]:
            """标准化为 0x + 64 位小写 hex，供 slot 比较使用。"""
            if value is None:
                return None
            body = self._strip_0x(str(value).strip()).lower() or "0"
            if len(body) > 64 or any(ch not in "0123456789abcdef" for ch in body):
                return None
            return f"0x{body.zfill(64)}"

        def word_to_int(value: str) -> Optional[int]:
            word = normalize_word(value)
            return int(word, 16) if word is not None else None

        def flatten_memory(memory: List[str]) -> Optional[str]:
            """将 memory 的 32 字节分段拼成连续、不含 0x 的 hex。"""
            words = []
            for raw_word in memory:
                word = normalize_word(raw_word)
                if word is None:
                    return None
                words.append(word[2:])
            return "".join(words)

        # canonical slot -> SLOAD/SSTORE 栈中实际出现的字符串格式
        used_slots: Dict[str, Set[str]] = {}
        # canonical slot -> address；同时承担此前 slot 历史的作用
        slot_addr_map: Dict[str, str] = {}

        for index, step in enumerate(steps):
            opcode = step.get("opcode", "").upper()
            stack = step.get("stack", []) or []

            if opcode in {"SLOAD", "SSTORE"} and stack:
                canonical_slot = normalize_word(stack[-1])
                if canonical_slot is not None:
                    used_slots.setdefault(canonical_slot, set()).add(str(stack[-1]).lower())

            if opcode not in {"SHA3", "KECCAK256", "KECCAK"}:
                continue
            if len(stack) < 2 or index + 1 >= len(steps):
                continue

            memory_offset = word_to_int(stack[-1])
            memory_size = word_to_int(stack[-2])
            if memory_offset is None or memory_size != 64:
                continue

            memory = step.get("memory", []) or []
            if not isinstance(memory, list):
                continue
            memory_hex = flatten_memory(memory)
            if memory_hex is None:
                continue

            start = memory_offset * 2
            end = start + memory_size * 2
            if end > len(memory_hex):
                continue
            keccak_input = memory_hex[start:end]
            address_word = keccak_input[:64]
            base_slot = f"0x{keccak_input[64:128]}"

            # mapping key 必须是左侧补 12 字节 0 的标准 address word。
            if address_word[:24] != "0" * 24:
                continue
            significant_address_length = len(address_word.lstrip("0"))
            if not 20 <= significant_address_length <= 40:
                continue

            next_stack = steps[index + 1].get("stack", []) or []
            if not next_stack:
                continue
            slot = normalize_word(next_stack[-1])
            if slot is None:
                continue

            # 第二部分引用已识别 slot，说明是 allowance 等二级 mapping。
            if base_slot in slot_addr_map:
                logger.debug(f"[slot_map] skip nested mapping slot: {slot}")
                continue

            address = self._normalize_address(f"0x{address_word[-40:]}")
            if address:
                slot_addr_map[slot] = address

        # 对外保留 trace 栈中的 slot 格式，兼容下游的直接字符串查表。
        return {
            raw_slot: address
            for slot, address in slot_addr_map.items()
            for raw_slot in used_slots.get(slot, set())
        }

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
        tx_sender_address: str = "",    # 交易发起者地址
        initial_address: str = "", # 根合约地址
        miner_address: str = ""  # 矿工地址
    ) -> Dict[str, str]:
        """
        构建全地址-名称映射表：
        - 交易发起者：优先命名为 User_From
        - 根合约： 优先命名为 contract_to
        - ERC20合约：使用token名称
        - 非ERC20合约：优先尝试获取 name()，获取不到再用 contract_a、contract_b...
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

        # 非 ERC20 合约：先尝试获取 name()
        non_erc20_contracts = [addr for addr in contracts_addresses if addr not in full_name_map]

        # 用于获取合约名称的极简 ABI
        ONLY_NAME_ABI = [
            {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"payable":False,"stateMutability":"view","type":"function"}
        ]

        for idx, addr in enumerate(non_erc20_contracts):
            try:
                # 根合约固定命名
                if addr == initial_address:
                    full_name_map[addr] = "contract_to"
                    continue

                # 尝试获取合约名称（Uniswap / BUSD / 各种协议都能识别）
                contract = self.web3.eth.contract(
                    address=Web3.to_checksum_address(addr),
                    abi=ONLY_NAME_ABI
                )
                contract_name = contract.functions.name().call()
                contract_name = str(contract_name).strip()

                # 能拿到合法名字 → 直接用
                if contract_name:
                    full_name_map[addr] = contract_name
                else:
                    # 拿不到 → 用 contract_a, contract_b...
                    contract_suffix = chr(ord('a') + idx)
                    full_name_map[addr] = f"contract_{contract_suffix}"

            except:
                # 调用 name() 失败 → 用默认命名
                contract_suffix = chr(ord('a') + idx)
                full_name_map[addr] = f"contract_{contract_suffix}"

        # 3. 处理用户地址
        sorted_users = sorted(list(users_addresses))
        
        # 3.1 先处理交易发起者
        if tx_sender_address and tx_sender_address in sorted_users:
            full_name_map[tx_sender_address] = "User_From"
            sorted_users.remove(tx_sender_address)

        # 3.2 处理矿工地址
        if miner_address and miner_address in sorted_users:
            full_name_map[miner_address] = "Miner"
            sorted_users.remove(miner_address)

        # 3.3 剩余用户
        for idx, addr in enumerate(sorted_users):
            user_suffix = chr(ord('A') + idx)
            full_name_map[addr] = f"User_{user_suffix}"
        
        return full_name_map
