"""Run with Python 3.12+: python -m unittest discover -s tests -v.

Protocol and receiver tests use real local sockets. Hub tests supply only HA's
clock and callback decorator, not a simulated HA config-entry implementation.
Actual HA loading/reconfiguration is verified separately on the installed host.
"""

import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import random
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "emax_local"
package = types.ModuleType("emax_test")
package.__path__ = [str(COMPONENT)]
sys.modules[package.__name__] = package

from emax_test import protocol, receiver  # noqa: E402

FRAMES = [bytes.fromhex(h) for h in json.loads(
    (ROOT / "tests/fixtures/synthetic_frames.json").read_text())]
MEASUREMENTS = [f for f in FRAMES if protocol.decode_measurement(f)]
MAC = "020000000001"


def checksum(frame):
    frame = bytearray(frame)
    frame[-4:-2] = (sum(frame[:-4]) & 0xFFFF).to_bytes(2, "little")
    return bytes(frame)


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class ProtocolTests(unittest.TestCase):
    def test_synthetic_stream(self):
        parser = protocol.FrameStream()
        self.assertEqual(parser.feed(b"".join(FRAMES)), FRAMES)
        self.assertEqual(len(FRAMES), 32)
        self.assertEqual(len(MEASUREMENTS), 3)
        final = protocol.decode_measurement(MEASUREMENTS[-1])
        self.assertEqual([(r.temperature_c, r.humidity_pct) for r in final.readings],
                         [(23.1, 31), (4.7, 66), (11.6, 43), (19.9, 37)])

    def test_missing_channel_is_not_zero_or_previous(self):
        first = protocol.decode_measurement(MEASUREMENTS[0]).readings[2]
        self.assertIsNone(first.temperature_c)
        self.assertIsNone(first.humidity_pct)
        self.assertEqual(protocol.decode_measurement(MEASUREMENTS[1]).readings[2].humidity_pct, 43)

    def test_every_possible_split(self):
        data = b"".join(FRAMES)
        for cut in range(len(data) + 1):
            p = protocol.FrameStream()
            self.assertEqual(p.feed(data[:cut]) + p.feed(data[cut:]), FRAMES)

    def test_single_byte_chunks(self):
        p = protocol.FrameStream()
        actual = [f for byte in b"".join(FRAMES) for f in p.feed(bytes([byte]))]
        self.assertEqual(actual, FRAMES)

    def test_corrupt_checksum_and_trailer_resynchronize(self):
        bad = bytearray(MEASUREMENTS[-1])
        bad[-4] ^= 1
        trailer = bytearray(MEASUREMENTS[-1])
        trailer[-1] ^= 1
        p = protocol.FrameStream()
        self.assertEqual(p.feed(b"noise" + bad + trailer + MEASUREMENTS[-1]), [MEASUREMENTS[-1]])
        self.assertEqual(p.invalid_candidates, 2)
        self.assertGreater(p.discarded_bytes, 100)

    def test_full_checksum_including_high_byte(self):
        f = bytearray(MEASUREMENTS[-1])
        f[-3] ^= 1
        self.assertIsNone(protocol.decode_measurement(f))

    def test_unsupported_versions_types_and_lengths(self):
        for position in (3, 10, 12, 14, 16):
            f = bytearray(MEASUREMENTS[-1])
            f[position] ^= 1
            self.assertIsNone(protocol.decode_measurement(checksum(f)))
        self.assertIsNone(protocol.decode_measurement(MEASUREMENTS[-1][:-1]))

    def test_invalid_humidity_is_independent(self):
        f = bytearray(MEASUREMENTS[-1])
        f[16 + 9 + 2] = 101
        r = protocol.decode_measurement(checksum(f)).readings[0]
        self.assertEqual(r.temperature_c, 23.1)
        self.assertIsNone(r.humidity_pct)

    def test_negative_temperature(self):
        f = bytearray(MEASUREMENTS[-1])
        f[25:27] = (1040).to_bytes(2, "little")
        self.assertEqual(protocol.decode_measurement(checksum(f)).readings[0].temperature_c, -10.0)

    def test_bounded_noise_and_oversized_length(self):
        p = protocol.FrameStream()
        p.feed(random.Random(42).randbytes(1_000_000))
        self.assertLessEqual(p.buffered_bytes, protocol.MAX_PAYLOAD_SIZE + 19)
        bad = bytearray(MEASUREMENTS[-1][:16])
        bad[14:16] = b"\xff\xff"
        p = protocol.FrameStream()
        self.assertEqual(p.feed(bad + MEASUREMENTS[-1]), [MEASUREMENTS[-1]])


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.writers = []
        self.server = receiver.PassiveReceiver("127.0.0.1", MAC, 0, self.received.append)
        await self.server.async_start()
        self.port = self.server._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.async_stop()
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        return reader, writer

    async def test_empty_first_connection_and_passive_receiver(self):
        await self.connect()
        reader, writer = await self.connect()
        writer.write(b"".join(FRAMES))
        await writer.drain()
        await until(lambda: len(self.received) == 3)
        self.assertEqual(self.server.active_connections, 2)
        self.assertEqual(self.server.frames_received, 32)
        self.assertEqual(self.server.measurements_received, 3)
        self.assertEqual(self.server.bytes_received, 790)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.05):
                await reader.read(1)

    async def test_foreign_mac_and_service_packets_do_not_update(self):
        _, writer = await self.connect()
        foreign = bytearray(MEASUREMENTS[-1])
        foreign[4] ^= 1
        writer.write(FRAMES[0] + checksum(foreign))
        await writer.drain()
        await until(lambda: self.server.frames_received == 2)
        self.assertEqual(self.received, [])
        self.assertEqual(self.server.foreign_mac_frames, 1)

    async def test_rejected_source_ip(self):
        self.server.station_ip = "127.0.0.2"
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        self.assertEqual(self.server.connections_rejected, 1)

    async def test_reconnect_discards_incomplete_frame(self):
        _, writer = await self.connect()
        writer.write(MEASUREMENTS[-1][:30])
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        _, writer = await self.connect()
        writer.write(MEASUREMENTS[-1])
        await writer.drain()
        await until(lambda: len(self.received) == 1)

    async def test_client_limit(self):
        with patch.object(receiver, "MAX_CLIENTS", 2):
            await self.connect()
            await self.connect()
            reader, _ = await self.connect()
            self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
            self.assertEqual(self.server.active_connections, 2)

    async def test_idle_client_is_closed(self):
        with patch.object(receiver, "CLIENT_IDLE_SECONDS", 0.03):
            reader, _ = await self.connect()
            self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")

    async def test_stop_closes_clients_and_releases_port(self):
        reader, _ = await self.connect()
        await self.server.async_stop()
        await self.server.async_stop()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        other = receiver.PassiveReceiver("127.0.0.1", MAC, self.port, self.received.append)
        await other.async_start()
        self.assertTrue(other.listening)
        await other.async_stop()

    async def test_double_start_cannot_leak_listener(self):
        with self.assertRaises(RuntimeError):
            await self.server.async_start()
        self.assertTrue(self.server.listening)


# Hub alone depends on these three HA symbols; actual HA is tested after deploy.
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda function: function
dt_module = types.ModuleType("homeassistant.util.dt")
dt_module.utcnow = lambda: datetime.now(timezone.utc)
util = types.ModuleType("homeassistant.util")
util.dt = dt_module
with patch.dict(sys.modules, {"homeassistant": types.ModuleType("homeassistant"),
                             "homeassistant.core": core, "homeassistant.util": util,
                             "homeassistant.util.dt": dt_module}):
    from emax_test import hub


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_recovery_unsubscribe_and_stop(self):
        state = hub.EmaxHub(types.SimpleNamespace(loop=asyncio.get_running_loop()),
                            "127.0.0.1", MAC, 0, "Test")
        events = []
        unsubscribe = state.async_subscribe(lambda: events.append(state.fresh))
        self.assertFalse(state.fresh)
        with patch.object(hub, "STALE_AFTER_SECONDS", 0.04):
            state._receive(protocol.decode_measurement(MEASUREMENTS[-1]))
            received_at = state.received_utc
            self.assertTrue(state.fresh)
            await until(lambda: events[-1] is False)
            self.assertEqual(state.received_utc, received_at)
            state._receive(protocol.decode_measurement(MEASUREMENTS[0]))
            self.assertTrue(state.fresh)
            self.assertIsNone(state.measurement.readings[2].temperature_c)
            unsubscribe()
            count = len(events)
            await state.async_stop()
            await state.async_stop()
            self.assertIsNone(state._expiry_timer)
            self.assertFalse(state.fresh)
            state._receive(protocol.decode_measurement(MEASUREMENTS[-1]))
            self.assertEqual(len(events), count)


if __name__ == "__main__":
    unittest.main()
