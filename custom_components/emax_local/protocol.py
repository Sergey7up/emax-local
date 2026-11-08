"""Decode the observed EMAX W0003 v01 sensor upload.

This module uses only the standard library and never constructs replies.
The temperature encoding and offsets were checked against captured W0003 packets.
Unknown message types, firmware layouts, and status flags are not interpreted.
"""

from dataclasses import dataclass
from datetime import datetime

MAGIC = b"\xaa\x3c\x57"
TRAILER = b"\xcc\x3e"
MEASUREMENT_TYPE = b"\x53\x30\x01\x00"
MAX_PAYLOAD_SIZE = 8192
READ_SIZE = 4096


@dataclass(frozen=True, slots=True)
class Reading:
    """Independent current temperature and relative humidity values."""

    temperature_c: float | None
    humidity_pct: int | None
    temperature_max_c: float | None = None
    humidity_max_pct: int | None = None
    temperature_min_c: float | None = None
    humidity_min_pct: int | None = None


@dataclass(frozen=True, slots=True)
class Measurement:
    """Current values in indoor, CH1, CH2, CH3 order."""

    mac: str
    readings: tuple[Reading, Reading, Reading, Reading]
    station_time: str | None = None
    payload_version: int = 0
    timezone_code: int = 0
    weather_refresh_code: int = 0
    power_status: int = 0
    wireless_status: int = 0
    pressure_hpa: int | None = None
    local_forecast_code: int | None = None
    payload_hex: str = ""

    def radio_status(self, channel: int) -> str:
        """Keep undocumented pair 01 distinct; do not assume it is live."""
        if channel == 0:
            return "wired"
        pair = (self.wireless_status >> (2 * (channel - 1))) & 3
        return {0: "receiving", 1: "unknown", 2: "held", 3: "missing"}[pair]

    def battery_low(self, channel: int) -> bool | None:
        """EM3371 per-channel bit mask, matching W0003 cloud power bit values.

        Bit zero is also confirmed for the EM3378D console backup batteries.
        This is not a four-level percentage. See docs/protocol.md for the
        related-model disagreement and limits of the W0003 verification.
        """
        reading = self.readings[channel]
        if channel and (self.radio_status(channel) != "receiving" or
                        (reading.temperature_c is None and reading.humidity_pct is None)):
            return None
        return bool(self.power_status & (1 << channel))


def decode_measurement(frame: bytes) -> Measurement | None:
    """Independently validate and decode exactly the observed 50-byte layout."""
    if (
        len(frame) != 70
        or frame[:4] != MAGIC + b"\x01"
        or frame[10:14] != MEASUREMENT_TYPE
        or frame[14:16] != b"\x32\x00"
        or frame[16] != 1
        or frame[-2:] != TRAILER
        or int.from_bytes(frame[-4:-2], "little") != (sum(frame[:-4]) & 0xFFFF)
    ):
        return None

    payload = frame[16:-4]

    def pair(offset: int) -> tuple[float | None, int | None]:
        temperature = int.from_bytes(payload[offset:offset + 2], "little")
        humidity = payload[offset + 2]
        return (None if temperature == 0xFFFF else round((temperature - 1220) / 18, 1),
                humidity if humidity <= 100 else None)

    def reading(offset: int) -> Reading:
        return Reading(*pair(offset), *pair(offset + 3), *pair(offset + 6))

    try:
        # v01 carries seconds directly. No timezone is invented from byte 1.
        station_time = datetime(2000 + payload[2], *payload[3:8]).isoformat(sep=" ")
    except ValueError:
        station_time = None
    pressure = int.from_bytes(payload[47:49], "little")

    return Measurement(
        mac=frame[4:10].hex().upper(),
        readings=(reading(9), reading(18), reading(27), reading(36)),
        station_time=station_time,
        payload_version=payload[0],
        timezone_code=payload[1],
        weather_refresh_code=payload[8],
        power_status=payload[45],
        wireless_status=payload[46],
        pressure_hpa=None if pressure == 0xFFFF else pressure,
        local_forecast_code=None if payload[49] == 0xFF else payload[49],
        payload_hex=payload.hex(),
    )


class FrameStream:
    """Reassemble checksummed frames independently for one TCP connection.

    A read may contain a fragment, several frames, or unrelated bytes. A partial
    frame retains at most MAX_PAYLOAD_SIZE + 19 bytes between calls. Processing
    large input in chunks also bounds transient buffer allocation.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.invalid_candidates = 0
        self.discarded_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes awaiting a complete frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Return complete validated frames, preserving a possible fragment."""
        frames: list[bytes] = []
        for offset in range(0, len(data), READ_SIZE):
            self._buffer.extend(data[offset : offset + READ_SIZE])
            frames.extend(self._extract())
        return frames

    def _extract(self) -> list[bytes]:
        frames: list[bytes] = []
        while self._buffer:
            start = self._buffer.find(MAGIC)
            if start < 0:
                # The final two bytes may be the beginning of the next marker.
                discarded = max(0, len(self._buffer) - 2)
                self.discarded_bytes += discarded
                del self._buffer[:discarded]
                break
            if start:
                self.discarded_bytes += start
                del self._buffer[:start]
            if len(self._buffer) < 16:
                break

            payload_size = int.from_bytes(self._buffer[14:16], "little")
            if payload_size > MAX_PAYLOAD_SIZE:
                self.invalid_candidates += 1
                self.discarded_bytes += 1
                del self._buffer[0]
                continue
            frame_size = 20 + payload_size
            if len(self._buffer) < frame_size:
                break

            frame = bytes(self._buffer[:frame_size])
            if (
                frame[-2:] != TRAILER
                or int.from_bytes(frame[-4:-2], "little")
                != (sum(frame[:-4]) & 0xFFFF)
            ):
                self.invalid_candidates += 1
                self.discarded_bytes += 1
                del self._buffer[0]
                continue
            frames.append(frame)
            del self._buffer[:frame_size]
        return frames
