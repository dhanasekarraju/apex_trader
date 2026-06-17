"""Data quality engine — pause trading on bad feeds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd


@dataclass
class DataQualityReport:
    score: float
    trade_allowed: bool
    issues: list[str]
    missing_bars: int
    duplicate_bars: int
    stale_seconds: float


class DataQualityEngine:
    def __init__(self, min_score: float = 0.85, max_stale_sec: int = 120) -> None:
        self.min_score = min_score
        self.max_stale_sec = max_stale_sec

    def assess(self, df: pd.DataFrame, symbol: str = "") -> DataQualityReport:
        issues: list[str] = []
        score = 1.0

        if df.empty:
            return DataQualityReport(0, False, ["Empty dataframe"], 0, 0, 9999)

        missing = int(df.isnull().any(axis=1).sum())
        if missing:
            score -= min(0.3, missing / len(df))
            issues.append(f"{missing} bars with missing values")

        dupes = int(df.index.duplicated().sum()) if hasattr(df.index, "duplicated") else 0
        if dupes:
            score -= min(0.2, dupes / max(len(df), 1))
            issues.append(f"{dupes} duplicate timestamps")

        stale = 0.0
        bar_seconds = 900.0
        if len(df) > 2 and hasattr(df.index, "to_series"):
            diffs = df.index.to_series().diff().dropna()
            median_gap = diffs.median()
            if len(diffs) and median_gap is not pd.NaT and median_gap > pd.Timedelta(0):
                bar_seconds = max(float(median_gap.total_seconds()), 60.0)

        if hasattr(df.index, "max"):
            try:
                last_ts = pd.Timestamp(df.index.max())
                if last_ts.tzinfo is None:
                    from zoneinfo import ZoneInfo

                    last_ts = last_ts.tz_localize(ZoneInfo("Asia/Kolkata"))
                stale = (datetime.now(timezone.utc) - last_ts.tz_convert("UTC")).total_seconds()
                stale_limit = max(float(self.max_stale_sec), bar_seconds * 1.5)
                if stale > stale_limit:
                    score -= 0.4
                    issues.append(f"Stale feed {stale:.0f}s")
            except Exception:
                pass

        gaps = 0
        if len(df) > 2 and hasattr(df.index, "to_series"):
            diffs = df.index.to_series().diff().dropna()
            median_gap = diffs.median()
            if len(diffs) and median_gap is not pd.NaT and median_gap > pd.Timedelta(0):
                # Overnight/weekend session breaks are expected on daily bar series.
                intraday = (diffs > median_gap * 3) & (diffs < pd.Timedelta(hours=12))
                gaps = int(intraday.sum())
                if gaps:
                    score -= min(0.15, gaps / len(df))
                    issues.append(f"{gaps} abnormal time gaps")

        score = max(0, round(score, 3))
        return DataQualityReport(
            score=score,
            trade_allowed=bool(score >= self.min_score),
            issues=issues,
            missing_bars=missing,
            duplicate_bars=dupes,
            stale_seconds=stale,
        )
