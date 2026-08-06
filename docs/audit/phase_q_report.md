# Phase Q 完成报告：Quantitative Intelligence Layer（2026-07-07）

## 交付物

| 层 | 位置 | 证据等级 |
|---|---|---|
| Feature Registry（23 特征 ×6 类，版本化，含公式/回看/可用性规则） | `noalpha/quant/features.py` | E2 |
| Factor Registry（Momentum/Reversal/LowVol/Liquidity） | 同上 | E2 |
| Label Engine + Decision-Time Protocol（T close 决策 → T+1 open 执行 → T+1+h open 平仓） | `noalpha/quant/labels.py` | E2 |
| PIT Dataset Builder（横截面 label、按日最小截面过滤、版本哈希） | `noalpha/quant/dataset.py` | E4 |
| WalkForwardSplitter（purge=horizon+1+embargo，只提供时序切分） | 同上 | E4 |
| 模型层：Linear/LightGBM/XGBoost + 4 个 baseline，train-only 归一化 | `noalpha/quant/models.py` | E4 |
| IC/RankIC/ICIR + net-of-cost top-k 回测（CN 含卖出印花税参数） | `noalpha/quant/evaluation.py` | E4 |
| Experiment Store（可复现：同 seed 同 metrics，已断言） | `noalpha/quant/experiment.py` | E3 |
| Model Registry + Promotion Gate（EXPERIMENTAL→…→ACTIVE 禁止跳级，已断言） | `noalpha/quant/registry.py` | E3 |
| Live Inference（训练/推理同引擎同列，缺列拒绝预测；横截面新鲜度门） | `noalpha/quant/inference.py` | E5 |
| Signal Calibration（分桶命中率，rank→校准概率） | `noalpha/quant/calibration.py` | E4 |
| Signal Ensemble + SignalConflict（冲突不平均，reduce/no_action） | `noalpha/quant/ensemble.py` | E5 |
| Portfolio baseline（EW / InverseVol + max-weight + turnover penalty） | `noalpha/quant/portfolio.py` | E5 |
| 主链路接入（rank→top-k→research→ensemble→optimizer→risk→broker） | `noalpha/trading/autonomous.py` | E5 |
| API `/api/quant/*` + 前端 `/quant` 页 + Trading 页 Quant Panel + Proposal Quant View | `noalpha/quant/routes.py`, `frontend/src/Quant.tsx` | E2E |
| 数据服务（tencent qfq 分页回溯 2019-，磁盘缓存） | `noalpha/quant/data.py` | E4 |

测试：`pytest` 95 passed（含 7 项 Leakage Attack、3 项主链路集成场景）；Playwright quant E2E 2 passed。

## 真实历史 Walk-Forward（24 只/市场，2019-01→2026-07，11 folds，expanding train 500d / valid 60d / test 120d / purge 11d，net of cost：佣金 5bp+滑点 10bp，CN 加卖出印花税 5bp）

### A 股（top-5，5 日调仓）

| 模型 | IC | Rank IC | Rank ICIR | 净收益 | Sharpe | MDD | 换手 |
|---|---|---|---|---|---|---|---|
| random | -0.006 | +0.002 | +0.01 | **-47.2%** | -0.45 | -54% | 0.83 |
| simple_momentum | +0.021 | +0.018 | +0.06 | **+71.5%** | +0.58 | -35% | 0.37 |
| simple_reversal | +0.007 | +0.002 | +0.01 | -31.0% | -0.16 | -43% | 0.76 |
| 现有 opportunity_score | +0.014 | +0.013 | +0.04 | **-26.1%** | -0.14 | -57% | 0.58 |
| linear_ridge | +0.015 | -0.003 | -0.01 | -31.3% | -0.18 | -47% | 0.49 |
| **lightgbm（冠军，ACTIVE）** | +0.033 | +0.027 | +0.115 | +11.5% | +0.21 | -30% | 0.46 |
| xgboost | +0.030 | **+0.035** | **+0.154** | -2.4% | +0.09 | -36% | 0.52 |
| 等权基准 | — | — | — | +2.9% | +0.13 | -32% | 0 |

### 美股（同设置）

等权基准 +149.8%（Sharpe 1.02）；simple_momentum +198%；lightgbm +145.7%；linear_ridge Rank IC 最高（+0.022）但净收益仅 +40%。**没有模型在 Sharpe 上稳定跑赢等权** → 美股 lightgbm 只注册到 SHADOW，不驱动交易。

### Hybrid 对比（同 OOS 区间，第一百一十五条）

| 策略 | CN 净收益 | US 净收益 |
|---|---|---|
| Quant Only (lightgbm) | **+11.5%** | **+145.7%** |
| Research Only（deterministic 研究分数） | -13.3% | +96.4% |
| Hybrid Ensemble | -1.8% | +13.4% |

**Hybrid 没有更好**（第一百一十六条要求诚实）：当前 research 是无 LLM 的确定性动量复述，与 quant 信号高度相关但噪声更大，融合后稀释了排名质量。真 LLM 证据接入前，ensemble 的价值在**冲突拦截**而非 alpha 增强。

### Leakage 演示（第一百一十二条，两市场均复现）

same-bar label（close[T+4]/close[T-1]，含决策日当天涨跌）把 RankIC 从 0.027 吹到 **0.364**（13 倍虚假优势）；同一模型放回可执行收益回测：CN **-33.1%**（干净协议 +11.5%）。系统能制造并识破漂亮但虚假的结果。

### Full-Day/Multi-Day Replay（E5）

60 个真实 A 股交易日（2026-04-07→07-03）通过真实主链路（ACTIVE lightgbm 每日刷新推理 60 次）：
- future_bars_served = 0（代码断言）
- Quant-driven BUY 完整链：quant rank + 校准概率 46.2% + weight chain（alpha 0.0672 → optimizer 0.075 → pre-risk 0.0672）→ risk → fill
- Quant-driven REDUCE：603288 排名 22/24、预期超额转负 → 减仓
- 12 次 Quant/Research 冲突 → no_action / reduce（未被平均掉）
- 60 天净值 -2.61%；按决策质量归因 realized PnL 已落盘

## 一百二十五条诚实回答

1. **哪种模型最好？** 没有全能赢家。A 股按 RankICIR：xgboost > lightgbm；按净收益：simple_momentum。选 lightgbm 做 ACTIVE 是"信号质量+正净收益"的折衷。美股无模型胜出。
2. **优于原策略？** 是。原 opportunity_score 在 A 股 OOS 净收益 -26.1%，lightgbm +11.5%。
3. **优于简单 Momentum？** 净收益上**否**（CN momentum +71.5%）。lightgbm 胜在 RankIC/回撤/换手结构，但必须承认 momentum baseline 更赚钱——小 universe + 白马股行情下动量极强。
4. **优于 Equal Weight？** CN 是（+11.5% vs +2.9%）；US 否（+145.7% vs +149.8%，Sharpe 更低）。
5. **Quant+LLM 优于 Quant Only？** 当前**否**（见上表）。原因：research 端没有真 LLM/事件证据。
6. **哪些特征有效？** CN 全样本 IC：动量族（return_20d、momentum_5_20、ma_distance）与波动族最稳；见 `/quant` 因子表。
7. **哪些无效？** 短期反转（simple_reversal OOS 亏损）、volume_zscore 单因子 IC 噪声大。
8. **哪些模型过拟合？** linear_ridge：IC +0.015 但 RankIC -0.003（拟合幅值不拟合排序）；random baseline 高换手下 -47% 印证成本模型在惩罚噪声交易。
9. **A股/美股不同？** 显著。CN 横截面可预测性略高（RankIC 0.027 vs 0.005）；US 巨型股 beta 淹没 alpha。
10. **数据偏差？** ①幸存者偏差：universe 是今日成分（`survivorship_bias_risk=true` 全链路携带）；②tencent qfq 前复权用当前复权因子回推历史价（点位非当时所见，收益率近似正确）；③无退市股。
11. **最大 Leakage 风险？** 剩余最大风险是 **universe 选择本身**（今日活跃≈过去活过），其次 qfq 复权；bar 级别泄漏已被攻击套件覆盖。
12. **足以长期模拟交易？** 结构上是（promotion gate、freshness 门、冲突处理、E5 replay 已通）；但换手/成本控制（60 天 187 fills）需要 loop 级冷却期，建议先以 PAPER 观察。
13. **BLOCKED 能力**（更正于 2026-07-07：LLM key 实际已配置且有效——`.cache/api/credentials.json` 中的 DeepSeek `deepseek-v4-pro`，07-06 已有成功调用记录；此前"无 key"是过时结论）：
    - **LLM-derived 特征入模（训练侧）**：仍 BLOCKED，但原因不是 key，而是 **PIT 历史数据不存在**——训练 2019–2026 模型需要"当时"生成的 LLM 研究输出（available_at ≤ T）。系统近期才开始积累 LLM 研究记录；用今天的 LLM 补写历史观点等于用未来知识污染训练集（第十二条禁止）。解锁方式：从现在起持续落盘结构化 LLM 输出（bull_strength 等，带 model/prompt_version/context_id），积累数月后方可入模。
    - **LLM research 进交易循环（live 侧）**：**不算 BLOCKED**——是待接入项。当前 AutonomousTradingLoop 用 deterministic research 喂 ensemble；LLM research 路径在 investigation pipeline 中已可用，接入 loop 是工程工作而非数据缺口。
    - 基本面/估值 PIT 特征（无 PIT 财报 provider）；事件特征入模（Investigation 事件未结构化为训练数据）；跨市场特征（需时间对齐的隔夜行情快照存储）；PIT 历史成分股（无数据源）；HRP/CVaR 优化器（等 EW/InvVol 有更长 OOS 证据）。

## 运行入口

```
.venv/bin/python experiments/quant_phase_q.py --market cn --leakage-demo
.venv/bin/python experiments/train_champion.py --market cn
.venv/bin/python experiments/full_replay_phase_q.py --days 60
cd frontend && npx playwright test tests/e2e/quant.spec.ts
```
