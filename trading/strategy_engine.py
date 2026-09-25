"""
AENIDA Strategy Engine
Playbook system + backtester. Heavy work runs on Worker PC.
BUG-35 fix: fee (0.05%) + slippage (0.03%) applied to every trade.
Reports gross_return AND net_return separately.
"""

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STRATEGIES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "strategies")


# ── Playbook ──────────────────────────────────────────────────────
@dataclass
class Playbook:
    """A trading strategy stored as structured data."""
    name: str
    version: int = 1
    active: bool = True
    entry_rules: List[str] = field(default_factory=list)
    exit_rules: List[str] = field(default_factory=list)
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 8.0
    risk_per_trade_pct: float = 2.0
    max_positions: int = 3
    market_conditions: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    raw_md: str = ""


def load_playbook(strategy_name: str) -> Optional[Playbook]:
    """Load a playbook from strategies/ folder."""
    path = os.path.join(STRATEGIES_DIR, f"{strategy_name}.playbook.md")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        content = f.read()

    pb = Playbook(name=strategy_name, raw_md=content)

    # Parse YAML front matter
    front = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if front:
        for line in front.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k == "version":
                    pb.version = int(v)
                elif k == "active":
                    pb.active = v.lower() == "true"
                elif k == "stop_loss_pct":
                    pb.stop_loss_pct = float(v)
                elif k == "take_profit_pct":
                    pb.take_profit_pct = float(v)
                elif k == "risk_per_trade_pct":
                    pb.risk_per_trade_pct = float(v)

    # Parse entry/exit rules (simple extraction)
    entry_section = re.search(r"## Entry Rules\n(.*?)(?=##|\Z)",
                               content, re.DOTALL)
    if entry_section:
        pb.entry_rules = [l.strip("- ").strip()
                          for l in entry_section.group(1).splitlines()
                          if l.strip().startswith("-")]

    exit_section = re.search(r"## Exit Rules\n(.*?)(?=##|\Z)",
                              content, re.DOTALL)
    if exit_section:
        pb.exit_rules = [l.strip("- ").strip()
                         for l in exit_section.group(1).splitlines()
                         if l.strip().startswith("-")]
    return pb


def save_playbook(playbook_md: str, name: str) -> str:
    """Save playbook markdown to strategies/ folder."""
    os.makedirs(STRATEGIES_DIR, exist_ok=True)
    path = os.path.join(STRATEGIES_DIR, f"{name}.playbook.md")
    with open(path, "w") as f:
        f.write(playbook_md)
    logging.info(f"[STRATEGY] Saved playbook: {name}")
    return path


def create_playbook_from_natural_language(description: str) -> str:
    """
    Use Worker AI to convert natural language into a playbook.
    Returns the generated playbook markdown.
    """
    prompt = f"""Convert this trading strategy description into a structured playbook.

Description: {description}

Return ONLY valid markdown with this exact structure:
---
name: strategy_name_snake_case
version: 1
active: true
stop_loss_pct: 3.0
take_profit_pct: 8.0
risk_per_trade_pct: 2.0
max_positions: 3
---

## Entry Rules
- Rule 1
- Rule 2

## Exit Rules
- Stop loss: X% from entry
- Take profit: Y% from entry
- Rule 3

## Position Sizing
- Risk per trade: Z% of portfolio
- ATR-based sizing where applicable

## Market Conditions
- Only trade if: condition

No explanations. Only the markdown playbook."""

    try:
        from model_shell import call
        result = call(prompt)
        md = result.get("result", "")
        md = re.sub(r"```markdown\n?|```\n?", "", md).strip()
        return md
    except Exception as e:
        logging.error(f"[STRATEGY] Playbook generation failed: {e}")
        return f"---\nname: custom_strategy\nversion: 1\nactive: true\n---\n\n## Entry Rules\n- {description}\n"


def list_strategies() -> List[Dict[str, Any]]:
    """List all saved strategy playbooks."""
    os.makedirs(STRATEGIES_DIR, exist_ok=True)
    result = []
    for f in os.listdir(STRATEGIES_DIR):
        if f.endswith(".playbook.md"):
            name = f.replace(".playbook.md", "")
            pb = load_playbook(name)
            if pb:
                result.append({
                    "name": name,
                    "version": pb.version,
                    "active": pb.active,
                    "stop_loss_pct": pb.stop_loss_pct,
                    "take_profit_pct": pb.take_profit_pct,
                })
    return result


def activate_strategy(name: str) -> bool:
    """Mark a strategy as active."""
    pb = load_playbook(name)
    if not pb:
        return False
    content = pb.raw_md.replace("active: false", "active: true")
    save_playbook(content, name)
    return True


def deactivate_strategy(name: str) -> bool:
    """Mark a strategy as inactive."""
    pb = load_playbook(name)
    if not pb:
        return False
    content = pb.raw_md.replace("active: true", "active: false")
    save_playbook(content, name)
    return True


# ── Backtester (BUG-35 fee model) ────────────────────────────────
@dataclass
class Trade:
    """A simulated trade."""
    entry_price: float
    exit_price: float
    direction: str          # "long" | "short"
    entry_time: float
    exit_time: float
    stop_loss: float
    take_profit: float
    taker_fee_pct: float = 0.05
    slippage_pct: float = 0.03

    @property
    def gross_pnl_pct(self) -> float:
        if self.direction == "long":
            return (self.exit_price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - self.exit_price) / self.entry_price * 100

    @property
    def total_cost_pct(self) -> float:
        """BUG-35: taker_fee + slippage applied at BOTH entry and exit."""
        return (self.taker_fee_pct + self.slippage_pct) * 2

    @property
    def net_pnl_pct(self) -> float:
        """BUG-35: net after fees."""
        return self.gross_pnl_pct - self.total_cost_pct


def _calculate_metrics(trades: List[Trade],
                        initial_capital: float = 10000.0) -> Dict[str, Any]:
    """
    Calculate 16 performance metrics.
    BUG-35: gross AND net return reported separately.
    """
    if not trades:
        return {"error": "No trades to evaluate"}

    gross_returns = [t.gross_pnl_pct for t in trades]
    net_returns = [t.net_pnl_pct for t in trades]
    winning = [r for r in net_returns if r > 0]
    losing = [r for r in net_returns if r <= 0]

    total_gross = sum(gross_returns)
    total_net = sum(net_returns)
    total_fees = sum(t.total_cost_pct for t in trades)
    win_rate = len(winning) / len(net_returns) if net_returns else 0
    avg_win = sum(winning) / len(winning) if winning else 0
    avg_loss = sum(losing) / len(losing) if losing else 0
    profit_factor = (sum(winning) / abs(sum(losing))
                     if losing and sum(losing) != 0 else float("inf"))

    # Max drawdown
    equity = [initial_capital]
    for r in net_returns:
        equity.append(equity[-1] * (1 + r / 100))
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe (annualised, assume daily)
    if len(net_returns) > 1:
        mean_r = sum(net_returns) / len(net_returns)
        var = sum((r - mean_r) ** 2 for r in net_returns) / len(net_returns)
        std = math.sqrt(var) if var > 0 else 0.0001
        sharpe = (mean_r / std) * math.sqrt(252) if std else 0.0
    else:
        sharpe = 0.0

    # Sortino
    downside = [r for r in net_returns if r < 0]
    if downside and len(downside) > 1:
        mean_r = sum(net_returns) / len(net_returns)
        down_var = sum(r ** 2 for r in downside) / len(downside)
        down_std = math.sqrt(down_var)
        sortino = (mean_r / down_std) * math.sqrt(252) if down_std else 0.0
    else:
        sortino = 0.0

    calmar = total_net / max_dd if max_dd > 0 else 0.0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    recovery_factor = total_net / max_dd if max_dd > 0 else 0.0
    volatility = math.sqrt(
        sum((r - (total_net / len(net_returns))) ** 2
            for r in net_returns) / len(net_returns)
    ) if len(net_returns) > 1 else 0.0
    ulcer_idx = math.sqrt(
        sum((r ** 2 for r in
             [max(0, -r) for r in net_returns])) / len(net_returns)
    ) if net_returns else 0.0

    return {
        "total_trades": len(trades),
        "gross_return_pct": round(total_gross, 2),
        "net_return_pct": round(total_net, 2),       # BUG-35
        "total_fees_pct": round(total_fees, 2),       # BUG-35
        "total_fees_dollars": round(total_fees / 100 * initial_capital, 2),
        "win_rate": round(win_rate * 100, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3),
        "expectancy_pct": round(expectancy, 2),
        "recovery_factor": round(recovery_factor, 2),
        "volatility_pct": round(volatility, 2),
        "ulcer_index": round(ulcer_idx, 2),
    }


def backtest(strategy_name: str,
             ohlcv_data: List[Dict[str, Any]],
             taker_fee_pct: float = 0.05,
             slippage_pct: float = 0.03,
             initial_capital: float = 10000.0) -> Dict[str, Any]:
    """
    Run backtest for a strategy against OHLCV data.
    BUG-35: fee + slippage applied to EVERY trade at entry AND exit.
    """
    pb = load_playbook(strategy_name)
    if not pb:
        return {"error": f"Strategy not found: {strategy_name}"}
    if not ohlcv_data or len(ohlcv_data) < 35:
        return {"error": "Insufficient data (need 35+ candles)"}

    closes = [c.get("close", 0) for c in ohlcv_data]
    highs = [c.get("high", 0) for c in ohlcv_data]
    lows = [c.get("low", 0) for c in ohlcv_data]
    times = [c.get("timestamp", i * 3600) for i, c in enumerate(ohlcv_data)]

    # Simple RSI-based signal for all strategies (rules would normally
    # be parsed from playbook — this is a universal baseline)
    trades: List[Trade] = []
    in_trade = False
    entry_price = 0.0
    entry_time = 0.0
    sl_price = 0.0
    tp_price = 0.0

    # Calculate RSI
    rsi_values = []
    try:
        from technical_indicators import TechnicalIndicators
        ti = TechnicalIndicators()
        for i in range(14, len(closes)):
            r = ti.calculate_rsi(closes[:i + 1])
            rsi_values.append(r.value if r.value else 50.0)
    except Exception:
        rsi_values = [50.0] * len(closes)

    for i in range(14, len(closes)):
        rsi = rsi_values[i - 14] if i - 14 < len(rsi_values) else 50.0
        price = closes[i]

        if not in_trade:
            # Entry: RSI oversold
            if rsi < 35:
                entry_price = price
                entry_time = times[i]
                sl_price = price * (1 - pb.stop_loss_pct / 100)
                tp_price = price * (1 + pb.take_profit_pct / 100)
                in_trade = True
        else:
            # Exit conditions
            exited = False
            exit_price = price

            if lows[i] <= sl_price:
                exit_price = sl_price
                exited = True
            elif highs[i] >= tp_price:
                exit_price = tp_price
                exited = True
            elif rsi > 70:
                exited = True

            if exited:
                trades.append(Trade(
                    entry_price=entry_price,
                    exit_price=exit_price,
                    direction="long",
                    entry_time=entry_time,
                    exit_time=times[i],
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    taker_fee_pct=taker_fee_pct,
                    slippage_pct=slippage_pct,
                ))
                in_trade = False

    metrics = _calculate_metrics(trades, initial_capital)
    metrics["strategy"] = strategy_name
    metrics["data_points"] = len(ohlcv_data)
    return metrics


def export_pine_script(strategy_name: str) -> str:
    """Export strategy as Pine Script v5 for TradingView."""
    pb = load_playbook(strategy_name)
    if not pb:
        return f"// Strategy '{strategy_name}' not found"

    pine = f"""//@version=5
strategy("{pb.name}", overlay=true, default_qty_type=strategy.percent_of_equity,
         default_qty_value={pb.risk_per_trade_pct}, commission_type=strategy.commission.percent,
         commission_value={0.05})

// === AENIDA Strategy: {pb.name} v{pb.version} ===
// Generated by AENIDA Trading Agent

// Indicators
rsi = ta.rsi(close, 14)
[macdLine, signalLine, hist] = ta.macd(close, 12, 26, 9)
ema20 = ta.ema(close, 20)

// Entry Conditions
longEntry = rsi < 35 and hist > 0 and close > ema20

// Exit Conditions
stopLoss   = strategy.position_avg_price * (1 - {pb.stop_loss_pct}/100)
takeProfit = strategy.position_avg_price * (1 + {pb.take_profit_pct}/100)

// Execute
if longEntry
    strategy.entry("Long", strategy.long)

if strategy.position_size > 0
    strategy.exit("Exit", "Long", stop=stopLoss, limit=takeProfit)

// Plots
plot(ema20, color=color.blue, linewidth=1)
bgcolor(longEntry ? color.new(color.green, 90) : na)
"""
    return pine


def get_strategy_performance(strategy_name: str) -> Dict[str, Any]:
    """Quick summary of strategy (no backtest)."""
    pb = load_playbook(strategy_name)
    if not pb:
        return {"error": f"Strategy not found: {strategy_name}"}
    return {
        "name": pb.name,
        "version": pb.version,
        "active": pb.active,
        "stop_loss_pct": pb.stop_loss_pct,
        "take_profit_pct": pb.take_profit_pct,
        "entry_rules_count": len(pb.entry_rules),
        "exit_rules_count": len(pb.exit_rules),
    }
