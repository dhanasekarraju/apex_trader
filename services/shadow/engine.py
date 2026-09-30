"""Persistent shadow entries and completed net outcomes from real quotes."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from services.brokers.base import OrderResult, OrderStatus
from shared.config import get_settings

SHADOW_DIR = Path(__file__).resolve().parents[2] / 'data' / 'shadow'

class ShadowEngine:
    def __init__(self):
        self.cfg = get_settings()
        self.events = []
        self.positions = {}
        SHADOW_DIR.mkdir(parents=True, exist_ok=True)
        self.path = SHADOW_DIR / 'lifecycle-v2.jsonl'
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                self.events.append(event)
                self._apply(event)

    def _apply(self, event):
        if event['kind'] == 'entry':
            self.positions[event['symbol']] = event
        elif event['kind'] == 'exit':
            self.positions.pop(event['symbol'], None)

    def _record(self, event):
        event['timestamp'] = datetime.now(timezone.utc).isoformat()
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, allow_nan=False) + '\n')
        self.events.append(event)
        self._apply(event)

    def simulate(self, req, market_price, actual_later_price=None):
        if req.symbol in self.positions:
            return OrderResult(req.client_order_id, '', OrderStatus.REJECTED, 0, 0, 0, 'Shadow position already open')
        fill = market_price * (1 + self.cfg.shadow_slippage_bps / 10000)
        self._record(dict(kind='entry', symbol=req.symbol, qty=req.qty, entry=fill,
                          strategy=req.strategy, stop_loss=req.stop_price, take_profit=req.take_profit,
                          client_order_id=req.client_order_id))
        return OrderResult(req.client_order_id, 'SHADOW-' + req.client_order_id, OrderStatus.FILLED,
                           req.qty, fill, self.cfg.shadow_slippage_bps, 'Shadow entry; outcome pending')

    def simulate_exit(self, req, market_price):
        pos = self.positions.get(req.symbol)
        if not pos or req.qty != pos['qty']:
            return OrderResult(req.client_order_id, '', OrderStatus.REJECTED, 0, 0, 0, 'Shadow position mismatch')
        fill = market_price * (1 - self.cfg.shadow_slippage_bps / 10000)
        costs = pos['entry'] * req.qty * self.cfg.estimated_round_trip_cost_bps / 10000
        pnl = (fill - pos['entry']) * req.qty - costs
        self._record(dict(kind='exit', symbol=req.symbol, qty=req.qty, exit=fill, pnl=pnl,
                          strategy=pos['strategy'], entry_timestamp=pos['timestamp']))
        return OrderResult(req.client_order_id, 'SHADOW-EXIT-' + req.client_order_id, OrderStatus.FILLED,
                           req.qty, fill, self.cfg.shadow_slippage_bps, 'Shadow exit', raw={'net_pnl': pnl})

    def record_missed(self, symbol, reason, market_price):
        self._record(dict(kind='missed', symbol=symbol, reason=reason))

    def weekly_report(self):
        exits = [e for e in self.events if e['kind'] == 'exit']
        entries = [e for e in self.events if e['kind'] == 'entry']
        days = {e['timestamp'][:10] for e in entries + exits}
        return dict(period='since_reset', simulated_fills=len(entries), completed_trades=len(exits),
                    open_positions=len(self.positions), active_days=len(days),
                    missed_opportunities=sum(e['kind'] == 'missed' for e in self.events),
                    avg_slippage_bps=self.cfg.shadow_slippage_bps,
                    total_shadow_pnl=round(sum(e['pnl'] for e in exits), 2),
                    win_rate=100 * sum(e['pnl'] > 0 for e in exits) / len(exits) if exits else 0)
