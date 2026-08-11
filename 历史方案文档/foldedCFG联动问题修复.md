# Folded CFG 联动问题修复方案

## 1. 目标

修复 Token Flow、调用树与 Folded CFG 的联动，使三者统一使用交易执行轨迹中的 `step` 作为动态坐标，不再使用 Folded CFG 的单一 PC 范围进行关联。

Folded CFG 的一个节点可能在交易中被反复执行，因此节点不能只有一个 `start_step/end_step`，而应保存该节点在本次交易中的全部执行区间：

```json
"step_ranges": [
  {"start_step": 120, "end_step": 148},
  {"start_step": 386, "end_step": 412}
]
```

本方案只调整 Folded CFG 的动态执行元数据和联动方式，不修改套利检测逻辑，也不改变 Plain CFG 按单次执行实例展示的定位方式。

## 2. 是否删除 PC

### 结论

删除 Folded CFG 对外元数据和详情界面中的 `start_pc/end_pc`，但不要从底层 CFG、原始 trace 和 Plain CFG 中全局删除 PC。

具体边界如下：

| 位置 | 是否保留 PC | 原因 |
|---|---|---|
| `folded_blocks_information.json` 的 `start_pc/end_pc` | 删除 | Folded 节点可能包含不连续分支、回环和重复执行，单一区间没有可靠语义 |
| Folded CFG 详情面板的 PC 行 | 删除 | 当前结束点为 `None`，而且即使补出最大 PC 也可能误导 |
| Folded CFG 联动键 | 删除 PC 路径 | 改用动态 `source_step → step_ranges → node_id` |
| `Block.start_pc/end_pc` | 保留 | 静态反汇编、基本块切分和 original CFG 构造仍依赖 PC |
| `trace.json` 中每一步的 PC | 保留 | 它是原始执行证据，Plain CFG 和问题审计仍需要 |
| `balance_and_eth_changes.json`、配对结果中的 `source_pcs` | 第一阶段保留 | 暂时作为审计和兼容证据，但不再参与 Folded 联动 |
| Plain CFG 指令中的 PC | 保留 | Plain CFG 是指令级证据层，PC 在这里仍然有价值 |

因此，“去掉 PC”应理解为：**Folded CFG 不再声明一个虚假的连续 PC 范围，也不再用 PC 做跨图匹配**。不应扩大成删除整个分析管线中的 PC，否则会连带破坏字节码分块、original CFG 和追溯能力。

Folded 节点当前的 `instructions` 可以暂时保留，便于显示 opcode 和调试；其中单条指令携带的 PC 不属于节点 PC 范围。如果产品层希望 Folded 视图完全不显示 PC，可以在前端仅显示 opcode，而无需删除底层指令证据。

## 3. 当前问题原因

### 3.1 调用树为什么能正确联动

调用树记录每次调用实例的 `entry_step/exit_step`，再使用 `edge_id-step.json` 中 Folded CFG 实际边的 `edge_step` 找到最终节点。它具有以下特点：

- 使用动态执行 step，可以区分同一合约、同一 PC 的多次执行。
- 直接引用最终 Folded CFG 的节点 ID。
- 不经过 original CFG 到 Folded CFG 的二次翻译。

### 3.2 Token Flow 为什么容易错位

资产变化数据已经保存了 `source_steps`，但 `afg_to_fcfg()` 当前仍采用：

```text
source PC
→ address + PC 定位 original block
→ folded_node_map 翻译
→ Folded CFG node ID
```

这条路径存在以下问题：

1. 同一个静态 PC 被反复执行时无法区分执行实例。
2. dispatch 折叠可能让一个 original block 对应多个 Folded 节点，当前逻辑只取第一个匹配。
3. ERC-20 映射会把已经得到的 Folded ID 再放入 `folded_node_map` 查询一次，存在二次翻译错误。
4. Plain 和 Folded 的 `_mode_fold()` 共同修改 `self.folded_node_map`，同时复用从 1 开始的节点编号，使映射结果可能受执行顺序和 ID 碰撞影响。

### 3.3 PC 结束点为什么是 `None`

原始 basic block 已经有正确的 `end_pc`，但 `build_fcfg_blocks_information()` 没有导出 PC。随后 `_enrich_folded_blocks_information()` 从未维护过 `end_pc` 的 `fold_info` 中取值，因此最终写成字符串 `"None"`。

即使修复这个取值错误，也不能把 Folded 节点简单描述成一个 PC 区间：线性折叠、钻石折叠、回环折叠和 dispatch 下沉都可能让一个节点包含多个不连续的静态块。

## 4. 新的数据契约

### 4.1 Folded block 信息

`folded_blocks_information.json` 中每个节点使用 `step_ranges` 表示全部执行实例：

```json
{
  "42": {
    "block_id": 42,
    "address": "0x...",
    "step_ranges": [
      {"start_step": 120, "end_step": 148},
      {"start_step": 386, "end_step": 412}
    ],
    "blocks_number": 4,
    "folded_blocks": [18, 21, 24, 27],
    "gas": 18342,
    "actions": [],
    "instructions": []
  }
}
```

约束：

- `start_step/end_step` 均为包含端点的整数区间。
- 区间按 `start_step` 升序保存。
- 每次离开并重新进入同一 Folded 节点都保留为独立区间，不因相邻而自动合并。
- 不再使用 `-1` 表示无结束点；交易末尾节点以 `trace.steps.length - 1` 结束。
- 同一执行 step 在最终 Folded CFG 中只能归属于一个节点执行实例；若出现多个候选，应作为构图错误报告，不能静默取第一个。
- Folded block 不再导出标量 `start_pc/end_pc`，也不新增标量 `start_step/end_step`。

Plain CFG 继续保留单个 `start_step/end_step`，因为 Plain 节点本身就是一次动态执行实例。

### 4.2 Token Flow 到 Folded CFG 的映射

`TFG_link_FCFG.json` 保留当前 `edge_id/type/matched_blocks`，降低前端迁移成本，同时增加可审计的 step 证据：

```json
{
  "edge_id": 7,
  "type": "ERC20_TOKEN_TRANSFER",
  "mapping_status": "complete",
  "matched_blocks": {
    "sender": [42, 43],
    "receiver": [61, 62]
  },
  "evidence": [
    {"role": "sender_sload", "source_step": 132, "block_id": 42},
    {"role": "sender_sstore", "source_step": 141, "block_id": 43},
    {"role": "receiver_sload", "source_step": 390, "block_id": 61},
    {"role": "receiver_sstore", "source_step": 401, "block_id": 62}
  ]
}
```

`mapping_status` 取值：

- `complete`：所有证据 step 唯一匹配。
- `partial`：只匹配到部分证据，前端仍可展示已找到的节点。
- `unmatched`：没有 step 能匹配。
- `ambiguous`：同一个 step 命中多个节点，属于构图或区间数据错误。

不能再因为四个 ERC-20 证据中有一个未命中就静默丢弃整条 Token Flow 映射。

## 5. Step 区间的生成方式

Step 区间必须在最终 Folded CFG 构造过程中记录，不能事后通过 PC 推断。

### 5.1 记录原则

在 folded `_mode_fold()` 遍历按 `edge_step` 排序的动态边时，为当前节点维护一次“打开的执行实例”：

```text
进入节点：记录 start_step
遇到该节点的离开边：以 edge_step 关闭当前区间
进入目标节点：目标区间从 edge_step + 1 开始
交易结束：最后一个打开区间以 trace 最后一个 step 关闭
再次进入复用节点：向该节点的 step_ranges 追加新区间
```

关键修改是“追加区间”，而不是像当前代码一样反复覆盖 `fold_info.start_step/end_step`。固定节点可以继续复用同一个图节点对象，但每次访问必须有独立执行区间。

### 5.2 构造期不变量

最终生成产物前检查：

1. 每个区间满足 `0 <= start_step <= end_step < trace_step_count`。
2. 每个资产事件的 `source_step` 最多命中一个 Folded 节点。
3. 每个 `edge_id-step.json` 的 `source_node/target_node` 都存在于 Folded SVG 和 block 信息中。
4. 对同一个 Folded 节点的重复访问会产生多个区间，而不是扩展成覆盖中间其他节点的大区间。
5. 不允许通过 `end_step = -1` 或“step 大于 start 就匹配”的方式吞掉后续所有执行。

## 6. 两条联动路径

### 6.1 调用树 → Folded CFG

保留现有方式：

```text
call_id
→ entry_step / exit_step
→ 区间内的 folded edge_step
→ edge 的实际 source_node / target_node
```

调用树联动关注一次调用覆盖的执行路径，因此使用边 step 范围是合理的。

### 6.2 Token Flow → Folded CFG

改为：

```text
Token Flow edge order
→ 每个资产证据的 source_step
→ 查找包含该 step 的 Folded step_range
→ 最终 Folded node ID
```

各类资产证据分别匹配：

- ETH：`source_steps[0]`
- ERC-20 transfer：`sender_sload_step`、`sender_sstore_step`、`receiver_sload_step`、`receiver_sstore_step`
- 未配对 ERC-20 balance change：两个 `source_steps`

PC 不再参与这条路径，`find_node_by_pc_address()` 和 ERC-20 的二次 `folded_node_map` 查询应从 Folded 联动中移除。

不能直接拿 `edge_id-step.json` 替代 step range：CALL 通常发生在边界上，但 SLOAD/SSTORE 经常位于节点内部。正确做法是让调用树使用边 step，让 Token Flow 使用节点执行区间，两者共享动态 step 坐标。

## 7. 前端展示与交互

### 7.1 Folded 节点详情

- 删除 PC 行。
- 增加 `Step ranges`，按执行顺序展示全部区间。
- 区间较多时默认显示前几项和总数，可展开完整列表。
- 如果 `step_ranges` 为空，显示 `Unknown`，不能退回错误的 PC 匹配。
- Plain CFG 继续显示单个 Step 范围和指令级证据。

建议显示形式：

```text
Step ranges   120–148, 386–412, 905–921  (3 executions)
```

### 7.2 Token Flow 点击

- 前端继续使用 `matched_blocks` 高亮节点。
- `partial/ambiguous/unmatched` 应有明确提示，不能表现成“这条资产流没有对应 CFG”。
- 切换 Folded/Plain 模式时，必须等对应的 `TFG_link_FCFG.json` 或 `TFG_link_PCFG.json` 加载完成后再接受点击，避免短暂使用上一模式的映射。
- 模式切换应保留 Token Flow 的逻辑边选择，并用新模式的映射重新计算高亮，而不是直接丢弃选择。

## 8. 代码改造范围

### 后端

- `backend/utils/cfg_transaction.py`
  - 将 Folded 节点的标量 step 改为 `step_ranges`。
  - 在最终 folded `_mode_fold()` 中记录每次进入/离开区间。
  - 交易末尾使用真实 trace 结束 step，消除 `-1`。
  - Plain 和 Folded 使用独立的节点映射与计数器，避免共享状态污染。
- `backend/utils/extract_token_changes.py`
  - `afg_to_fcfg()` 改用 `source_steps`。
  - 增加 step-range 索引和唯一性检查。
  - 删除 Folded 联动中的 PC 查询和二次 folded ID 翻译。
  - 输出 `evidence` 与 `mapping_status`。
- `backend/main_api.py`、`backend/main.py`
  - 保持服务入口和独立入口的产物语义一致。
  - Folded block 信息不再调用 PC 回填逻辑。
  - 在生成 `TFG_link_FCFG.json` 前完成 Folded step-range 索引。

### 前端

- `frontend/src/api/analyze.ts`
  - 为 Folded block 增加 `step_ranges` 类型。
  - 最好将 Folded/Plain block 信息拆成区分联合类型，避免所有字段都可选。
  - 为映射增加 `evidence/mapping_status` 类型。
- `frontend/src/components/CfgPanel.vue`
  - Folded 详情删除 PC 行，增加多区间 Step 展示。
  - Plain 详情保持现有单区间展示。
- `frontend/src/components/AfgPanel.vue`
  - 保留 Token Flow 选择并在模式映射加载后重新联动。
  - 显示不完整或歧义映射状态。

## 9. 兼容与迁移

旧分析目录没有 `step_ranges`，而现有 `end_pc` 又不可靠，因此不应尝试从旧 PC 范围自动推导新区间。

迁移策略：

1. 新产物增加 Folded step-range schema 版本标识。
2. 前端检测到 Folded block 缺少 `step_ranges` 时，显示“Legacy analysis, please re-run”。
3. 重新分析交易以生成新产物。
4. `matched_blocks` 第一阶段继续保留，避免一次性重写全部前端交互。
5. 等新格式稳定后，再评估是否从配对中删除冗余 `source_pcs`；raw trace 中的 PC 始终保留。

## 10. 实施顺序

1. 先让 Folded CFG 正确生成 `step_ranges`，并验证重复节点的多个区间。
2. 将 `afg_to_fcfg()` 切换到 `source_steps`，同时移除二次 ID 翻译。
3. 更新 Folded block 和 TFG link JSON 契约。
4. 更新前端类型、Step ranges 展示和映射状态。
5. 修复模式切换时 Token Flow 选择丢失或使用旧映射的问题。
6. 删除 Folded PC 行和 Folded 产物中的 `start_pc/end_pc`。
7. 最后清理不再被 Folded 联动调用的 PC 匹配代码；保留 original/plain CFG 所需部分。

## 11. 验收标准

- 同一个 Folded 节点执行三次时，产物中存在三个准确的 step 区间。
- 同一合约、同一 PC 的多次 Token Flow 事件能够通过不同 source step 命中正确执行位置。
- 每个 `TFG_link_FCFG.json` 中的 block ID 都存在于 `folded_cfg.svg` 和 `folded_blocks_information.json`。
- ERC-20 的四个证据点分别可追溯到 source step 和 Folded block，不再发生二次 ID 翻译。
- ETH、ERC-20 transfer、mint/burn、wrap/unwrap 都能联动。
- 调用树 → Folded CFG 的现有 step 联动不回退。
- Folded/Plain 切换后，当前 Token Flow 边仍然选中，并使用对应模式的映射。
- Folded block 信息和 UI 中不再出现 `start_pc/end_pc`、`None` PC 结束点或标量 step。
- 末尾节点具有真实结束 step，不使用无限范围。
- 映射不完整、无匹配和歧义三种状态能够被明确区分。

## 12. 非目标

- 不改变 Token Flow 边的顺序和 `edge_id`。
- 不修改套利候选检测、利润判断或资产配对算法。
- 不把 Folded CFG 改成按执行实例完全展开；精确的逐次执行细节仍由 Plain CFG 承担。
- 不删除 original CFG、Plain CFG 或原始 trace 中用于静态分析和审计的 PC。
