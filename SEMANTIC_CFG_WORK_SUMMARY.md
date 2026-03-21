# Semantic CFG 工作总结

## 1. 这次完成了什么

本轮开发围绕“让 CFG 主视图更简约、更可读，同时保留可追溯性”展开，最终落地的是一套“规则骨架 + LLM 命名”的语义化简 CFG 方案，而不是让大模型直接自由生成整张 DOT 图。

已经完成的核心能力：

- 在后端新增了 semantic CFG 生成链路。
- 保留 folded CFG 作为底层事实源，不推翻原有 CFG 逻辑。
- 先由程序做规则约束下的候选语义区域切分与聚合，再由 LLM 只负责给这些区域命名和补充语义说明。
- LLM 输出严格 JSON，不直接输出 DOT。
- 后端验证 LLM 输出是否合法，不合法时自动回退到 folded CFG。
- 后端程序化生成 `semantic_cfg.json`、`semantic_cfg.dot`、`semantic_cfg.svg`、`semantic_edge_id-step.json`。
- 前端优先显示 semantic CFG，失败时自动回退到 folded CFG。
- 前端点击语义节点后，可以看到语义摘要、trace step 范围、gas、底层 folded blocks 和 instructions，实现“语义层可读，底层仍可追溯”。
- 兼容 OpenAI-compatible API 中转服务，并适配只支持 `chat.completions` 的提供商。

## 2. 为什么这么做

一开始讨论过“把 dot/json/trace 全喂给 LLM，让它直接输出简化后的 dot”。这个思路做 demo 可以，但默认主视图风险较高，主要问题是：

- 结构不容易验证，模型可能改坏图结构。
- 结果不稳定，同一段 opcode 可能被多种方式命名。
- token 成本高，trace + dot + json 一起喂容易超长。
- 多合约、call/delegatecall、回环、分支汇合等结构很容易被错误压扁。
- 用户难以信任结果，因为缺少“这个语义块到底对应哪些底层 block”的追溯链路。

所以最终采用的是更稳健的路线：

1. folded CFG 仍然是真值来源。
2. 后端先按结构规则切出可合并区域。
3. 再做一层更粗粒度的语义聚合。
4. LLM 只做“命名 + purpose + entry/exit 解释”。
5. DOT 一律由程序生成。
6. 任意异常都自动回退到 folded CFG。

## 3. 最终流程

现在一次交易分析的 semantic CFG 流程如下：

1. 原有后端照常生成交易级 CFG。
2. 导出 `folded_blocks_information.json` 和 `edge_id-step.json`。
3. 从 folded CFG 中提取当前可见节点与可见边。
4. 基于规则生成 fine-grained semantic region candidates。
5. 对 candidates 做第一轮粗聚合。
6. 对聚合结果做第二轮“按目标节点数压缩”。
7. 按合约分组，再按固定 region 数分批请求 LLM。
8. 给 LLM 的输入是结构化 JSON，不是原始 DOT。
9. LLM 返回 JSON：`semantic_node_id`、`label`、`purpose`、`confidence`、`entry_conditions`、`exit_effects`。
10. 后端校验输出是否合法，包括：
    - 是否改动了 member block 归属
    - 是否缺失节点
    - 置信度是否低于阈值
    - label / purpose 是否为空
11. 校验通过后，后端程序化拼装 semantic CFG payload。
12. 渲染 `semantic_cfg.dot` 和 `semantic_cfg.svg`。
13. 前端优先加载 semantic CFG 资源。
14. 如果 semantic 文件不存在、超时或校验失败，则前端继续使用 folded CFG。

## 4. 后端新增功能与逻辑

### 4.1 语义 CFG 构建器

新增文件：

- [backend/utils/semantic_cfg.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/utils/semantic_cfg.py)

这个模块现在负责：

- semantic region candidate 构建
- coarse grouping
- target node count 压缩
- opcode/action 摘要提炼
- LLM 请求与 provider 兼容
- JSON 解析与修复
- 输出合法性校验
- semantic CFG payload 组装
- semantic 结果导出

关键实现点：

- `CALL` / `DELEGATECALL` 默认作为关键边界，不跨它们做激进合并。
- `TERMINATE` 不再被当作绝对硬边界，这样可以进一步减少碎块。
- prompt 中强调“用更高层的 umbrella label 概括整个 region，而不是逐个 micro-step 命名”。
- 每个 region 的 opcode 摘要被缩短，降低 token 成本和噪声。
- 对同一合约先分批，再对单个 batch 超长时自动二分递归拆分。
- 支持返回 fenced JSON 或轻微 malformed JSON 的修复解析。

### 4.2 规则聚合 + 二次压缩

语义聚合不是只做一次。

当前是两层聚合：

- 第一层：基于结构规则，把连续非锚点链条合成初级候选区域。
- 第二层：先按 `OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE` 做 coarse grouping，再按 `OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT` 做压缩。

这次还新增了一轮“相邻候选块迭代合并”：

- 不再只是按固定 chunk size 生硬切块。
- 会在同合约内寻找“最适合合并”的相邻语义块。
- 对带 action、多出入边、关键边界的块增加惩罚。
- 优先吞掉短小、碎片化、偏 setup/dispatch/validation 的小块。

这让语义图进一步从约 `72` 个节点压到了 `28` 个节点。

### 4.3 API 兼容与配置

已接入并调通 OpenAI-compatible provider，支持当前使用的中转服务：

- `OPENAI_BASE_URL=https://aicanapi.com/v1`
- `OPENAI_SEMANTIC_CFG_API_MODE=chat_completions`
- `OPENAI_SEMANTIC_CFG_MODEL=gemini-3-flash-preview`

处理中间遇到的问题：

- provider 不支持 `/responses`
- base URL 可能被错误拼接为双重路径
- 部分 provider 不支持 `response_format`
- 部分 provider 不支持 `reasoning_effort`

现在的兼容策略是：

- 优先按配置的 API mode 调用。
- 如果 `/responses` 不支持，则自动回退到 `chat.completions`。
- 自动规范化 base URL，避免 `/v1/chat/completions/v1/chat/completions` 这类错误。
- 不支持 `response_format` 或 `reasoning_effort` 时自动降级重试。

### 4.4 生成与导出 helper

为了清理开发过程中产生的重复代码，这次又新增了一个统一 helper：

- `generate_and_export_semantic_cfg(...)`

它把这几件事收口到一处：

- 创建 `SemanticCFGBuilder`
- 调用 `build(...)`
- 导出 semantic json / edge-step json
- 返回 `semantic_cfg`

这样 [backend/main.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/main.py) 和 [backend/main_api.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/main_api.py) 就不再各自维护一套重复逻辑了。

## 5. 渲染与前端改动

### 5.1 Semantic DOT 渲染

修改文件：

- [backend/utils/render_cfg.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/utils/render_cfg.py)

新增了 semantic CFG 专用 renderer，特点是：

- 语义节点使用更大的卡片化节点
- 标签只显示短标题，不在图里塞过多细节
- 合同名作为副标题显示
- 边更轻、更弱，减少“全是线、节点很小”的问题
- 取消语义图上的聚类色块背景，避免画面过空、过杂

目标是：

- 图面主信息是“语义节点”
- 细节信息去右侧详情面板看

### 5.2 前端 semantic 优先 + fallback

修改文件：

- [frontend/src/api/analyze.ts](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/frontend/src/api/analyze.ts)
- [frontend/src/components/CfgPanel.vue](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/frontend/src/components/CfgPanel.vue)

新增能力：

- 前端优先拉取：
  - `semantic_cfg.svg`
  - `semantic_cfg.json`
  - `semantic_edge_id-step.json`
- 若缺失或失败，则自动回退：
  - `transaction_cfg.svg`
  - `folded_blocks_information.json`
  - `edge_id-step.json`

语义视图交互：

- 点击语义节点可显示：
  - `label`
  - `purpose`
  - `confidence`
  - contract 信息
  - trace step 范围
  - gas
  - start/end pc
  - entry/exit summary
  - action summary
  - member folded blocks
  - 每个底层 block 的 instructions
- 支持从高亮的 raw block id 反查到语义节点。
- CFG、AFG、trace 之间的导航链路仍然保留。

## 6. 改动过的主要文件

### 后端

- [backend/utils/semantic_cfg.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/utils/semantic_cfg.py)
  - 新增 semantic CFG 主模块
  - provider 兼容
  - prompt / schema / validation / batching / compression
  - 新增 `generate_and_export_semantic_cfg(...)`
  - 本次清理中收拢常量、去掉未使用代码、删除重复分支
- [backend/utils/render_cfg.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/utils/render_cfg.py)
  - 新增 semantic CFG 的 DOT 渲染
  - 增大节点尺寸，弱化边，优化可读性
- [backend/main.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/main.py)
  - 接入 semantic CFG 生成与渲染
  - 改为使用统一 helper
- [backend/main_api.py](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/main_api.py)
  - 接入 semantic CFG 生成与渲染
  - 改为使用统一 helper
- [backend/.env](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/backend/.env)
  - 配置中转 API
  - 配置 semantic CFG model / mode / batching / aggregation

### 前端

- [frontend/src/api/analyze.ts](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/frontend/src/api/analyze.ts)
  - 增加 semantic CFG 数据类型
  - 增加 semantic-first / folded-fallback 的获取逻辑
- [frontend/src/components/CfgPanel.vue](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/frontend/src/components/CfgPanel.vue)
  - 主画布支持语义图
  - 右侧详情支持语义节点详情与底层 block 展开
  - 支持从 raw block 到 semantic node 的映射高亮

### 文档

- [README.md](/c:/Users/lanhe/Desktop/evm-transaction-analyzer/README.md)
  - 补充 semantic CFG 相关环境变量说明

## 7. 新增的主要产物

当 semantic CFG 成功生成时，会在结果目录下新增：

- `semantic_cfg.json`
- `semantic_cfg.dot`
- `semantic_cfg.svg`
- `semantic_edge_id-step.json`

其中：

- `semantic_cfg.json` 是语义层主数据源
- `semantic_cfg.dot` / `semantic_cfg.svg` 是可视化输出
- `semantic_edge_id-step.json` 用于前端过滤、高亮和导航

## 8. 当前重要配置

当前建议配置如下：

```env
OPENAI_BASE_URL=https://aicanapi.com/v1
OPENAI_SEMANTIC_CFG_API_MODE=chat_completions
OPENAI_SEMANTIC_CFG_MODEL=gemini-3-flash-preview
OPENAI_SEMANTIC_CFG_BATCH_SIZE=20
OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE=18
OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT=28
OPENAI_SEMANTIC_CFG_REASONING_EFFORT=minimal
```

配置含义：

- `BATCH_SIZE`：单批送 LLM 的 region 数量
- `COARSE_GROUP_SIZE`：第一轮粗聚合强度
- `TARGET_NODE_COUNT`：第二轮压缩后的目标节点数
- `REASONING_EFFORT=minimal`：尽量关掉深度思考，直接出结果

## 9. 本次“整理和清理”具体做了什么

这次专门做了一轮代码收口，主要包括：

- 删除 `semantic_cfg.py` 中未再使用的状态和工具函数。
- 将重复的大段 schema 和 prompt 抽成模块级常量。
- 将 `main.py` 与 `main_api.py` 中重复的 semantic 生成逻辑提取为统一 helper。
- 精简了一些批次拆分与 chat completion 调用分支，让主流程更短、更直观。
- 保持了现有行为不变：仍然保留校验、fallback、provider 兼容和结果导出。

## 10. 验证情况

已完成的验证：

- `python -m py_compile` 已通过：
  - `backend/utils/semantic_cfg.py`
  - `backend/main.py`
  - `backend/main_api.py`
  - `backend/utils/render_cfg.py`
- 实际重新运行样例交易：
  - `0x840ecb2b5d55a682afd529138b36e97992eda9706e206237b57ec4697e4f8186`
- 验证到：
  - semantic CFG 成功生成
  - 中转 API 实际返回 `200 OK`
  - semantic 视图成功导出
  - 当前语义 CFG 压缩到 `28` 个节点

## 11. 后续开发注意事项

### 11.1 不要让 LLM 直接生成 DOT

继续保持：

- LLM 只输出结构化 JSON
- DOT 仍由程序渲染

否则结构可验证性和稳定性会明显下降。

### 11.2 聚合强度优先靠规则，不要优先靠 prompt

如果后面还想把节点继续压少，优先调：

- `OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE`
- `OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT`
- `_merge_penalty(...)`

不要首先指望改 prompt 就能减少节点数，因为节点数主要由前置规则聚合决定。

### 11.3 需要继续关注语义标签质量

当前已经明显比最开始更高层了，但仍可能出现：

- “dispatch / validate / check result” 这类标签偏多
- 某些大合约里语义仍然偏保守

后续如果继续优化，可以考虑：

- 在 prompt 中加入少量好坏标签示例
- 对入口/分发/收尾类 region 再做规则级合并
- 引入基于 actions 的更强语义提示

### 11.4 继续保留 fallback

semantic CFG 不是稳定真值层，必须继续保留 folded fallback，尤其在这些情况下：

- LLM 超时
- provider 输出非法 JSON
- confidence 太低
- provider 临时不可用

### 11.5 评估“是否真的更易读”

后续不要只看节点数减少，还要看：

- 是否更快定位关键 token / ETH 变化
- 是否更快定位关键跨合约路径
- 是否更容易理解 swap / transfer / bookkeeping 的整体流程

建议后面用几类固定样本交易做主观和客观对比测试。

## 12. 当前结论

这套 semantic CFG 功能已经从“想法”变成了可以跑通、能导出、能前端展示、支持 fallback 的完整链路。

当前版本的定位是：

- 可作为 folded CFG 之上的语义抽象层
- 已经适合继续迭代
- 还可以继续优化语义质量和聚合粒度
- 但架构方向已经基本稳定，不建议再回到“纯 LLM 直出整图”的路线
