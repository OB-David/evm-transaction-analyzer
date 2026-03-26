import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


FOURBYTE_SIGNATURE_API = "https://www.4byte.directory/api/v1/signatures/"
FOURBYTE_MAX_PAGES = 50
FOURBYTE_TIMEOUT_SECONDS = 8
SELECTOR_PATTERN = re.compile(r"^0x[0-9a-f]{8}$")
FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PRIORITY_FUNCTION_NAMES = [
    "transfer",
    "transferFrom",
    "approve",
    "safeTransfer",
    "safeTransferFrom",
    "swapExactTokensForTokens",
    "swapTokensForExactTokens",
    "swapExactETHForTokens",
    "swapTokensForExactETH",
    "swapExactTokensForETH",
    "swap",
    "exactInputSingle",
    "exactOutputSingle",
    "multicall",
    "flashLoan",
    "flashSwap",
    "executeOperation",
    "flashLoanSimple",
    "getReserves",
    "getAmountOut",
    "getAmountIn",
    "balanceOf",
    "decimals",
    "factory",
    "pairFor",
    "getPool",
    "deposit",
    "withdraw",
    "safeApprove",
    "pull",
    "call",
]
PRIORITY_SIGNATURES = [f"{name}()" for name in PRIORITY_FUNCTION_NAMES]
PRIORITY_SIGNATURE_RANK = {name: idx for idx, name in enumerate(PRIORITY_SIGNATURES)}


def _extract_selector(calldata_value: Any) -> str | None:
    if not isinstance(calldata_value, str):
        return None
    text = calldata_value.strip().lower()
    if len(text) < 10:
        return None
    selector = text[:10]
    if SELECTOR_PATTERN.match(selector):
        return selector
    return None


def _extract_function_name(text_signature: Any) -> str | None:
    if not isinstance(text_signature, str):
        return None
    cleaned = text_signature.strip()
    if not cleaned:
        return None
    left_paren_idx = cleaned.find("(")
    if left_paren_idx <= 0:
        return None
    fn_name = cleaned[:left_paren_idx].strip()
    if not FUNCTION_NAME_PATTERN.match(fn_name):
        return None
    return f"{fn_name}()"


def _fetch_4byte_results(hex_selector: str) -> list[dict]:
    all_results: list[dict] = []
    next_url = f"{FOURBYTE_SIGNATURE_API}?hex_signature={quote(hex_selector)}"
    seen_urls: set[str] = set()
    pages = 0

    while next_url and pages < FOURBYTE_MAX_PAGES:
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)

        try:
            req = Request(
                next_url,
                headers={
                    "User-Agent": "evm-transaction-analyzer/1.0",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=FOURBYTE_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"WARNING: 4byte lookup failed for selector {hex_selector}: {exc}")
            return []

        results = payload.get("results", [])
        if isinstance(results, list):
            all_results.extend(item for item in results if isinstance(item, dict))

        next_link = payload.get("next")
        next_url = urljoin(next_url, next_link) if isinstance(next_link, str) and next_link else ""
        pages += 1

    if next_url:
        print(f"WARNING: 4byte pagination truncated at {FOURBYTE_MAX_PAGES} pages for selector {hex_selector}")
    return all_results


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _resolve_probable_signatures(
    selector: str,
    selector_signature_cache: dict[str, list[str]],
) -> list[str]:
    cached = selector_signature_cache.get(selector)
    if cached is not None:
        return cached

    results = _fetch_4byte_results(selector)
    signature_to_min_id: dict[str, int] = {}
    for item in results:
        fn = _extract_function_name(item.get("text_signature"))
        if not fn:
            continue
        item_id = _safe_int(item.get("id"), fallback=2**31 - 1)
        prev = signature_to_min_id.get(fn)
        if prev is None or item_id < prev:
            signature_to_min_id[fn] = item_id

    sorted_signatures = sorted(
        signature_to_min_id.items(),
        key=lambda item: (
            0 if item[0] in PRIORITY_SIGNATURE_RANK else 1,
            PRIORITY_SIGNATURE_RANK.get(item[0], 10**9),
            item[1],
            item[0],
        ),
    )
    ordered = [item[0] for item in sorted_signatures]
    selector_signature_cache[selector] = ordered
    return ordered


def _format_compact_signatures(signatures: list[str], max_items: int = 2) -> str:
    if not signatures:
        return ""
    # 若命中优先函数，按需求仅展示一个并直接追加 /...
    first_signature = signatures[0]
    if first_signature in PRIORITY_SIGNATURE_RANK:
        return f"{first_signature}/..."
    compact = "/".join(signatures[:max_items])
    if len(signatures) > max_items:
        compact += "/..."
    return compact

def build_refined_hierarchical_trace(steps):
    """
    calldata断点切片:
    Primary Breakpoints (首断点): CALLDATALOAD 传入的 offset。
    Hidden Breakpoints (隐藏断点) 分两类：
       - 起点=0：读取8个16进制字符（4字节）
       - 起点≠0：读取64个16进制字符（32字节）
    自动截断: 段终点 = min(下一个首断点, 隐藏断点, 数据总长)。
    """
    if not steps:
        return {}

    # 定义常量（移到函数内部，避免外部依赖）
    CALL_OPS = {"CALL", "DELEGATECALL", "STATICCALL", "CALLCODE", "CREATE", "CREATE2"}
    RETURN_OPS = {"RETURN", "STOP", "REVERT", "INVALID", "SELFDESTRUCT"}
    
    global_gas_counter = 0

    def process_level(start_index):
        nonlocal global_gas_counter
        start_step = steps[start_index]
        
        last_step_op = "OUTSIDECALL"
        raw_calldata = ""
        primary_breakpoints = set()

        # --- 1. 提取原始数据  ---
        if start_index != 0:
            prev_step = steps[start_index - 1]
            last_step_op = prev_step.get("opcode", "OUTSIDECALL")  # 适配opcode字段
            stack = prev_step.get("stack", [])
            memory = prev_step.get("memory", [])
            full_mem = "".join([m for m in memory if m])
            
            try:
                if last_step_op in ["CALL", "CALLCODE"]:
                    offset = int(stack[-4], 16) if len(stack) >=4 else 0
                    size = int(stack[-5], 16) if len(stack) >=5 else 0
                elif last_step_op in ["DELEGATECALL", "STATICCALL"]:
                    offset = int(stack[-3], 16) if len(stack) >=3 else 0
                    size = int(stack[-4], 16) if len(stack) >=4 else 0
                elif last_step_op in ["CREATE", "CREATE2"]:
                    offset = int(stack[-2], 16) if len(stack) >=2 else 0
                    size = int(stack[-3], 16) if len(stack) >=3 else 0
                else: 
                    offset, size = 0, 0
                
                if size > 0 and offset >=0:
                    # 计算内存截取范围（适配hex字符串）
                    start_pos = offset * 2
                    end_pos = (offset + size) * 2
                    if len(full_mem) >= end_pos: 
                        raw_calldata = full_mem[start_pos:end_pos]  
                    else:
                        raw_calldata = ''
                        print("no calldata error")
            except (IndexError, ValueError, TypeError): 
                # stack长度不足/转换失败时置空
                raw_calldata = ""

        # 直接使用原生depth字段
        current_level_depth = start_step.get("depth", 0)
        
        node = {
            "contract": start_step.get("address"),
            "depth": current_level_depth,
            "entry_op": last_step_op,
            "entry_step": start_index - 1,
            "calldata_raw": "0x" + raw_calldata if raw_calldata else "",
            "calldata_active_segments": [],
            "calls": [],
            "exit_op": "PENDING", 
            "exit_step": 1,
            "exit_gas": 0
        }

        # --- 2. 遍历执行流并收集首断点 ---
        i = start_index
        while i < len(steps):
            step = steps[i]
            # 直接使用原生depth判断层级
            step_depth = step.get("depth", 0)
            
            if step_depth > current_level_depth:
                # 子调用：递归处理
                child_node, next_i = process_level(i)
                node["calls"].append(child_node)
                i = next_i
                continue
            
            if step_depth < current_level_depth:
                # 层级回退：退出当前层级处理
                break

            op = step.get("opcode", "")
            stk = step.get("stack", [])
            
            # 记录退出操作
            if op in RETURN_OPS:
                node["exit_op"] = op
                node["exit_step"] = i

            # 收集CALLDATALOAD断点
            if op == "CALLDATALOAD" and len(stk) > 0:
                try:
                    off = int(stk[-1], 16)
                    num_bytes = len(raw_calldata) // 2
                    if off < num_bytes:
                        primary_breakpoints.add(off)
                except (ValueError, IndexError):
                    pass

            # 累计Gas消耗
            gas_cost = step.get("gascost", 0)
            global_gas_counter += int(gas_cost) if str(gas_cost).isdigit() else 0
            i += 1

        # --- 3. 按起点类型区分切片长度 ---
        if raw_calldata:
            # 强制将 0 加入首断点（确保选择器等起始位被识别）
            primary_breakpoints.add(0)
            sorted_p = sorted(list(primary_breakpoints))
            segments = []
            
            num_bytes = len(raw_calldata) // 2  # 总字节数
            for idx, p_start in enumerate(sorted_p):
                # 隐藏断点规则：
                # 1. 起点=0 → 读取4字节（8个16进制字符）
                # 2. 起点≠0 → 读取32字节（64个16进制字符）
                if p_start == 0:
                    hidden_end = p_start + 4  # 4字节（函数选择器）
                else:
                    hidden_end = p_start + 32  # 32字节（标准EVM数据）
                
                # 定义“逻辑终点”：
                # 1. 如果有下一个首断点，则不能超过它
                # 2. 不能超过当前隐藏断点（按规则计算）
                # 3. 不能超过数据总长
                if idx + 1 < len(sorted_p):
                    next_p = sorted_p[idx + 1]
                    actual_end = min(next_p, hidden_end, num_bytes)
                else:
                    actual_end = min(hidden_end, num_bytes)
                
                # 只有当区间有效时才截取（处理重叠或末尾情况）
                if p_start < actual_end:
                    # 计算16进制字符串的截取范围（1字节=2个16进制字符）
                    hex_start = p_start * 2
                    hex_end = actual_end * 2
                    seg_hex = raw_calldata[hex_start:hex_end]
                    segments.append({
                        "count": idx,
                        "val": f"0x{seg_hex}"
                    })
            
            node["calldata_active_segments"] = segments

        # 补全退出状态
        node["exit_gas"] = global_gas_counter
        if node["exit_op"] == "PENDING": 
            node["exit_op"] = "STOP"

        return node, i

    # 从根节点开始构建
    root_tree, _ = process_level(0)
    return root_tree

def _build_message_link(call_id, from_name, to_name, entry_op):
    tooltip = f"{entry_op}: {from_name} -> {to_name}"
    tooltip = tooltip.replace("{", "(").replace("}", ")").replace("[", "(").replace("]", ")")
    return f"[[#call-{call_id}{{{tooltip}}}]]"


def render_puml_to_svg(puml_path):
    """Render a PlantUML file to SVG if a PlantUML jar is available."""
    abs_puml_path = os.path.abspath(puml_path)
    svg_path = os.path.splitext(abs_puml_path)[0] + ".svg"

    plantuml_jar = os.environ.get("PLANTUML_JAR")
    if not plantuml_jar:
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plantuml_jar = os.path.join(backend_dir, "tools", "plantuml.jar")

    plantuml_jar = os.path.abspath(plantuml_jar)
    if not os.path.isfile(plantuml_jar):
        print(f"WARNING: PlantUML jar not found, sequence SVG skipped: {plantuml_jar}")
        return False

    try:
        proc = subprocess.run(
            ["java", "-jar", plantuml_jar, "-charset", "UTF-8", "-tsvg", abs_puml_path],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
    except Exception as exc:
        print(f"WARNING: PlantUML SVG generation failed for {abs_puml_path}: {exc}")
        return False

    if not os.path.isfile(svg_path):
        print(f"WARNING: PlantUML reported success but SVG not found: {svg_path}")
        return False

    print(f"✅ 时序图SVG已生成：{svg_path}")
    return True


def tree_to_puml(trace_tree, output_file, erc20_token_map, full_address_name_map, addr_color_map):
    
    # 新增：初始化calldata映射字典（
    call_data_mapping = {
        "total_calls": 0,  # 总调用数
        "calls": []        # 每笔调用的详细映射
    }
    selector_signature_cache: dict[str, list[str]] = {}
    
    # PUML基础模板（核心修改：新增box的padding/margin控制间距）
    puml_lines = [
        "@startuml",
        "skinparam backgroundcolor #FFFFFF",
        # 合约样式：矩形
        "skinparam participant {",
        "    BorderColor black",
        "    BackgroundColor lightblue",
        "    FontSize 10",
        "    Shape rectangle",
        "}",
        # 交互样式
        "skinparam call {",
        "    BorderColor black",
        "    BackgroundColor lightgreen",
        "    FontSize 9",
        "}",
        "skinparam return {",
        "    BorderColor black",
        "    BackgroundColor lightyellow",
        "    FontSize 9",
        "}",
        # 深度分组box样式
        "skinparam box {",
        "    BorderColor black",
        "    FontSize 8",
        "    BackgroundColor #FFFFFF",
        "    Padding 8",
        "    Margin 20",
        "    Separator 5",
        "}",
        "hide footbox"
    ]
    
    # 核心状态管理（无修改）
    contract_call_seq = {}        # 地址→调用次数
    contract_instances = []       # 所有合约实例
    interaction_lines = []        # 所有交互行
    instance_id = 0               # 实例计数器
    DEPTH_BG_COLORS = [
        "#E0F7FF40",
        "#B3E5FC40",
        "#81D4FA40",
        "#4FC3F740",
        "#29B6F640",
        "#0288D140",
        "#0277BD40",
        "#01579B40"
    ]

    # -------------------------- 工具函数 --------------------------
    def _sanitize_puml_text(text, fallback):
        cleaned = str(text).strip() if text is not None else ""
        if not cleaned:
            cleaned = fallback
        cleaned = cleaned.replace('"', "'").replace("\n", " ").replace("\r", " ")
        return cleaned

    def _get_contract_name(addr):
        """精准获取合约名称"""
        if not addr:
            return "UNKNOWN"
        addr_lower = addr.lower().strip()
        
        if addr_lower in full_address_name_map:
            mapped_name = _sanitize_puml_text(
                full_address_name_map[addr_lower],
                f"{addr[:12]}..." if len(addr) > 12 else addr
            )
            if mapped_name:
                return mapped_name
        elif addr_lower in erc20_token_map:
            token_info = erc20_token_map[addr_lower]
            if isinstance(token_info, dict):
                token_name = token_info.get("name")
            else:
                token_name = token_info
            token_name = _sanitize_puml_text(token_name, f"ERC20_{addr_lower[2:10]}")
            if token_name:
                return token_name
        else:
            return f"{addr[:12]}..." if len(addr) > 12 else addr
        return f"{addr[:12]}..." if len(addr) > 12 else addr

    def _is_token_contract(addr):
        """判断是否为Token合约"""
        return addr and addr.lower().strip() in erc20_token_map

    def _get_contract_color(addr):
        """获取统一颜色"""
        addr_lower = addr.lower().strip()
        return addr_color_map.get(addr_lower, "#B0BEC5E6")

    def _create_contract_instance(addr, depth):
        """创建合约实例"""
        nonlocal instance_id
        addr = addr.strip() if addr else "UNKNOWN"
        addr_lower = addr.lower()
        
        if addr_lower not in contract_call_seq:
            contract_call_seq[addr_lower] = 0
        call_num = contract_call_seq[addr_lower]
        
        # 生成唯一别名
        addr_short = addr.replace("0x", "").replace(":", "_").replace(".", "_")[:16]
        alias = f"inst_{addr_short}_{call_num}"
        
        # 记录实例信息
        fallback_name = addr[:12] + "..." if len(addr) > 12 else addr
        instance = {
            "id": instance_id,
            "alias": alias,
            "address": addr,
            "address_short": addr[:12] + "..." if len(addr) > 12 else addr,  # 新增：短地址，便于JSON显示
            "name": _sanitize_puml_text(_get_contract_name(addr), fallback_name),
            "call_num": call_num,
            "depth": depth,
            "is_token": _is_token_contract(addr),
            "color": _get_contract_color(addr)
        }
        contract_instances.append(instance)
        
        contract_call_seq[addr_lower] += 1
        instance_id += 1
        
        return instance

    # -------------------------- 递归遍历调用树--------------------------
    def _traverse_call_tree(node, parent_instance=None, indent_level=0):
        """递归遍历所有深度的调用节点"""
        current_addr = node.get("contract", "").strip()
        current_depth = node.get("depth", 0)
        entry_step = node.get("entry_step",0)
        exit_step = node.get("exit_step",0)
        entry_op = node.get("entry_op", "UNKNOWN").strip()
        exit_op = node.get("exit_op", "STOP").strip()
        segments = node.get("calldata_active_segments", [])

        # 创建当前合约实例
        current_instance = _create_contract_instance(current_addr, current_depth)
        indent = "    " * indent_level

        if parent_instance:
            # 1. 提取完整calldata数组
            full_calldata = []
            for idx, seg in enumerate(segments):
                if seg and "val" in seg:
                    full_calldata.append(seg["val"])
                else:
                    full_calldata.append("无Calldata")
            calldata0 = segments[0]["val"] if (segments and len(segments) > 0) else "无Calldata"
            probable_text_signatures: list[str] = []
            selector = _extract_selector(calldata0)
            if selector:
                probable_text_signatures = _resolve_probable_signatures(selector, selector_signature_cache)
            compact_signatures = _format_compact_signatures(probable_text_signatures, max_items=2)
            call_label = compact_signatures if compact_signatures else calldata0
            
            call_id = call_data_mapping["total_calls"] + 1
            message_link = _build_message_link(
                call_id,
                parent_instance["name"],
                current_instance["name"],
                entry_op,
            )

            # 2. 原有PUML调用线生成逻辑
            op_type = entry_op.upper()
            if op_type in ["DELEGATECALL", "CALLCODE"]:
                # 虚线
                call_line = (
                    f"{indent}{parent_instance['alias']} -[dashed]-> {current_instance['alias']} "
                    f"{message_link} : {entry_op}\\l{call_label}"
                )
            else:
                # 实线
                call_line = (
                    f"{indent}{parent_instance['alias']} -> {current_instance['alias']} "
                    f"{message_link} : {entry_op}\\l{call_label}"
                )
            interaction_lines.append(call_line)

            # 3. 记录到JSON映射（核心）
            call_data_mapping["total_calls"] = call_id
            call_entry = {
                "call_id": call_id,
                "entry_step": entry_step,
                "exit_step": exit_step,
                "entry_op": entry_op,
                "exit_op": exit_op,
                "from_name": parent_instance["name"],
                "to_name": current_instance["name"],
                "calldata": full_calldata,
            }
            if probable_text_signatures:
                call_entry["probable_text_signatures"] = probable_text_signatures
            call_data_mapping["calls"].append(call_entry)

        # 递归处理子节点
        if "calls" in node and isinstance(node["calls"], list):
            for child_node in node["calls"]:
                _traverse_call_tree(child_node, current_instance, indent_level + 1)

        # 生成返回边
        if parent_instance:
            return_line = f"{indent}{current_instance['alias']} -> {parent_instance['alias']}: {exit_op}"
            interaction_lines.append(return_line)

    # -------------------------- 构建PUML --------------------------
    # 递归遍历调用树
    _traverse_call_tree(trace_tree)

    # 按深度分组生成合约实例
    depth_groups = {}
    for instance in contract_instances:
        depth = instance["depth"]
        if depth not in depth_groups:
            depth_groups[depth] = []
        depth_groups[depth].append(instance)

    # 生成各深度的box
    for depth in sorted(depth_groups.keys()):
        instances = depth_groups[depth]
        if not instances:
            continue
        
        # 深度背景色
        bg_color = DEPTH_BG_COLORS[depth] if depth < len(DEPTH_BG_COLORS) else DEPTH_BG_COLORS[-1]
        puml_lines.append(f'\nbox "Depth {depth}" {bg_color}')
        
        # 生成合约实例
        for instance in instances:
            display_name = _sanitize_puml_text(instance["name"], instance["address_short"])
            if instance["is_token"]:
                puml_lines.append(
                    f'    participant "{display_name}" as {instance["alias"]} {instance["color"]}'
                )
            else:
                puml_lines.append(
                    f'    participant "{display_name}" as {instance["alias"]} {instance["color"]}'
                )
        
        puml_lines.append("end box")
        puml_lines.append("")  # 兜底：不同深度box后加空行，确保间距
    
    # 添加交互行
    puml_lines.append("\n' === 全深度调用交互序列 === '")
    puml_lines.extend(interaction_lines)

    # 结束PUML
    puml_lines.append("\n@enduml")

    # -------------------------- 写入文件 --------------------------
    # 1. 原有PUML文件写入
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(puml_lines))
    
    # 2. 新增：写入JSON映射文件（和PUML同目录，同名不同后缀）
    json_file = output_file.replace(".puml", "_calldata_mapping.json")
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(call_data_mapping, f, ensure_ascii=False, indent=4)
    
    # 输出统计信息（新增JSON文件提示）
    print(f"✅ 时序图PUML已生成：{output_file}")
    print(f"✅ Calldata映射JSON已生成：{json_file}")
    
    return True
