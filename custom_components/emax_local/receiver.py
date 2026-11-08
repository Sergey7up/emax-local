"""Bounded, receive-only TCP server for the station's Socket B."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
import logging

from .const import CLIENT_IDLE_SECONDS, MAX_CLIENTS
from .protocol import READ_SIZE, FrameStream, Measurement, decode_measurement

_LOGGER = logging.getLogger(__name__)


class PassiveReceiver:
    """Receive copied UART uploads without transmitting application data."""

    def __init__(
        self,
        station_ip: str,
        expected_mac: str,
        listen_port: int,
        on_measurement: Callable[[Measurement], None],
    ) -> None:
        self.station_ip = station_ip
        self.expected_mac = expected_mac
        self.listen_port = listen_port
        self._on_measurement = on_measurement
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._stopping = False
        self.connections_accepted = 0
        self.connections_rejected = 0
        self.bytes_received = 0
        self.frames_received = 0
        self.measurements_received = 0
        self.foreign_mac_frames = 0
        self.invalid_candidates = 0
        self.discarded_bytes = 0
        self.last_activity_utc: datetime | None = None
        self.frame_types: dict[str, int] = {}

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def active_connections(self) -> int:
        return len(self._clients)

    async def async_start(self) -> None:
        """Bind the local listener; do not contact or configure the station."""
        if self._server is not None:
            raise RuntimeError("EMAX receiver is already started")
        self._stopping = False
        self._server = await asyncio.start_server(
            self._accept,
            host="0.0.0.0",
            port=self.listen_port,
            limit=2 * READ_SIZE,
            backlog=MAX_CLIENTS,
        )
        _LOGGER.info(
            "Listening for passive EMAX uploads from %s on TCP port %s",
            self.station_ip,
            self.listen_port,
        )

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if (
            self._stopping
            or not peer
            or peer[0] != self.station_ip
            or len(self._clients) >= MAX_CLIENTS
        ):
            self.connections_rejected += 1
            writer.close()
            return

        # The real station opened an empty connection before its data connection.
        # Each accepted connection needs its own parser and independent lifetime.
        self.connections_accepted += 1
        task = asyncio.create_task(
            self._handle_client(reader, writer), name="emax_local_tcp_client"
        )
        self._clients[task] = writer
        task.add_done_callback(self._client_done)

    def _client_done(self, task: asyncio.Task[None]) -> None:
        self._clients.pop(task, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "EMAX TCP receiver failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        parser = FrameStream()
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Accepted EMAX TCP connection from %s", peer)
        try:
            while not self._stopping:
                # Clean up abandoned sockets while allowing the observed empty
                # connection to coexist with the actual data connection.
                async with asyncio.timeout(CLIENT_IDLE_SECONDS):
                    data = await reader.read(READ_SIZE)
                if not data:
                    break
                self.bytes_received += len(data)
                self.last_activity_utc = datetime.now(timezone.utc)
                old_invalid = parser.invalid_candidates
                old_discarded = parser.discarded_bytes
                for frame in parser.feed(data):
                    self.frames_received += 1
                    if frame[4:10].hex().upper() != self.expected_mac:
                        self.foreign_mac_frames += 1
                        continue
                    kind = frame[10:14].hex()
                    # Bounded even if a device sends arbitrary service types.
                    if kind not in self.frame_types and len(self.frame_types) >= 32:
                        kind = "other"
                    self.frame_types[kind] = self.frame_types.get(kind, 0) + 1
                    measurement = decode_measurement(frame)
                    if measurement is not None:
                        self.measurements_received += 1
                        self._on_measurement(measurement)
                self.invalid_candidates += parser.invalid_candidates - old_invalid
                self.discarded_bytes += parser.discarded_bytes - old_discarded
        except (TimeoutError, ConnectionError, OSError):
            _LOGGER.debug("EMAX TCP connection ended: %s", peer)
        finally:
            writer.close()
            with suppress(TimeoutError, ConnectionError, OSError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()

    async def async_stop(self) -> None:
        """Release the listener and all clients so HA can reload immediately."""
        self._stopping = True
        server, self._server = self._server, None
        if server is not None:
            server.close()

        clients = tuple(self._clients.items())
        for task, writer in clients:
            writer.close()
            task.cancel()
        if clients:
            await asyncio.gather(*(task for task, _ in clients), return_exceptions=True)
        self._clients.clear()
        if server is not None:
            await server.wait_closed()
