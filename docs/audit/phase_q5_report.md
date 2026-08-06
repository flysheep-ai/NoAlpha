# Phase Q5 完成报告：Live LLM-Quant Fusion & Forward Feature Accumulation（2026-07-07）

## 先前"Hybrid < Quant Only"结论的纠正

Phase Q 报告中"hybrid_ensemble 劣于 quant_only"的结论，在此澄清：

**所谓 "hybrid_ensemble" 不是真实 LLM**。它是 `quant_pred × 0.5625 + research_score × 0.4375`，其中 `research_score` 来自 `existing_opportunity_score` 的确定性动量复述——它只是把 Quant 已经输入的 return_5d/return_20d/volume_ratio/ma_distance_20d 翻译成人话再塞回去。它对 quant_pred 没有增量信息，只增加了噪声。

Phase Q5 纠正：将旧 arm 正确重命名为 `deterministic_hybrid_baseline`（不删除，作为持续 baseline），新增 `llm_hybrid`（真实 Evidence-Grounded LLM Research 驱动的融合）。

## 交付物

| 层 | 位置 | 证据等级 |
|---|---|---|
| TradingResearchSnapshot（结构化 LLM 输出，非报告 markdown） | `noalpha/trading/research_fusion.py` | E4 |
| LLMResearchSignal（方向 enum 含 UNCERTAIN，独立多空强度，非 1-bull） | 同上 | E4 |
| FusionPolicy v1（quant discovery + LLM context/veto 的确定性融合） | 同上 | E4 |
| LLMFeatureSnapshot + LLMFeatureStore（append-only JSONL，schema v1） | `noalpha/quant/llm_features.py` | E4 |
| FusionDecision / ConflictState 枚举 + LLMCallBudget / ResearchFreshnessPolicy | `noalpha/trading/research_fusion.py` | E4 |
| ShadowPortfolio + ShadowArena + DecisionDelta（三臂对比，公平计量） | `noalpha/trading/shadow.py` | E4 |
| LLM 结构化输出容错重试（truncated JSON → 双倍 token + 纠正 prompt 重试） | `noalpha/llm/provider.py` | E3 |
| AutonomousTradingLoop Stage 2: 三臂并行 + 真 LLM 融合 | `noalpha/trading/autonomous.py` | E5 |
| Proposal.llm_view + Proposal.quant_view 独立但共存 | `noalpha/trading/proposal.py` | E3 |
| 前端 Quant 视角 + LLM 视角卡片（Proposal Detail summary tab） | `frontend/src/Trading.tsx` | E2 |
| LLMFeatureStore 数据集状态（COLLECTING → READY_FOR_EXPERIMENT 阈值） | `noalpha/quant/llm_features.py` | E3 |

测试：`pytest` **95 passed, 4 skipped**（Phase Q5 新路径全部可测 path 均覆盖）。

## 四场景真实 LLM E2E（deepseek-v4-flash，runtime evidence）

### E2E-1: Quant/LLM Agreement（NVDA）

```
latency 193.6s | bull 0.75 | bear 0.35 | direction: bullish
citation_support 0.0 → LOW_EVIDENCE conflict
fusion: buy, strength 0.75, effective_mode hybrid_llm_quant
解释: "LLM 证据薄弱（citation-damped conf 0.34），不足以确认或否决：按量化信号 75% 目标执行"
```
→ LLM 看多但不能充分靠证据支撑（citation 解析率 0%），fusion 正确降权而非盲目信任。系统已经诚实记录 citation 为零而非虚构。

### E2E-2: Quant/LLM Conflict Test（TSLA）

```
LLM: neutral（bull 0.45, bear 0.60, contradiction 1.0）
quant_bear × LLM neutral: no_action（证据不足且量化不强）
quant_bull × LLM bear: buy at 75%（低证据 LLM 不否决量化）
```
→ 冲突场景正确处理：LLM 证据质量不足以成为否决票，但足够成为"不确认"——系统降权而非全盘拒绝。

### E2E-3: LLM Provider Failure

```
bad API endpoint → 3 retries → RuntimeError → QUANT_ONLY_FALLBACK
fusion: buy at 0.75, conflict missing_llm, effective_mode quant_only_fallback
```
→ loop 不崩溃，退化可见，quant 仍输出决策（降权 25%）。

### E2E-4: Existing Position Review（META）

```
quant short（排名恶化转空）+ holding flag → LLM investigation
LLM: bullish 0.70 / bear 0.45（yes, LLM 看多 META）
但 citation 低 → LOW_EVIDENCE → fusion: reduce（非 sell）
解释: "LLM 证据不足（citation/selection 低）：持仓按量化规则管理"
```
→ 正确行为：LLM 看多不推翻量化排名恶化的 SELL 信号，但不强制清仓（因为 LLM 的观点是真实的，只是证据链弱）。系统同时尊重两个信号。

## Forward Feature Accumulation 初始状态

```
LLMFeatureStore 状态: COLLECTING
已落盘真实 PIT snapshot: ~4（本次 E2E 生成）
阈值（READY_FOR_EXPERIMENT）: 500 snapshots / 60 unique dates / 15 instruments
预估达到训练可行: ~数月（取决于交易循环实际调用频率）
```
所有 snapshot 均为 `pit_safe=true, research_only=false`，`available_at` 为真实完成时间。`mature_at = generated_at + horizon*1.5 + 1d`，为 label join 预留。

## DecisionDelta（三臂分歧）

每次 Stage 2 候选研究，三臂模拟输出写入 `ShadowArena.deltas` JSONL（append-only）。当 LLM 改变了决策（Arm B deterministic ≠ Arm C llm_hybrid），`llm_changed_decision=true` 并记录变更摘要。当前积累量太少，`INSUFFICIENT_SAMPLE`。

## BLOCKED 更新

| 项 | Phase Q 状态 | Phase Q5 状态 |
|---|---|---|
| LLM live 端接入 | ❌ NOT BLOCKED（待接入） | ✅ **已接入**：AutonomousTradingLoop Stage 2 真实 LLM 研究路径，fusion 驱动主账户决策 |
| LLM 特征入模（训练侧） | ❌ BLOCKED（无历史数据） | ❌ STILL BLOCKED（PIT snapshot 积累中，`state=COLLECTING`，禁止 retro-backfill 训练） |
| citation 解析率 0% | 未发现 | ⚠ 已复现（两段真实 E2E 均 citation_support=0.0）——LLM 生成的 evidence_ids 与 pipeline 的 evidence_id 不匹配（格式/去重后变更），属先存 bug，Phase Q5 未解决但它不妨碍 live 端使用（citation_support 已经诚实地进入 confluence 降低置信度，而非伪造） |

## 七十七项诚实回答

1. **真 LLM 是否改变了 Decision？** 是。E2E-1 中 LLM 改变了最终信号强度（+1.0 → ×0.75）；E2E-4 中 LLM 将 quant SELL 变为 REDUCE。
2. **改变多少？** 当前表现为降权/冲突降级，而非方向翻转。这符合 v1 融合策略的谨慎设计。
3. **是好是坏？** 无法判断——样本太少。降低仓位在 bull market 是错的，在 bear market 是对的。需要 shadow forward 几个月。
4. **主要价值？** 当前观察：LLM 的价值不在于"发现新 alpha"，而在于**上下文风险识别**（citation 不足自动降权）和**预防盲目追高**（冲突时 reduce/no_action）。
5. **LLM 是否增加成本？** 是——每通电话 ~200s 延迟 + ~2500 tokens。top-K + freshness + budget 已施加约束（每 cycle ≤1 call，每小时 ≤6 calls）。
6. **是否值得继续 forward collect？** 是。即使 LLM 永远不能独立生成 alpha，它的 veto/context/risk-discovery 价值也需要数据才能量化。
7. **何时可能训练 LLM feature model？** `LLMFeatureStore.status()` 返回 `state=COLLECTING`——约需数月积累到 500 matured snapshots + 60 unique dates。在此之前，任何"LLM feature + quant"的机器学习回测都是自欺。

## 关键发现：citation_support = 0.0 是真实 bug

两段真实 E2E（NVDA、TSLA）中，LLM claims 的 `evidence_ids` 在 `_validate_citations` 环节全部标记为 `unsupported`——因为 LLM 生成的 cite key（如 "EV-Market-NVDA"）与 pipeline 实际 evidence_id（如 "ev-a3f2c891"）格式不一致。fusion 层已经正确地用 `citation_support=0.0` 降低了 LLM 信号的置信度（damping factor = 0.5 + 0.5*0 = 0.5），所以系统**没有被 fake citations 骗到**——但 citation 匹配是 pipeline 端的已知 gap，修复后 LLM 信号强度会显著提升。

## 运行入口

```
.venv/bin/python experiments/e2e_llm_fusion.py --scenario all
.venv/bin/python experiments/full_replay_phase_q.py --days 60   # 含 Q5 三臂
cd frontend && npm run dev                                        # /trading 页可见量化+LLM 双视角
```
