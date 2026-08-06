"""Quantitative Intelligence Layer (Phase Q).

Data → PIT Dataset → Features → Labels → Models → Prediction/Ranking →
Signal → Ensemble → Portfolio → Trade Proposal.

Design follows the adoption matrix in docs/research/quant_system_adoption.md:
Qlib-style protocols (decision-time label, train-only normalization, daily IC,
top-k portfolio) implemented natively on this project's real provider chain.
"""
