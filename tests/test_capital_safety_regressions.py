"""Regression tests for broker ambiguity, payoff, capital and exit accounting."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import asyncio
import pytest
from fastapi.testclient import TestClient
from services.brokers.base import OrderRequest, OrderResult, OrderStatus, OrderType
from services.brokers.kite import KiteBroker
from services.execution.execution_engine import ExecutionEngine
from services.execution.lifecycle import PositionLifecycleService
from services.execution.reconciliation import reconcile_on_startup
from services.portfolio.manager import PortfolioManager, PositionView
from services.sizing.engine import PositionSizingEngine, SizeInput
from services.risk.engine import RiskEngine, RiskState, TradeProposal
from shared.config import get_settings

def position():
    return PositionView('TEST', 10, 100, 95, 110, 'trend_following', 0, 0.5, stop_order_id='STOP')

def fake_result(status=OrderStatus.FILLED, qty=10, price=110):
    return OrderResult('exit', 'BROKER', status, qty, price, 0, 'test')

@pytest.mark.asyncio
async def test_real_adapter_failure_keeps_position_and_degrades():
    broker = KiteBroker()
    broker._kite = NS(positions=Mock(side_effect=ConnectionError('network down')))
    pf = PortfolioManager()
    pf.state.positions = [position()]
    report = await reconcile_on_startup(broker=broker, portfolio=pf,
        trades=NS(open_trades=AsyncMock(return_value=[])), trading_mode='live')
    assert report['reconciliation_status'] == 'DEGRADED'
    assert len(pf.state.positions) == 1

@pytest.mark.asyncio
async def test_disconnected_positions_are_unknown():
    with pytest.raises(ConnectionError):
        await KiteBroker().fetch_open_positions()

@pytest.mark.asyncio
async def test_absent_position_does_not_create_fake_exit():
    pf = PortfolioManager()
    pf.state.positions = [position()]
    lifecycle = PositionLifecycleService(portfolio=pf, execution=ExecutionEngine(portfolio=pf), market_data=Mock())
    lifecycle._close_position = AsyncMock()
    await lifecycle._sync_missing_broker_positions(NS(fetch_open_positions=AsyncMock(return_value=[])), 'live')
    lifecycle._close_position.assert_not_awaited()

@pytest.mark.asyncio
async def test_partial_exit_retains_and_persists_remaining_quantity():
    pf = PortfolioManager()
    await pf.load()
    p = position()
    await pf.record_fill(p)
    await pf.record_exit(symbol='TEST', qty=4, exit_price=110, exit_reason='target', pnl=40)
    assert pf.state.positions[0].qty == 6
    loaded = PortfolioManager()
    await loaded.load()
    assert loaded.state.positions[0].qty == 6
    assert loaded.state.daily_pnl == 40

@pytest.mark.asyncio
async def test_lifecycle_accounts_actual_partial_fill():
    pf = PortfolioManager()
    pf.state.positions = [position()]
    execution = ExecutionEngine(portfolio=pf)
    execution.place_exit = AsyncMock(return_value=fake_result(OrderStatus.PARTIAL, 4))
    lifecycle = PositionLifecycleService(portfolio=pf, execution=execution, market_data=Mock())
    lifecycle._ltps = AsyncMock(return_value={'TEST':110})
    lifecycle._close_position = AsyncMock()
    await lifecycle._check_position(pf.state.positions[0], NS(fetch_order_status=AsyncMock(return_value={'status':'TRIGGER PENDING','pending_quantity':10,'filled_quantity':0,'trigger_price':95,'tradingsymbol':'TEST','transaction_type':'SELL','exchange':'NSE','product':'MIS','order_type':'SL-M'})), 'live')
    assert lifecycle._close_position.call_args.kwargs['qty'] == 4

@pytest.mark.asyncio
async def test_target_cancels_and_confirms_stop_before_selling():
    get_settings().trading_mode = 'live'
    get_settings().default_broker = 'kite'
    pf = PortfolioManager()
    pf.state.positions = [position()]
    engine = ExecutionEngine(portfolio=pf)
    events = []
    async def cancel(oid): events.append('cancel'); return True
    async def status(oid): events.append('confirm'); return {'status':'CANCELLED','filled_quantity':0}
    async def sell(req, price, cfg): events.append('sell'); return fake_result()
    engine._broker = NS(cancel_order=cancel, fetch_order_status=status,
        fetch_open_positions=AsyncMock(return_value=[dict(symbol='TEST',qty=10,side='long')]),
        reconcile_order=AsyncMock(return_value=fake_result()))
    engine._place_with_retry = sell
    result = await engine.place_exit(symbol='TEST',qty=10,reason='target',market_price=110)
    assert events == ['cancel','confirm','sell']
    assert result.filled_qty == 10

@pytest.mark.asyncio
async def test_unconfirmed_stop_cancel_never_sells():
    get_settings().trading_mode = 'live'
    get_settings().default_broker = 'kite'
    pf = PortfolioManager()
    pf.state.positions = [position()]
    engine = ExecutionEngine(portfolio=pf)
    engine._broker = NS(cancel_order=AsyncMock(return_value=False),fetch_order_status=AsyncMock(return_value={'status':'UNKNOWN'}))
    engine._place_with_retry = AsyncMock()
    result = await engine.place_exit(symbol='TEST',qty=10,reason='target',market_price=110)
    assert result.status == OrderStatus.REJECTED
    engine._place_with_retry.assert_not_awaited()
    assert pf.is_trading_halted()

@pytest.mark.asyncio
async def test_stop_fill_race_does_not_double_sell():
    get_settings().trading_mode = 'live'
    get_settings().default_broker = 'kite'
    pf = PortfolioManager()
    pf.state.positions = [position()]
    engine = ExecutionEngine(portfolio=pf)
    engine._broker = NS(cancel_order=AsyncMock(return_value=False),fetch_order_status=AsyncMock(return_value={'status':'COMPLETE','filled_quantity':10,'average_price':94}))
    engine._place_with_retry = AsyncMock()
    result = await engine.place_exit(symbol='TEST',qty=10,reason='target',market_price=110)
    assert result.filled_qty == 10
    assert result.avg_price == 94
    engine._place_with_retry.assert_not_awaited()

@pytest.mark.asyncio
async def test_failed_flatten_preserves_exposure():
    pf = PortfolioManager()
    pf.state.positions = [position()]
    engine = ExecutionEngine(portfolio=pf)
    engine.place_exit = AsyncMock(return_value=fake_result(OrderStatus.REJECTED,0,0))
    result = await engine.activate_kill_switch()
    assert not result['ok']
    assert pf.state.positions[0].qty == 10
    assert pf.is_trading_halted()

@pytest.mark.asyncio
async def test_live_timeout_submits_once_and_latches_unknown():
    cfg = get_settings()
    cfg.trading_mode = 'live'
    cfg.default_broker = 'kite'
    cfg.external_api_timeout_sec = 0.01
    pf = PortfolioManager()
    engine = ExecutionEngine(portfolio=pf)
    async def slow(*args): await asyncio.sleep(1)
    engine._broker = NS(place_order=AsyncMock(side_effect=slow))
    result = await engine._place_with_retry(OrderRequest('TEST','long',1,OrderType.MARKET,stop_price=95),100,cfg)
    assert engine._broker.place_order.await_count == 1
    assert result.raw['unknown']
    assert pf.is_trading_halted()

@pytest.mark.asyncio
async def test_sync_does_not_erase_drawdown():
    pf = PortfolioManager()
    pf.state.equity, pf.state.peak_equity = 94000,100000
    await pf.sync_capital_from_kite(94000,94000)
    assert pf.state.peak_equity == 100000
    assert pf.metrics()['drawdown_pct'] == 6

@pytest.mark.asyncio
async def test_zero_cash_does_not_fall_back_to_old_balance():
    pf = PortfolioManager()
    await pf.sync_capital_from_kite(0,0,0)
    assert pf.state.cash == 0
    assert pf.state.buying_power == 0

@pytest.mark.parametrize('bp', [10000,50000,128000])
def test_10000_capital_position_value_is_not_leveraged(bp):
    size = PositionSizingEngine().compute(SizeInput(equity=10000,cash=10000,buying_power=bp,entry=100,stop_loss=99))
    assert size.qty * 100 <= 2500
    assert size.risk_rs <= 50

def test_no_cash_means_no_trade():
    size = PositionSizingEngine().compute(SizeInput(equity=10000,cash=0,buying_power=50000,entry=100,stop_loss=99))
    assert size.qty == 0

def test_exhausted_daily_budget_means_no_trade():
    size = PositionSizingEngine().compute(SizeInput(equity=10000,cash=10000,entry=100,stop_loss=99,daily_pnl=-150))
    assert size.qty == 0

def test_total_open_value_cannot_use_margin_as_extra_cash():
    size = PositionSizingEngine().compute(SizeInput(equity=10000, cash=9000,
        buying_power=50000, open_notional=8900, entry=100, stop_loss=99))
    assert size.qty <= 1

def test_backtest_uses_intrabar_gap_and_retains_full_history():
    import pandas as pd
    from services.backtest.engine import BacktestEngine
    from services.strategies.engine import Signal
    df = pd.DataFrame({'open':[100.0]*400,'high':[101.0]*400,
        'low':[99.0]*400,'close':[100.0]*400,'volume':[100000]*400},
        index=pd.date_range('2026-01-01',periods=400,freq='15min'))
    df.iloc[62, df.columns.get_loc('open')] = 90
    df.iloc[62, df.columns.get_loc('low')] = 89
    engine = BacktestEngine()
    engine.regime.analyze = Mock(return_value=NS(trade_allowed=True, regime=NS(value='trend_up'),recommended_strategies=['trend_following']))
    engine.strategies.scan = Mock(return_value=[Signal('TEST','trend_following','long',100,95,110,80,0,[])])
    result = engine.run('TEST',df)
    assert result.trades[0]['exit_reason'] == 'gap_stop'
    assert result.trades[0]['exit'] < 90
    assert len(result.trades) == result.total_trades > 20
    assert all(not t['win'] for t in result.trades)

def test_low_net_payoff_rejected():
    proposal = TradeProposal('TEST','equity','long',100,98,100.8,10,90,'momentum','trend_up')
    decision = RiskEngine().evaluate(proposal,RiskState(equity=10000,cash=10000,peak_equity=10000))
    assert not decision.approved
    assert any(c.name == 'net_reward_risk' and not c.passed for c in decision.checks)

def test_dashboard_never_discloses_shared_credential():
    from services.gateway.main import app
    client = TestClient(app)
    denied = client.get('/')
    assert denied.status_code == 401
    assert 'Basic' in denied.headers.get('www-authenticate','')
    response = client.get('/', auth=('apex','test-api-key-for-ci'))
    assert response.status_code == 200
    assert 'test-api-key-for-ci' not in response.text
    assert 'APEX_API_KEY' not in response.text

def test_shadow_persists_completed_net_outcomes():
    from services.shadow.engine import ShadowEngine
    engine = ShadowEngine()
    req = OrderRequest('TEST','long',10,OrderType.MARKET,stop_price=95,take_profit=110)
    engine.simulate(req,100)
    assert engine.weekly_report()['completed_trades'] == 0
    restored = ShadowEngine()
    restored.simulate_exit(OrderRequest('TEST','short',10,OrderType.MARKET),110)
    report = ShadowEngine().weekly_report()
    assert report['completed_trades'] == 1
    assert 0 < report['total_shadow_pnl'] < 100
