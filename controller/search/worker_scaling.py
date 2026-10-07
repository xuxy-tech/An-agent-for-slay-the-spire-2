from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


def recommended_hardware_workers(logical_cpu_count: Optional[int] = None) -> int:
    """Return a conservative live-run ceiling while reserving CPU for the game."""
    logical = max(1, int(logical_cpu_count or os.cpu_count() or 1))
    reserved = 2 if logical >= 6 else 1
    usable = max(1, logical - reserved)
    target = max(1, int(logical * 0.8))
    return min(usable, target, 16)


def recommended_initial_workers(logical_cpu_count: Optional[int] = None) -> int:
    """Use the live-run ceiling so shallow frontiers can fill available CPU."""
    return min(8, recommended_hardware_workers(logical_cpu_count))


def _windows_cpu_percent(interval_s: float = 0.2) -> float:
    class FileTime(ctypes.Structure):
        _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]

    def read_times():
        idle, kernel, user = FileTime(), FileTime(), FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def value(item):
            return (int(item.high) << 32) | int(item.low)

        return value(idle), value(kernel), value(user)

    before = read_times()
    time.sleep(max(0.01, interval_s))
    after = read_times()
    if before is None or after is None:
        return 0.0
    idle = after[0] - before[0]
    total = (after[1] - before[1]) + (after[2] - before[2])
    return max(0.0, min(100.0, 100.0 * (total - idle) / total)) if total > 0 else 0.0


def system_cpu_percent(interval_s: float = 0.2) -> float:
    if os.name == 'nt':
        return _windows_cpu_percent(interval_s)
    try:
        load = os.getloadavg()[0]
        return max(0.0, min(100.0, 100.0 * load / max(1, os.cpu_count() or 1)))
    except (AttributeError, OSError):
        time.sleep(max(0.01, interval_s))
        return 0.0


class CpuDecisionMonitor:
    def __init__(self, sampler: Callable[[float], float] = system_cpu_percent, interval_s: float = 0.2):
        self.sampler = sampler
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._samples = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._samples = []
        self._stop.clear()

        def collect():
            while not self._stop.is_set():
                self._samples.append(float(self.sampler(self.interval_s)))

        self._thread = threading.Thread(target=collect, daemon=True)
        self._thread.start()

    def finish(self) -> Dict[str, float]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.5, self.interval_s * 3.0))
        samples = list(self._samples)
        return {
            'cpu_avg_percent': round(sum(samples) / len(samples), 3) if samples else 0.0,
            'cpu_peak_percent': round(max(samples), 3) if samples else 0.0,
            'cpu_samples': len(samples),
            'cpu_window_ms': round(len(samples) * self.interval_s * 1000.0, 3),
        }


@dataclass
class AdaptiveWorkerController:
    hardware_max: int
    mode: str = 'adaptive'
    current_workers: int = 0
    underloaded_streak: int = 0

    def __post_init__(self) -> None:
        logical = max(1, os.cpu_count() or 1)
        self.hardware_max = max(1, min(int(self.hardware_max or logical), logical))
        if self.current_workers <= 0:
            self.current_workers = min(
                self.hardware_max,
                recommended_initial_workers(logical),
            )
        self.current_workers = max(1, min(self.current_workers, self.hardware_max))

    def workers_for(self, root_count: int) -> int:
        return max(1, min(self.current_workers, max(1, int(root_count or 1))))

    def observe(
        self,
        cpu_avg: float,
        cpu_peak: float,
        queued_root_work: bool,
        deadline_overrun: bool = False,
        deadline_overrun_ratio: float = 0.0,
        worker_failures: int = 0,
        memory_pressure: bool = False,
    ) -> Dict[str, object]:
        before = self.current_workers
        reason = 'stable'
        if self.mode != 'adaptive':
            return {'before': before, 'after': before, 'reason': 'fixed_mode'}
        if cpu_peak >= 95.0:
            self.current_workers = max(1, self.current_workers - 2)
            self.underloaded_streak = 0
            reason = 'cpu_peak_safety'
        elif memory_pressure or worker_failures > 0 or cpu_avg > 90.0:
            self.current_workers = max(1, self.current_workers - 1)
            self.underloaded_streak = 0
            reason = 'runtime_pressure'
        elif deadline_overrun:
            # Engine calls are not preemptible, so an overrun alone is not a
            # reason to shrink the pool. When root work is still queued and the
            # machine is idle, add capacity to improve candidate coverage.
            self.underloaded_streak = 0
            can_scale_for_coverage = (
                queued_root_work
                and deadline_overrun_ratio < 2.0
                and cpu_avg < 70.0
                and cpu_peak < 85.0
                and self.current_workers < self.hardware_max
            )
            if can_scale_for_coverage:
                step = 2 if self.current_workers >= 4 and cpu_avg < 45.0 and cpu_peak < 70.0 else 1
                self.current_workers = min(self.hardware_max, self.current_workers + step)
                reason = 'deadline_coverage_scale_up'
            else:
                reason = (
                    'severe_deadline_overrun_held'
                    if deadline_overrun_ratio >= 2.0
                    else 'deadline_overrun_held'
                )
        elif cpu_avg < 70.0 and queued_root_work:
            self.underloaded_streak += 1
            if self.underloaded_streak >= 2 and self.current_workers < self.hardware_max:
                self.current_workers += 1
                self.underloaded_streak = 0
                reason = 'sustained_underload'
            else:
                reason = 'underload_observation'
        else:
            self.underloaded_streak = 0
        return {'before': before, 'after': self.current_workers, 'reason': reason}
