"""Home Assistant runtime state for one passive EMAX listener."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from functools import partial
import logging
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import STALE_AFTER_SECONDS, CONF_SETUP_REQUEST, CONF_LAST_SETUP, CONF_RECEIVER_IP
from .control import StationInfo, ControlError, configure_station, inspect_station
from .protocol import Measurement
from .receiver import PassiveReceiver

_LOGGER = logging.getLogger(__name__)


class EmaxHub:
    """Keep live readings and expire them independently of service traffic."""

    def __init__(
        self,
        hass: HomeAssistant,
        station_ip: str,
        expected_mac: str,
        listen_port: int,
        name: str,
    ) -> None:
        self.hass = hass
        self.station_ip = station_ip
        self.mac = expected_mac
        self.name = name
        self.measurement: Measurement | None = None
        self.received_utc: datetime | None = None
        self._received_monotonic: float | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._subscribers: set[Callable[[], None]] = set()
        self._stopped = False
        self._reported_stale = False
        self.setup_status = "waiting_for_data"
        self.control_error: str | None = None
        self.last_control_result: dict = {}
        self._control_lock = asyncio.Lock()
        self.receiver = PassiveReceiver(
            station_ip, expected_mac, listen_port, self._receive
        )

    @property
    def fresh(self) -> bool:
        """Check elapsed monotonic time, including a delayed timer callback."""
        return (
            not self._stopped
            and self._received_monotonic is not None
            and self.hass.loop.time() - self._received_monotonic < STALE_AFTER_SECONDS
        )

    async def async_start(self) -> None:
        """Start receiving without waiting for a sensor upload."""
        self._stopped = False
        await self.receiver.async_start()

    @callback
    def _receive(self, measurement: Measurement) -> None:
        if self._stopped:
            return
        if self.received_utc is None:
            _LOGGER.info("First EMAX measurement received")
        elif self._reported_stale:
            _LOGGER.info("EMAX measurements resumed")
        self._reported_stale = False
        self.measurement = measurement
        if self.setup_status == "waiting_for_data":
            self.setup_status = "ready"
        self.received_utc = dt_util.utcnow()
        self._received_monotonic = self.hass.loop.time()
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
        self._expiry_timer = self.hass.loop.call_later(
            STALE_AFTER_SECONDS, self._expire
        )
        self._notify()

    @callback
    def _expire(self) -> None:
        self._expiry_timer = None
        # Asyncio may run a timer slightly before its deadline. Reschedule the
        # remainder instead of leaving entities available indefinitely.
        if self._received_monotonic is not None:
            remaining = (
                STALE_AFTER_SECONDS
                - (self.hass.loop.time() - self._received_monotonic)
            )
            if remaining > 0 and not self._stopped:
                self._expiry_timer = self.hass.loop.call_later(remaining, self._expire)
                return
        if not self._stopped and not self._reported_stale:
            self._reported_stale = True
            if self.setup_status == "ready":
                self.setup_status = "waiting_for_data"
            _LOGGER.warning(
                "No EMAX measurement for %s seconds; readings are unavailable",
                STALE_AFTER_SECONDS,
            )
        self._notify()

    @property
    def measurement_age_seconds(self) -> float | None:
        """Elapsed time independent of wall-clock adjustments."""
        if self._received_monotonic is None:
            return None
        return max(0.0, self.hass.loop.time() - self._received_monotonic)

    @callback
    def async_subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity callback and return its removal callback."""
        self._subscribers.add(listener)

        @callback
        def unsubscribe() -> None:
            self._subscribers.discard(listener)

        return unsubscribe

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._subscribers):
            listener()

    async def async_stop(self) -> None:
        """Cancel freshness tracking and release every TCP resource."""
        if self._stopped:
            return
        self._stopped = True
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        await self.receiver.async_stop()
        self._notify()

    async def async_configure(self, entry, request: dict, *, restart_only=False) -> None:
        """Run an explicitly queued, durable, single-use management operation."""
        async with self._control_lock:
            self.setup_status = "configuring"
            self.control_error = None
            running = {**request, "state": "in_progress"}
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_SETUP_REQUEST: running})
            self._notify()
            try:
                info = StationInfo(**request["station"])
                result = await self.hass.async_add_executor_job(partial(
                    configure_station, info, entry.data[CONF_RECEIVER_IP],
                    self.receiver.listen_port, self.hass.config.path("emax_local_backups"),
                    request["id"], restart_only=restart_only,
                ))
            except asyncio.CancelledError:
                # The bounded executor operation may still finish. Its recovery
                # markers survive; startup never automatically replays it.
                self.setup_status = "interrupted"
                raise
            except (ControlError, OSError, ValueError) as error:
                self.control_error = str(error)[:300]
                self.setup_status = "error"
                result = {"result": "unverified", "error": self.control_error}
                _LOGGER.error("EMAX automatic setup unverified: %s", self.control_error)
            else:
                self.setup_status = "ready" if self.fresh else "waiting_for_data"
            self.last_control_result = result
            self.hass.config_entries.async_update_entry(entry, data={
                **entry.data, CONF_SETUP_REQUEST: {**running, "state": "completed"},
                CONF_LAST_SETUP: result,
            })
            self._notify()

    async def async_restart_module(self, entry) -> None:
        """Restart the Wi-Fi module on an explicit button press, preserving A/B."""
        from dataclasses import asdict
        from homeassistant.components.network import async_get_source_ip
        from homeassistant.exceptions import HomeAssistantError

        if self._control_lock.locked() or self.setup_status == "configuring":
            raise HomeAssistantError("EMAX setup is already running")
        self.setup_status = "configuring"
        self._notify()
        try:
            info = await self.hass.async_add_executor_job(inspect_station, self.station_ip, self.mac)
            if CONF_RECEIVER_IP not in entry.data:
                address = await async_get_source_ip(self.hass, target_ip=self.station_ip)
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_RECEIVER_IP: address})
            await self.async_configure(entry, {"id": uuid4().hex, "station": asdict(info)}, restart_only=True)
        except (ControlError, OSError, ValueError) as error:
            self.control_error = str(error)[:300]
            self.setup_status = "error"
            self._notify()
            raise HomeAssistantError(self.control_error) from error
        if self.control_error:
            raise HomeAssistantError(self.control_error)
