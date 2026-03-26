import json
import ast

def filter_to_file(input_path, output_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 模式
    p1 = ["MUL", "LT", "JUMPI", "GT", "JUMPI", "DIV"]
    p2 = ["ISZERO", "MUL", "DIV","EQ"]

    res = {"pattern_1": [], "pattern_2": []}


    def strict_pattern_order_no_interrupt(ops, pattern):
        ptr = 0
        pat_len = len(pattern)
        forbidden = set(pattern)  # 模式里所有关键词，中间不能出现

        for op in ops:
            if ptr >= pat_len:
                break

            if op == pattern[ptr]:
                ptr += 1
            else:
                if op in forbidden:
                    return False
        return ptr == pat_len

    for k, v in data.items():
        ops = []
        for s in v.get("instructions", []):
            try:
                obj = ast.literal_eval(s)
                op = obj[1] if isinstance(obj, tuple) else obj.get('opcode')
                if op:
                    ops.append(op)
            except:
                continue

        minimal_info = {
            "id": v.get("id", k),
            "address": v.get("address", "N/A")
        }

        if strict_pattern_order_no_interrupt(ops, p1):
            res["pattern_1"].append(minimal_info)
        if strict_pattern_order_no_interrupt(ops, p2):
            res["pattern_2"].append(minimal_info)

    with open(output_path, 'w', encoding='utf-8',) as f:
        json.dump(res, f, indent=2, ensure_ascii=False)