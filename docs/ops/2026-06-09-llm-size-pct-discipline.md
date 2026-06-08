# 2026-06-09 LLM size_pct 决策纪律强化

## 背景

近 7 天 `rejects` 表 ORDER_MARGIN 拒单 9 次，**全部 size_pct 0.25-0.40**（100% 超 0.2 硬上限）：

| symbol | size_pct | leverage | reason |
|---|---|---|---|
| ETHUSDT | 0.40 | 4 | order margin 2009.82 > max 1004.91 (20% of 5024.54) |
| ETHUSDT | 0.40 | 4 | order margin 2006.43 > max 1003.64 (20% of 5018.19) |
| SOLUSDT | 0.30 | 5 | order margin 1505.43 > max 1003.62 (20% of 5018.10) |
| SOLUSDT | 0.30 | 4 | order margin 1505.43 > max 1003.62 (20% of 5018.10) |
| ETHUSDT | 0.30 | 5 | order margin 1507.36 > max 1004.91 (20% of 5024.54) |
| ... | ... | ... | ... |

风险层 `src/risk/manager.py` 校验生效，决策被拒单（不截断），但 LLM 反复提交超限值说明**prompt 强调不够**。

## 根因

1. **system prompt 表述模糊**：决策原则 6 只说 "size_pct 为动用可用保证金比例(0~1)，按机会质量与风险动态调整"——LLM 理解成"0-1 内随便选"
2. **user prompt 给的是绝对 USDT 值**（1003.59），没显式说"对应 0.2 比例"。LLM 拿绝对值反推 0.2 上限**不直观**——容易当成"该用的目标值"
3. LLM 自己 reason 里出现"0.20×5018"是算对了（说明数学能力没问题），但决策字段填了 0.3（纪律问题，不是数学问题）

## 设计目标

- **不评判 LLM**（不说"切勿用足"——让 LLM 自主判断机会质量）
- **强调"硬上限"概念**：决策超过即被拒单，**不是软警告**
- **百分比 + 绝对值并列** user prompt：LLM 一眼看到 "20.0% 硬上限" 和对应 USDT 金额
- 风险层校验逻辑**不变**（ORDER_MARGIN 拒单保留，作为最终兜底）

## 改动

### A. system prompt 决策原则 6

**前**：
```
6. size_pct 为动用可用保证金比例(0~1)，按机会质量与风险动态调整，单笔注意控制风险敞口。
```

**后**：
```
6. size_pct 为动用可用保证金比例(0~1)，存在硬上限 max_order_margin_pct（系统按权益动态设置，
   通常约 0.2），超过该硬上限的决策会被直接拒单（不截断、不调整）。
```

要点：
- "存在硬上限 max_order_margin_pct"——明确是**硬约束**（与 leverage 5.x 拒单同语气）
- "通常约 0.2"——给具体数值，LLM 知道上限在哪
- "不截断、不调整"——杜绝 LLM 假设"系统会帮我降一点"
- **未说**"切勿用足" / "宁小勿大" / "保守"——这些是评判，让 LLM 自主判断

### B. user prompt size_pct 行

**前**：
```
单笔保证金上限: 1003.59 USDT（= size_pct × 可用保证金，不得超过此值，否则被拒单）
```

**后**：
```
单笔保证金硬上限: size_pct ≤ 20.0% (即 max_order_margin_pct=0.2000，硬性约束，超出直接拒单)
   对应绝对金额: 1003.60 USDT (= 0.2000 × 可用保证金 5018.00)
```

要点：
- 百分比写在最前（"size_pct ≤ 20.0%"）——LLM 一眼看到硬边界
- 绝对值并列在下一行，便于 LLM 交叉确认
- "硬性约束" + "超出直接拒单" 跟 system 表述一致

### 配套

`src/llm/schema.py`：`MarketContext` 新增 `max_order_margin_pct: float = 0.0` 字段（默认值 0 兼容旧数据）
`src/features/builder.py`：`build_context` 传 `max_order_margin_pct=settings.risk.max_order_margin_pct`

## 改动文件

| 文件 | 改动 |
|---|---|
| `src/llm/prompt.py` | SYSTEM 决策原则 6 改 2 行；user_prompt size_pct 行改 2 行 |
| `src/llm/schema.py` | MarketContext 加 1 字段 |
| `src/features/builder.py` | build_context 传 1 参数 |
| `tests/test_size_pct_prompt_discipline.py` | 3 个新测试（system 含硬上限/拒单；user 显式 pct+abs；pct 随 config 变） |

## 风险评估

| 风险 | 严重度 | 缓解 |
|---|---|---|
| LLM 矫枉过正（全部填 0.05） | 低 | prompt 未限制下限，LLM 自主判断 |
| LLM 不变（继续填 0.3） | 低 | 风险层 ORDER_MARGIN 拒单兜底（已观察有效） |
| 与现有规则冲突 | 极低 | size_pct 是独立变量，跟 leverage/stop_loss 正交 |
| prompt 变长增加 token | 极低 | 增加 ~30 token，影响 < 0.05% |

## 监控

跑 1-2 天看：
- `rejects.code='ORDER_MARGIN'` 数量变化（目标：从 9/7天 降到 0-2/7天）
- `decisions.size_pct` 分布（target: 0.05-0.20 内集中）
- LLM reason 是否出现 "max_order_margin_pct" 字样（说明 LLM 在 reasoning 时考虑了约束）
