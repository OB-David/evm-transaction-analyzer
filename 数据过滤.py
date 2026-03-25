import json
import ast

def filter_to_file(input_path, output_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 你的目标模式
    p1 = ["MUL", "LT", "JUMPI", "GT", "JUMPI", "DIV", "SWAP1"]
    p2 = ["MUL", "MLOAD", "JUMPI", "DIV", "EQ",]
    
    # 结果结构修改为列表，方便直接查看 id 和 address
    res = {"pattern_1": [], "pattern_2": []}

    for k, v in data.items():
        # 1. 提取 Opcode
        ops = []
        for s in v.get("instructions", []):
            try:
                # 处理元组字符串或字典对象
                obj = ast.literal_eval(s)
                op = obj[1] if isinstance(obj, tuple) else obj.get('opcode')
                if op: ops.append(op)
            except:
                continue
        
        # 准备精简后的数据结构
        # 假设数据中的 id 存储在 v['id']，address 存储在 v['address']
        # 如果 id 就是字典的 key (k)，则直接使用 k
        minimal_info = {
            "id": v.get("id", k), 
            "address": v.get("address", "N/A")
        }

        # 2. 顺序匹配逻辑
        it1 = iter(ops)
        if all(x in it1 for x in p1):
            res["pattern_1"].append(minimal_info)
        
        it2 = iter(ops) 
        if all(x in it2 for x in p2):
            res["pattern_2"].append(minimal_info)

    # 3. 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(res, f, indent=2, ensure_ascii=False)
    
    print(f"过滤完成！结果已保存至: {output_path}")

if __name__ == "__main__":
    # 请确保输入文件路径正确
    filter_to_file('folded_blocks_information.json', 'filtered_results.json')