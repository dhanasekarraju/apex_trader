"""Risk dashboard status tests."""

from services.portfolio.manager import PortfolioManager
from services.risk.dashboard import RiskDashboard, RiskStatus


def test_risk_status_safe():
    portfolio = PortfolioManager()
    dash = RiskDashboard(portfolio)
    result = dash.compute({"daily_pnl": 0, "open_exposure": 5})
    assert result["status"] == RiskStatus.SAFE.value
    assert not result["kill_switch"]


def test_risk_status_halted():
    portfolio = PortfolioManager()
    portfolio.emergency_shutdown()
    dash = RiskDashboard(portfolio)
    result = dash.compute()
    assert result["status"] == RiskStatus.HALTED.value
    assert result["kill_switch"]


def test_risk_status_warning():
    portfolio = PortfolioManager()
    portfolio.state.daily_pnl = -12000
    dash = RiskDashboard(portfolio)
    result = dash.compute()
    assert result["status"] in (RiskStatus.WARNING.value, RiskStatus.DANGER.value)
