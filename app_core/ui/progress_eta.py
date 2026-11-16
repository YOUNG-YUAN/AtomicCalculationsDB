"""Elapsed-time and ETA helpers for long-running GUI tasks."""

from __future__ import annotations

import re
import time
from collections import deque


_STAGE_RE = re.compile(r"阶段\s*([1-5])\s*/\s*5")
# End points of the five local-clean progress stages.  Stage 4 (MiniLM
# loading, encoding and similarity) is much longer than the cleanup stages,
# so its remaining time is estimated from its own rate instead of assuming
# that stage 5 will consume progress at the same speed.
_STAGE_END = {1: 0.33, 2: 0.38, 3: 0.44, 4: 0.81, 5: 0.99}
_FUTURE_PRIOR = {
    1: 3.0 + 2.0 + 210.0 + 25.0,
    2: 2.0 + 210.0 + 25.0,
    3: 210.0 + 25.0,
    4: 25.0,
    5: 0.0,
}


def format_duration(seconds: float | None) -> str:
    """Format a duration compactly for the status bar."""
    if seconds is None:
        return "计算中…"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return "不足 1 秒"
    if seconds < 60:
        return f"{int(round(seconds))} 秒"
    if seconds < 3600:
        minutes, remainder = divmod(int(round(seconds)), 60)
        return f"{minutes} 分 {remainder:02d} 秒"
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes = remainder // 60
    return f"{hours} 小时 {minutes:02d} 分"


class EtaTracker:
    """Monotonic progress timer with stage-aware remaining-time estimates.

    The caller feeds the same 0–1 fraction shown by the progress bar.  When
    the label carries a ``阶段 N/5`` marker, the ETA uses that stage's own
    rate plus short priors for the remaining stages.  This avoids the common
    failure mode where a fast metadata stage makes the global rate look fast
    and a long MiniLM stage makes the ETA grow to many minutes.
    """

    def __init__(self) -> None:
        self._started: float | None = None
        self._last_fraction = 0.0
        self._samples: deque[tuple[float, float]] = deque(maxlen=220)
        self._eta: float | None = None
        self._stage = 0
        self._stage_started: float | None = None

    def reset(self) -> None:
        self._started = time.monotonic()
        self._last_fraction = 0.0
        self._samples.clear()
        self._eta = None
        self._stage = 0
        self._stage_started = None

    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started)

    def _set_stage(self, label: str, fraction: float, now: float) -> None:
        match = _STAGE_RE.search(label or "")
        if not match:
            return
        stage = int(match.group(1))
        if stage != self._stage:
            self._stage = stage
            self._stage_started = now
            self._eta = None

    def _stage_rate(self, now: float, fraction: float) -> float | None:
        """Progress rate within the current stage, using a 30 s window."""
        if self._stage_started is None:
            return None
        samples = [
            (sample_time, sample_fraction)
            for sample_time, sample_fraction in self._samples
            if sample_time >= self._stage_started - 1e-6
        ]
        if len(samples) < 2:
            return None
        if self._stage in (4, 5):
            # Stage 4 starts with a bookkeeping jump (0.44 → 0.47) and model
            # loading; stage 5 starts with the NLI model load. Excluding the
            # first sample keeps those fixed costs from inflating the rate.
            if len(samples) < 3:
                return None
            samples = samples[1:]
        base_time, base_fraction = samples[0]
        for sample_time, sample_fraction in samples:
            if now - sample_time <= 30:
                break
            base_time, base_fraction = sample_time, sample_fraction
        window = max(0.0, now - base_time)
        delta = max(0.0, fraction - base_fraction)
        if window >= 2.0 and delta > 1e-4:
            return delta / window
        # Short stages can finish before a two-second window exists.
        base_time, base_fraction = samples[0]
        window = max(0.0, now - base_time)
        delta = max(0.0, fraction - base_fraction)
        if window >= 0.5 and delta > 1e-5:
            return delta / window
        return None

    def _remaining(self, now: float, fraction: float) -> float | None:
        if fraction >= 0.999:
            return 0.0
        if self._stage in _STAGE_END:
            stage_end = _STAGE_END[self._stage]
            if fraction >= stage_end:
                return _FUTURE_PRIOR[self._stage]
            rate = self._stage_rate(now, fraction)
            if rate is None or rate <= 1e-9:
                return None
            stage_remaining = max(0.0, (stage_end - fraction) / rate)
            remaining = stage_remaining + _FUTURE_PRIOR[self._stage]
            if self._stage == 5:
                remaining = min(remaining, 600.0)
            return min(24 * 3600.0, remaining)

        if fraction <= 0.005:
            return None
        # Incremental/force extraction has no stage markers; use the global
        # rate with a short window as before.
        base_time, base_fraction = self._samples[0]
        for sample_time, sample_fraction in self._samples:
            if now - sample_time <= 30:
                break
            base_time, base_fraction = sample_time, sample_fraction
        window = max(0.0, now - base_time)
        delta = max(0.0, fraction - base_fraction)
        if window >= 2.0 and delta > 1e-4:
            rate = delta / window
        else:
            rate = fraction / max(max(0.0, now - self._started), 1e-6)
        if rate <= 1e-9:
            return None
        return min(24 * 3600.0, max(0.0, (1.0 - fraction) / rate))

    def update(
        self, fraction: float, label: str = ""
    ) -> tuple[float, float | None]:
        """Return ``(elapsed_seconds, remaining_seconds_or_none)``."""
        if self._started is None:
            self.reset()
        assert self._started is not None
        now = time.monotonic()
        fraction = max(0.0, min(1.0, float(fraction)))
        # Progress callbacks must be monotonic; tolerate a tiny rounding
        # regression without making the ETA jump backwards.
        if fraction < self._last_fraction:
            fraction = self._last_fraction
        self._last_fraction = fraction
        self._set_stage(label, fraction, now)
        self._samples.append((now, fraction))
        elapsed = max(0.0, now - self._started)

        remaining = self._remaining(now, fraction)
        if remaining is not None:
            if self._stage:
                self._eta = remaining
            elif self._eta is None or self._eta <= 0:
                self._eta = remaining
            else:
                # Non-staged extraction: blend lightly to avoid a jittering
                # label, but adopt material changes immediately.
                ratio = remaining / self._eta if self._eta else 0.0
                self._eta = (
                    0.45 * remaining + 0.55 * self._eta
                    if 0.33 <= ratio <= 3.0
                    else remaining
                )
        elif fraction >= 0.999:
            self._eta = 0.0
        return elapsed, self._eta

    def summary(self, fraction: float, label: str = "") -> str:
        elapsed, remaining = self.update(fraction, label)
        if fraction >= 0.999:
            return f"总用时 {format_duration(elapsed)} · 已完成"
        if remaining is None:
            return f"已用 {format_duration(elapsed)} · 预计剩余 计算中…"
        return (
            f"已用 {format_duration(elapsed)} · "
            f"预计剩余 {format_duration(remaining)}"
        )

    def finish_text(self) -> str:
        return f"总用时 {format_duration(self.elapsed())} · 已完成"

    def stopped_text(self) -> str:
        return f"已用 {format_duration(self.elapsed())} · 已停止"
