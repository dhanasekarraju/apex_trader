"""Shadow mode — real data, simulated execution, performance tracking."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.brokers.base import OrderRequest, OrderResult, OrderStatus, OrderType
from shared.config import get_settings
from shared.logging import audit

_ROOT = Path(__file__).resolve().parents[2]
SHADOW_DIR = _ROOT / "data" / "shadow"


@dataclass
class ShadowFill:
    symbol: str
    side: str
    qty: float
    simulated_fill: float
    market_at_signal: float
    slippage_bps: float
    would_have_pnl: float | None
    missed: bool
    strategy: str
    timestamp: str


class ShadowEngine:
    """
    Real market data + simulated fills.
    No broker orders placed.
    """

    def __init__(self) -> None:
        self.cfg = get_settings()
        SHADOW_DIR.mkdir(parents=True, exist_ok=True)
        self.fills: list[ShadowFill] = []

    def simulate(
        self,
        req: OrderRequest,
        market_price: float,
        actual_later_price: float | None = None,
    ) -> OrderResult:
        slip_bps = self.cfg.shadow_slippage_bps
        slip = market_price * slip_bps / 10000
        fill = market_price + slip if req.side == "long" else market_price - slip

        would_pnl = None
        if actual_later_price:
            would_pnl = (actual_later_price - fill) * req.qty if req.side == "long" else (fill - actual_later_price) * req.qty

        record = ShadowFill(
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            simulated_fill=round(fill, 4),
            market_at_signal=market_price,
            slippage_bps=slip_bps,
            would_have_pnl=round(would_pnl, 2) if would_pnl else None,
            missed=False,
            strategy=req.strategy,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.fills.append(record)
        self._persist(record)
        audit("shadow_fill", symbol=req.symbol, fill=fill, slip_bps=slip_bps)

        return OrderResult(
            req.client_order_id, f"SHADOW-{len(self.fills)}",
            OrderStatus.FILLED, req.qty, fill, slip_bps,
            "Shadow simulated fill",
        )

    def record_missed(self, symbol: str, reason: str, market_price: float) -> None:
        record = ShadowFill(
            symbol=symbol, side="—", qty=0, simulated_fill=0,
            market_at_signal=market_price, slippage_bps=0,
            would_have_pnl=None, missed=True, strategy="—",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.fills.append(record)
        audit("shadow_missed", symbol=symbol, reason=reason)

    def _persist(self, record: ShadowFill) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = SHADOW_DIR / f"shadow_{day}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def weekly_report(self) -> dict:
        fills = [f for f in self.fills if not f.missed]
        missed = [f for f in self.fills if f.missed]
        pnls = [f.would_have_pnl for f in fills if f.would_have_pnl is not None]
        return {
            "period": "weekly",
            "simulated_fills": len(fills),
            "missed_opportunities": len(missed),
            "avg_slippage_bps": round(
                sum(f.slippage_bps for f in fills) / len(fills), 2
            ) if fills else 0,
            "total_shadow_pnl": round(sum(pnls), 2) if pnls else 0,
            "win_rate": round(
                sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1
            ) if pnls else 0,
        }
