from web3 import Web3

# 1. 连接节点（Geth/Infura/Alchemy 均可）
w3 = Web3(Web3.HTTPProvider('http://10.219.60.235:8545'))  # 本地 Geth 节点
# 2. 传入交易哈希，直接获取 from 地址
def get_tx_from_by_hash(tx_hash: str) -> str:
    """
    通过交易哈希获取发起者 from 地址
    :param tx_hash: 交易哈希（0x 开头）
    :return: 发起者地址（checksum 格式）
    """
    try:
        # 获取交易元数据（包含 from/to/value 等核心字段）
        tx = w3.eth.get_transaction(tx_hash)
        return tx["from"]  # 直接返回发起者地址
    except Exception as e:
        print(f"查询失败：{e}")
        return ""

# 使用示例
tx_hash = "0xd76d6cf2885323fbe0b9d1795763f8f9d30be648dcf0df4a524f7c3fe5c37177"
tx_from = get_tx_from_by_hash(tx_hash)
print(f"交易发起者：{tx_from}")