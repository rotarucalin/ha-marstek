"""Device-local read scheduling, API health and command-priority admission."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# BLE retains its existing cadence and unsupported-endpoint handling.
ENDPOINTS = {
    "es": ("ES.GetStatus", 30, 180),
    "es_mode": ("ES.GetMode", 60, 300),
    "battery": ("Bat.GetStatus", 60, 300),
    "em": ("EM.GetStatus", 60, 300),
    "pv": ("PV.GetStatus", 60, 300),
    "wifi": ("Wifi.GetStatus", 300, 900),
    "ble": ("BLE.GetStatus", 30, 300),
}
HEALTH_WINDOW = 120
HEALTH_FAILURE_ENDPOINTS = 3
HEALTH_RECOVERY_SUCCESSES = 3
HEALTH_RECOVERY_SECONDS = 60
HEALTH_PROBE_INTERVAL = 120
PASSIVE_MODE_INTERVAL = 300
ES_FRESH_SECONDS = 60


@dataclass
class SectionSchedule:
    """Completion-based deadlines; intentional skips never modify failures."""

    interval: float
    cap: float
    last_success: float | None = None
    next_poll: float = 0
    consecutive_failures: int = 0
    backoff_interval: float = 0
    diagnostic_state: tuple | None = None


class PollingPolicy:
    """Keep endpoint backoff independent of the global health circuit breaker."""

    def __init__(
        self,
        device: str,
        *,
        intervals: dict[str, float] | None = None,
        backoff_caps: dict[str, float] | None = None,
    ) -> None:
        self.device = device
        self.sections: dict[str, SectionSchedule] = {}
        for key, (_, interval, cap) in ENDPOINTS.items():
            interval = (intervals or {}).get(key, interval)
            cap = (backoff_caps or {}).get(key, cap)
            if interval <= 0 or cap < interval:
                raise ValueError(f"Invalid polling interval/backoff cap for {key}")
            self.sections[key] = SectionSchedule(interval, cap)
        self.normal_mode_interval = self.sections["es_mode"].interval
        self.degraded = False
        self.next_probe = 0.0
        self._failures: deque[tuple[float, str]] = deque()
        self._successes = 0
        self._success_since: float | None = None

    def require_mode(self, now: float) -> None:
        """Drop the normal mode deadline, but never bypass a failure backoff."""
        state = self.sections["es_mode"]
        if not state.consecutive_failures:
            state.next_poll = min(state.next_poll, now)

    def mode_interval(self, stable: bool, now: float) -> None:
        state = self.sections["es_mode"]
        interval = (
            max(PASSIVE_MODE_INTERVAL, self.normal_mode_interval)
            if stable
            else self.normal_mode_interval
        )
        if state.interval == interval:
            return
        state.interval = interval
        if not state.consecutive_failures and state.last_success is not None:
            state.next_poll = state.last_success + interval if stable else now

    def due(self, key: str, now: float, *, verification: bool = False) -> bool:
        state = self.sections[key]
        deadline = state.next_poll
        reason = "due"
        if verification and not state.consecutive_failures:
            deadline = now
        if self.degraded and not verification:
            if key != "es":
                reason = "API degraded; waiting for ES probe recovery"
                deadline = float("inf")
            else:
                deadline = max(deadline, self.next_probe)
        if now < deadline and reason == "due":
            reason = "endpoint backoff" if state.consecutive_failures else "interval"
            if self.degraded and deadline == self.next_probe:
                reason = "API probe interval"
        diagnostic = (
            state.interval,
            state.consecutive_failures,
            state.backoff_interval,
            self.degraded,
            deadline,
            reason,
        )
        if diagnostic != state.diagnostic_state:
            _LOGGER.debug(
                "Marstek poll: device=%s endpoint=%s normal_interval=%ss "
                "failure_count=%s backoff_interval=%ss API_health=%s "
                "next_eligible_poll=%s skip_reason=%s",
                self.device,
                ENDPOINTS[key][0],
                state.interval,
                state.consecutive_failures,
                state.backoff_interval,
                "degraded" if self.degraded else "healthy",
                deadline,
                reason,
            )
            state.diagnostic_state = diagnostic
        return now >= deadline

    def record(self, key: str, success: bool, now: float) -> None:
        state = self.sections[key]
        previous = state.consecutive_failures
        if success:
            state.last_success = now
            state.consecutive_failures = 0
            state.backoff_interval = 0
            state.next_poll = now + state.interval
        else:
            state.consecutive_failures += 1
            state.backoff_interval = min(
                state.cap, state.interval * 2 ** min(state.consecutive_failures, 16)
            )
            state.next_poll = now + state.backoff_interval
        if previous or not success:
            _LOGGER.debug(
                "Marstek endpoint backoff: device=%s endpoint=%s "
                "transition=%s failure_count=%s next_poll_delay=%ss",
                self.device,
                ENDPOINTS[key][0],
                "recovered" if success else "backoff",
                state.consecutive_failures,
                state.next_poll - now,
            )
        self.communication(key, success, now)
        if self.degraded and key == "es":
            self.next_probe = now + HEALTH_PROBE_INTERVAL

    def communication(self, endpoint: str, success: bool, now: float) -> None:
        """Require three distinct failing reads; recover on sustained successes."""
        while self._failures and self._failures[0][0] < now - HEALTH_WINDOW:
            self._failures.popleft()
        if success:
            if not self.degraded:
                return
            if self._success_since is None:
                self._success_since = now
            self._successes += 1
            if (
                self._successes >= HEALTH_RECOVERY_SUCCESSES
                and now - self._success_since >= HEALTH_RECOVERY_SECONDS
            ):
                _LOGGER.debug(
                    "Marstek API health: degraded -> healthy device=%s "
                    "reason=sustained successful communication successes=%s duration=%ss",
                    self.device,
                    self._successes,
                    now - self._success_since,
                )
                self.degraded = False
                self._failures.clear()
            return
        self._successes = 0
        self._success_since = None
        if endpoint == "ES.SetMode":
            return  # Failed commands alone do not open the read circuit breaker.
        self._failures.append((now, endpoint))
        failures = len({key for _, key in self._failures})
        if not self.degraded and failures >= HEALTH_FAILURE_ENDPOINTS:
            self.degraded = True
            self.next_probe = now + HEALTH_PROBE_INTERVAL
            _LOGGER.debug(
                "Marstek API health: healthy -> degraded device=%s "
                "reason=multiple failing endpoints endpoints=%s failures=%s window=%ss",
                self.device,
                failures,
                len(self._failures),
                HEALTH_WINDOW,
            )


class CommandPriorityGate:
    """Admit commands before queued reads without interrupting a UDP exchange."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = False
        self.pending_commands = 0
        self.pending_controls = 0

    @asynccontextmanager
    async def control(self):
        """Reserve priority even while a control producer waits for its lock."""
        async with self._condition:
            self.pending_controls += 1
        try:
            yield
        finally:
            async with self._condition:
                self.pending_controls -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def slot(self, *, command: bool = False):
        async with self._condition:
            if command:
                self.pending_commands += 1
            try:
                await self._condition.wait_for(
                    lambda: (
                        not self._active
                        and (
                            command
                            or not (self.pending_commands or self.pending_controls)
                        )
                    )
                )
                self._active = True
            finally:
                if command:
                    self.pending_commands -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._active = False
                self._condition.notify_all()
