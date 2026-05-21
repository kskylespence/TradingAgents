"""One-off driver: run the TradingAgents pipeline on CRM for 2026-05-19.

debug=True streams node-by-node progress to stdout so we can follow what's
happening (analyst → researcher → trader → risk → portfolio manager) without
the rich-panel CLI UI.

Windows console: debug messages contain Greek letters (σ for volatility, etc.)
which cp1252 can't encode, so force stdout to UTF-8 before any imports that
might print.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()

ta = TradingAgentsGraph(debug=True, config=config)

_, decision = ta.propagate("CRM", "2026-05-19")
print("\n" + "=" * 70)
print("FINAL DECISION")
print("=" * 70)
print(decision)
