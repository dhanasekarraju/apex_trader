"""AI decision support — ranking & confidence only, never overrides risk."""

from __future__ import annotations

from dataclasses import dataclass

from services.strategies.engine import Signal


@dataclass
class AIRanking:
    symbol: str
    strategy: str
    raw_confidence: float
    ai_confidence: float
    rank_score: float
    position_size_factor: float
    explanation: list[str]


class AISupportLayer:
    """
    ML-ready interface. v1 uses rule-based ensemble scoring.
    Responsibilities: rank trades, adjust confidence, suggest size — NOT approve trades.
    """

    def rank_signals(
        self,
        signals: list[Signal],
        *,
        regime_confidence: float,
        volatility_pct: float,
    ) -> list[AIRanking]:
        ranked: list[AIRanking] = []
        for sig in signals:
            score = sig.confidence
            notes: list[str] = []

            if regime_confidence >= 75:
                score += 4
                notes.append("High regime clarity")
            if volatility_pct < 25:
                score += 3
                notes.append("Volatility manageable")
            if sig.strategy in ("trend_following", "momentum"):
                score += 2

            rr = (sig.take_profit - sig.entry) / max(sig.entry - sig.stop_loss, 1e-9)
            if rr >= 2:
                score += 5
                notes.append(f"R:R {rr:.1f}:1")

            ai_conf = min(98, score)
            size_factor = 1.0 if ai_conf >= 80 else (0.75 if ai_conf >= 72 else 0.5)

            ranked.append(AIRanking(
                symbol=sig.symbol,
                strategy=sig.strategy,
                raw_confidence=sig.confidence,
                ai_confidence=round(ai_conf, 1),
                rank_score=round(ai_conf * (1 + rr * 0.05), 2),
                position_size_factor=size_factor,
                explanation=notes + sig.reasons[:3],
            ))

        ranked.sort(key=lambda r: r.rank_score, reverse=True)
        return ranked
