from web3 import Web3
from typing import Dict, Optional, List, Tuple
from functools import lru_cache
import json

class UniversalContractNameIdentifier:
    """最终修复版：解决lru_cache列表参数问题，精准识别Uniswap V3 Pool"""
    def __init__(self, rpc_url: str):
        self.web3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.web3.is_connected():
            raise ConnectionError(f"RPC节点连接失败: {rpc_url}")

        # ========== 1. 合约类型配置（优先直接调用验证） ==========
        self.CONTRACT_CONFIGS = {
            # Uniswap V3 Pool（优先级别最高，直接调用验证）
            "uniswap_v3_pool": {
                "verify_methods": [
                    ("token0", "0x0dfe1681", None),    # token0()
                    ("token1", "0x23b872dd", None),    # token1()
                    ("fee", "0x3fee5064", None)        # fee()
                ],
                "name_template": "Uniswap V3: {token0}/{token1} {fee}%",
                "required_verify": 3  # 必须3个方法都调用成功
            },
            # Uniswap V2 Pair
            "uniswap_v2_pair": {
                "verify_methods": [
                    ("token0", "0x0dfe1681", None),
                    ("token1", "0x23b872dd", None),
                    ("getReserves", "0x0902f1ac", None)
                ],
                "name_template": "Uniswap V2: {token0}/{token1}",
                "required_verify": 3
            },
            # ERC20代币
            "erc20": {
                "verify_methods": [
                    ("name", "0x06fdde03", None),
                    ("symbol", "0x95d89b41", None),
                    ("decimals", "0x313ce567", None)
                ],
                "name_template": "ERC20 Token: {name} ({symbol})",
                "required_verify": 2
            },
            # ERC721 NFT
            "erc721": {
                "verify_methods": [
                    ("name", "0x06fdde03", None),
                    ("symbol", "0x95d89b41", None),
                    ("balanceOf", "0x70a08231", ("0x0000000000000000000000000000000000000000",))
                ],
                "name_template": "ERC721 NFT: {name}",
                "required_verify": 2
            },
            # 代理合约
            "proxy_contract": {
                "verify_methods": [
                    ("implementation", "0xf4361c94", None),
                    ("admin", "0xa619486e", None)
                ],
                "name_template": "Proxy Contract (Implementation: {implementation})",
                "required_verify": 1
            },
            # 稳定币（固定识别）
            "usdt": {
                "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "name": "Tether USD (USDT)"
            },
            "usdc": {
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "name": "USD Coin (USDC)"
            }
        }

    def _normalize_address(self, addr: str) -> Optional[str]:
        """标准化地址"""
        try:
            return Web3.to_checksum_address(addr)
        except:
            return None

    @lru_cache(maxsize=1024)
    # 关键修复：将params改为元组（tuple），元组是可哈希的，列表不可哈希
    def _call_contract_method(self, addr: str, selector: str, params: Tuple = None) -> Optional[any]:
        """调用合约方法并返回解析后的值（修复lru_cache参数问题）"""
        try:
            # 构造调用数据
            data = selector
            if params:
                for param in params:
                    if isinstance(param, str) and param.startswith("0x"):
                        data += Web3.to_bytes(hexstr=param).hex().zfill(64)
                    elif isinstance(param, int):
                        data += hex(param)[2:].zfill(64)
            
            # 执行调用
            raw_result = self.web3.eth.call({"to": addr, "data": data})
            
            # 根据方法类型解析结果
            if selector in ["0x0dfe1681", "0x23b872dd"]:  # token0/token1
                return Web3.to_checksum_address(raw_result.hex()[-40:])
            elif selector == "0x3fee5064":  # fee
                return int.from_bytes(raw_result, byteorder='big') / 100
            elif selector in ["0x06fdde03", "0x95d89b41"]:  # name/symbol
                return self.web3.codec.decode(["string"], raw_result)[0].strip()
            elif selector == "0x313ce567":  # decimals
                return int.from_bytes(raw_result, byteorder='big')
            elif selector == "0xf4361c94":  # implementation
                return Web3.to_checksum_address(raw_result.hex()[-40:])
            else:  # 其他方法返回原始值
                return raw_result.hex() if raw_result else None
        except Exception as e:
            # 打印具体错误（便于调试）
            # print(f"调用方法失败 {selector}: {str(e)[:50]}")
            return None

    def _get_token_symbol(self, token_addr: str) -> str:
        """获取代币符号（兜底处理）"""
        if not token_addr:
            return "Unknown"
        symbol = self._call_contract_method(token_addr, "0x95d89b41", None)
        if symbol and isinstance(symbol, str):
            return symbol[:10]
        return token_addr[-6:]  # 地址后6位兜底

    def identify_contract_name(self, contract_address: str) -> Dict[str, any]:
        """
        核心方法：优先调用验证，精准识别Uniswap V3 Pool
        """
        result = {
            "contract_address": None,
            "etherscan_name": "Unknown Contract",
            "contract_type": "Unknown",
            "confidence": 0.0,
            "details": {},
            "error": None
        }

        # 1. 地址校验
        checksum_addr = self._normalize_address(contract_address)
        if not checksum_addr:
            result["error"] = "无效的合约地址"
            return result
        result["contract_address"] = checksum_addr

        # 2. 检查是否为合约
        bytecode = self.web3.eth.get_code(checksum_addr)
        if not bytecode:
            result["error"] = "非合约地址（无字节码）"
            return result

        # 3. 优先识别稳定币（固定地址匹配）
        for stable_type, config in self.CONTRACT_CONFIGS.items():
            if "address" in config and config["address"] == checksum_addr:
                result["etherscan_name"] = config["name"]
                result["contract_type"] = stable_type.upper()
                result["confidence"] = 1.0
                return result

        # 4. 优先识别Uniswap V3 Pool（核心修复）
        v3_config = self.CONTRACT_CONFIGS["uniswap_v3_pool"]
        v3_results = {}
        verify_count = 0
        
        # 调用所有V3核心方法
        for method_name, selector, params in v3_config["verify_methods"]:
            res = self._call_contract_method(checksum_addr, selector, params)
            if res is not None:
                v3_results[method_name] = res
                verify_count += 1
        
        # 验证通过：是Uniswap V3 Pool
        if verify_count >= v3_config["required_verify"]:
            # 获取代币符号
            token0_symbol = self._get_token_symbol(v3_results["token0"])
            token1_symbol = self._get_token_symbol(v3_results["token1"])
            
            # 生成Etherscan风格名称
            result["etherscan_name"] = v3_config["name_template"].format(
                token0=token0_symbol,
                token1=token1_symbol,
                fee=v3_results["fee"]
            )
            result["contract_type"] = "Uniswap V3 Pool"
            result["confidence"] = 1.0
            result["details"] = {
                "token0": v3_results["token0"],
                "token1": v3_results["token1"],
                "fee_percent": v3_results["fee"],
                "token0_symbol": token0_symbol,
                "token1_symbol": token1_symbol
            }
            return result

        # 5. 识别Uniswap V2 Pair
        v2_config = self.CONTRACT_CONFIGS["uniswap_v2_pair"]
        v2_results = {}
        verify_count = 0
        for method_name, selector, params in v2_config["verify_methods"]:
            res = self._call_contract_method(checksum_addr, selector, params)
            if res is not None:
                v2_results[method_name] = res
                verify_count += 1
        
        if verify_count >= v2_config["required_verify"]:
            token0_symbol = self._get_token_symbol(v2_results["token0"])
            token1_symbol = self._get_token_symbol(v2_results["token1"])
            result["etherscan_name"] = v2_config["name_template"].format(
                token0=token0_symbol,
                token1=token1_symbol
            )
            result["contract_type"] = "Uniswap V2 Pair"
            result["confidence"] = 0.95
            result["details"] = v2_results
            return result

        # 6. 识别ERC20代币
        erc20_config = self.CONTRACT_CONFIGS["erc20"]
        erc20_results = {}
        verify_count = 0
        for method_name, selector, params in erc20_config["verify_methods"]:
            res = self._call_contract_method(checksum_addr, selector, params)
            if res is not None:
                erc20_results[method_name] = res
                verify_count += 1
        
        if verify_count >= erc20_config["required_verify"]:
            result["etherscan_name"] = erc20_config["name_template"].format(
                name=erc20_results.get("name", "Unknown"),
                symbol=erc20_results.get("symbol", "Unknown")
            )
            result["contract_type"] = "ERC20 Token"
            result["confidence"] = 0.9
            result["details"] = erc20_results
            return result

        # 7. 识别ERC721 NFT
        erc721_config = self.CONTRACT_CONFIGS["erc721"]
        erc721_results = {}
        verify_count = 0
        for method_name, selector, params in erc721_config["verify_methods"]:
            res = self._call_contract_method(checksum_addr, selector, params)
            if res is not None:
                erc721_results[method_name] = res
                verify_count += 1
        
        if verify_count >= erc721_config["required_verify"]:
            result["etherscan_name"] = erc721_config["name_template"].format(
                name=erc721_results.get("name", "Unknown NFT")
            )
            result["contract_type"] = "ERC721 NFT"
            result["confidence"] = 0.9
            result["details"] = erc721_results
            return result

        # 8. 识别代理合约
        proxy_config = self.CONTRACT_CONFIGS["proxy_contract"]
        proxy_results = {}
        verify_count = 0
        for method_name, selector, params in proxy_config["verify_methods"]:
            res = self._call_contract_method(checksum_addr, selector, params)
            if res is not None:
                proxy_results[method_name] = res
                verify_count += 1
        
        if verify_count >= proxy_config["required_verify"]:
            result["etherscan_name"] = proxy_config["name_template"].format(
                implementation=proxy_results.get("implementation", "Unknown")
            )
            result["contract_type"] = "Proxy Contract"
            result["confidence"] = 0.9
            result["details"] = proxy_results
            return result

        return result

# ==================== 测试修复后的代码 ====================
if __name__ == "__main__":
    # 你的RPC节点地址（替换为实际可访问的地址）
    RPC_URL = "http://10.219.60.235:8545"
    # 你的Uniswap V3 Pool地址（重点测试）
    TARGET_V3_POOL = "0xc7bBeC68d12a0d1830360F8Ec58fA599bA1b0e9b"

    try:
        # 初始化识别器
        identifier = UniversalContractNameIdentifier(RPC_URL)
        
        # 识别Uniswap V3 Pool
        print("===== 识别Uniswap V3 Pool =====")
        v3_info = identifier.identify_contract_name(TARGET_V3_POOL)
        print(json.dumps(v3_info, indent=2, ensure_ascii=False))

        # 测试USDT（可选）
        # print("\n===== 识别USDT稳定币 =====")
        # usdt_info = identifier.identify_contract_name("0xdAC17F958D2ee523a2206206994597C13D831ec7")
        # print(json.dumps(usdt_info, indent=2, ensure_ascii=False))

    except Exception as e:
        print(f"识别失败: {str(e)}")
        # 打印完整错误栈（便于调试）
        import traceback
        traceback.print_exc()