"""Per-host concurrency and rate, shared by every thread of the writer process (operator decision 2026-09-28).

One :class:`HostGovernor` per host:

* **slots** - documents in flight for the host (``acquire_slot``); the limit adapts (AIMD) between 1 and the
  host's maximum (NRW 8, RII 6, juris Länder portals 4, court sites 2);
* **token bucket** - request rate: ``limit / min_interval`` requests per second (each slot is worth one
  request per ``min_interval``), burst ``limit``; every request of every thread takes a token;
* **additive increase** - +1 slot when the last increase is ``INCREASE_EVERY`` (120 s) ago, the host was not
  throttled for ``COOLDOWN`` (5 min), the window has at least ``MIN_SAMPLES`` answers, none failed, and their
  p95 latency is below ``2 ×`` the baseline (the p95 of the first ``BASELINE_SAMPLES`` answers);
* **multiplicative decrease** - on 429/503, a challenge page, a connection reset or a latency spike (p95 of the
  last ``SPIKE_SAMPLES`` answers above ``3 ×`` baseline): halve, wait out Retry-After (every thread of the
  host), no increase for ``COOLDOWN``;
* **repeated blocks** - 3 throttles within 10 min: the host drops to 1 slot for 30 min and the log says ALERT.

Challenges, CAPTCHAs and logins are never bypassed: they only ever lower the rate (and stop the source after
repeated blocks, sync.MAX_CONSECUTIVE_BLOCKING).  The clock is injectable for tests.
"""
from __future__ import annotations

import collections
import contextlib
import threading
import time
from dataclasses import dataclass

INCREASE_EVERY = 120.0
COOLDOWN = 300.0
BLOCK_WINDOW = 600.0
BLOCKS_TO_ALERT = 3
ALERT_HOLD = 1800.0
BASELINE_SAMPLES = 20
MIN_SAMPLES = 10
SPIKE_SAMPLES = 40      # p95 of 20 answers is their maximum: one slow document would read as a spike
SPIKE_MEDIAN = 1.5      # a spike is sustained: the median of the recent answers also rose by half
GROW_BELOW = 2.0
SPIKE_ABOVE = 3.0
SPIKE_MARGIN = 1.0      # and at least this many seconds above the baseline (small baselines are noisy)
MIN_BASELINE = 0.2
LOCAL_LAG = 0.5         # seconds a 100 ms sleep overran: this process was stalled, not the host
LAG_WINDOW = 15.0


class LagMonitor:
    """How late a 100 ms sleep wakes up in this process.  Latency measured while every thread was stalled
    (GIL, disk) says nothing about the host: such spikes neither halve nor count as quiet windows."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.samples: collections.deque = collections.deque(maxlen=2000)
        self._thread = threading.Thread(target=self._run, name="lag-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            started = self.clock()
            time.sleep(0.1)
            self.samples.append((self.clock(), max(0.0, self.clock() - started - 0.1)))

    def max_lag(self, seconds: float = LAG_WINDOW) -> float:
        now = self.clock()
        return max((lag for at, lag in list(self.samples) if now - at <= seconds), default=0.0)


@dataclass(frozen=True)
class Concurrency:
    start: int
    maximum: int


def _p95(values) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(0.95 * len(values)))] if values else 0.0


def _median(values) -> float:
    values = sorted(values)
    return values[len(values) // 2] if values else 0.0


class HostGovernor:
    def __init__(self, host: str, *, start: int, maximum: int, min_interval: float, log=None, clock=time.monotonic,
                 sleep=time.sleep, lag=None):
        self.host, self.maximum, self.min_interval = host, max(1, maximum), max(1.0, min_interval)
        self.limit = max(1, min(start, self.maximum))
        self.log = log or (lambda message: None)
        self.clock, self.sleep = clock, sleep
        self.lag = lag or (lambda: 0.0)       # LagMonitor.max_lag of the process
        self.local_stalls = 0
        self.cond = threading.Condition()
        now = clock()
        self.in_flight = 0
        self.tokens = float(self.limit)
        self.refilled = now
        self.blocked_until = 0.0
        self.cooldown_until = 0.0
        self.last_change = now
        self.ceiling_until = 0.0
        self.baseline: float | None = None
        self.baseline_median: float | None = None
        self._first: list[float] = []
        self.window: list[float] = []          # latencies since the last change
        self.window_errors = 0
        self.recent: collections.deque = collections.deque(maxlen=SPIKE_SAMPLES)
        self.blocks: collections.deque = collections.deque()
        self.requests = 0
        self.peak = self.limit
        self.history: list[tuple[float, int, str]] = [(now, self.limit, "start")]

    # --- slots (documents in flight) -------------------------------------------------------------
    @contextlib.contextmanager
    def slot(self):
        with self.cond:
            while self.in_flight >= self.limit:
                self.cond.wait(timeout=1.0)
            self.in_flight += 1
        try:
            yield
        finally:
            with self.cond:
                self.in_flight -= 1
                self.cond.notify_all()

    # --- tokens (requests) ------------------------------------------------------------------------
    def _refill(self, now: float) -> None:
        rate = self.limit / self.min_interval
        self.tokens = min(float(self.limit), self.tokens + (now - self.refilled) * rate)
        self.refilled = now

    def take_token(self) -> None:
        while True:
            with self.cond:
                now = self.clock()
                self._refill(now)
                wait = 0.0
                if now < self.blocked_until:
                    wait = self.blocked_until - now
                elif self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.requests += 1
                    return
                else:
                    wait = (1.0 - self.tokens) / (self.limit / self.min_interval)
            self.sleep(min(max(wait, 0.01), 5.0))

    # --- feedback ---------------------------------------------------------------------------------
    def _set(self, limit: int, reason: str, now: float) -> None:
        limit = max(1, min(limit, self.maximum, 1 if now < self.ceiling_until else self.maximum))
        if limit != self.limit:
            self.log(f"{self.host}: concurrency {self.limit} -> {limit} ({reason})")
            self.history.append((now, limit, reason))
        self.limit = limit
        self.peak = max(self.peak, limit)
        self.tokens = min(self.tokens, float(limit))
        self.last_change = now
        self.window, self.window_errors = [], 0
        self.cond.notify_all()

    def answered(self, latency: float) -> None:
        """A normal answer (any status that is not a throttle) with its latency."""
        with self.cond:
            now = self.clock()
            if self.baseline is None:
                self._first.append(latency)
                if len(self._first) >= BASELINE_SAMPLES:
                    self.baseline = max(MIN_BASELINE, _p95(self._first))
                    self.baseline_median = max(MIN_BASELINE / 2, _median(self._first))
                    self.window, self.window_errors = [], 0   # later windows judge against it
                return
            self.window.append(latency)
            self.recent.append(latency)
            if self.baseline is None:
                return
            spike = _p95(self.recent)
            stalled = self.lag() > LOCAL_LAG
            if stalled and len(self.recent) >= SPIKE_SAMPLES and spike > SPIKE_ABOVE * self.baseline:
                # The process itself stalled: the measured latency is ours, not the host's.
                self.local_stalls += 1
                self.recent.clear()
                self.window, self.window_errors = [], 0
                return
            if len(self.recent) >= SPIKE_SAMPLES and len(self.window) >= MIN_SAMPLES \
                    and spike > SPIKE_ABOVE * self.baseline and spike > self.baseline + SPIKE_MARGIN \
                    and _median(self.recent) > SPIKE_MEDIAN * self.baseline_median:
                self._throttle(now, f"latency spike p95 {_p95(self.recent):.2f}s > {SPIKE_ABOVE:.0f}x baseline "
                                    f"{self.baseline:.2f}s", retry_after=None, block=False)
                self.recent.clear()
                return
            if now - self.last_change >= INCREASE_EVERY and len(self.window) >= MIN_SAMPLES \
                    and not self.window_errors:
                # The baseline only ever tightens: the first answers of a run (TLS handshakes, every source
                # starting at once) are slow, and a high baseline would make both rules toothless.
                self.baseline = max(MIN_BASELINE, min(self.baseline, _p95(self.window)))
                self.baseline_median = max(MIN_BASELINE / 2, min(self.baseline_median, _median(self.window)))
            if (now - self.last_change >= INCREASE_EVERY and now >= self.cooldown_until and not stalled
                    and len(self.window) >= MIN_SAMPLES and not self.window_errors
                    and _p95(self.window) < GROW_BELOW * self.baseline and self.limit < self.maximum):
                self._set(self.limit + 1, f"p95 {_p95(self.window):.2f}s < {GROW_BELOW:.0f}x baseline "
                                          f"{self.baseline:.2f}s, no errors", now)

    def failed(self) -> None:
        """An error that is not a throttle (e.g. HTTP 404): it only blocks the next increase."""
        with self.cond:
            self.window_errors += 1

    def throttled(self, kind: str, retry_after: float | None = None) -> None:
        """429/503, a challenge page or a connection reset."""
        with self.cond:
            self._throttle(self.clock(), kind, retry_after=retry_after, block=True)

    def _throttle(self, now: float, reason: str, *, retry_after: float | None, block: bool) -> None:
        if retry_after:
            self.blocked_until = max(self.blocked_until, now + retry_after)
        self.cooldown_until = now + COOLDOWN
        if block:
            self.blocks.append(now)
            while self.blocks and now - self.blocks[0] > BLOCK_WINDOW:
                self.blocks.popleft()
            if len(self.blocks) >= BLOCKS_TO_ALERT:
                self.ceiling_until = now + ALERT_HOLD
                self.log(f"ALERT {self.host}: {len(self.blocks)} blocks within {BLOCK_WINDOW / 60:.0f} min "
                         f"({reason}): 1 request in flight for {ALERT_HOLD / 60:.0f} min")
                self._set(1, f"repeated blocks: {reason}", now)
                return
        self._set(self.limit // 2, f"{reason}" + (f", retry-after {retry_after:.0f}s" if retry_after else ""), now)

    def snapshot(self) -> dict:
        with self.cond:
            return {"limit": self.limit, "peak": self.peak, "in_flight": self.in_flight, "requests": self.requests,
                    "baseline_p95": round(self.baseline, 3) if self.baseline else None,
                    "local_stalls_ignored": self.local_stalls,
                    "recent_p95": round(_p95(self.recent), 3) if self.recent else None,
                    "blocks_10min": len(self.blocks)}


class Governors:
    """host -> HostGovernor, created on first use with the host's class (see ``concurrency_for``)."""

    def __init__(self, log=None, clock=time.monotonic, sleep=time.sleep, monitor_lag: bool = False):
        self.log, self.clock, self.sleep = log, clock, sleep
        self.lag_monitor = LagMonitor() if monitor_lag else None
        self._by_host: dict[str, HostGovernor] = {}
        self._policy: dict[str, tuple[Concurrency, float]] = {}
        self._lock = threading.Lock()

    def declare(self, host: str, concurrency: Concurrency, min_interval: float) -> None:
        with self._lock:
            known = self._policy.get(host)
            # Two sources on one host (be/bb …) share it; the stricter policy wins.
            if known is None or concurrency.maximum < known[0].maximum:
                self._policy[host] = (concurrency, max(min_interval, known[1] if known else 0.0))

    def get(self, host: str) -> HostGovernor:
        host = host.lower()
        with self._lock:
            governor = self._by_host.get(host)
            if governor is None:
                concurrency, interval = self._policy.get(host, (Concurrency(1, 2), 1.1))
                governor = HostGovernor(host, start=concurrency.start, maximum=concurrency.maximum,
                                        min_interval=interval, log=self.log, clock=self.clock, sleep=self.sleep,
                                        lag=self.lag_monitor.max_lag if self.lag_monitor else None)
                self._by_host[host] = governor
            return governor

    def snapshot(self) -> dict:
        with self._lock:
            hosts = dict(self._by_host)
        return {host: governor.snapshot() for host, governor in sorted(hosts.items())}
