"""Decoder boundaries and failure-safe automatic module configuration."""

from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from test_runtime import COMPONENT, MEASUREMENTS, MAC, checksum, protocol
from emax_test import control


class ExtendedDecoderTests(unittest.TestCase):
    def decode(self, offset, value):
        frame = bytearray(MEASUREMENTS[-1])
        frame[16 + offset:16 + offset + len(value)] = value
        return protocol.decode_measurement(checksum(frame))

    def test_extrema_are_independent_fields(self):
        m = self.decode(9, bytes.fromhex('c40432680664ffff00'))
        r = m.readings[0]
        self.assertEqual((r.temperature_c, r.humidity_pct), (0.0, 50))
        self.assertEqual((r.temperature_max_c, r.humidity_max_pct), (23.3, 100))
        self.assertEqual((r.temperature_min_c, r.humidity_min_pct), (None, 0))

    def test_no_percentage_from_bit_positions(self):
        for mask in range(16):
            m = self.decode(45, bytes([mask, 0]))
            self.assertEqual([m.battery_low(c) for c in range(4)],
                             [bool(mask & (1 << c)) for c in range(4)])

    def test_radio_pairs_and_unknown_battery(self):
        for value, expected in enumerate(['receiving', 'unknown', 'held', 'missing']):
            for channel in range(1, 4):
                m = self.decode(46, bytes([value << (2 * (channel - 1))]))
                self.assertEqual(m.radio_status(channel), expected)
                if value:
                    self.assertIsNone(m.battery_low(channel))
        absent = protocol.decode_measurement(MEASUREMENTS[0])
        self.assertIsNone(absent.battery_low(2))

    def test_clock_is_local_naive_and_invalid_does_not_discard_weather(self):
        m = self.decode(7, bytes([57]))
        self.assertTrue(m.station_time.endswith(':57'))
        m = self.decode(3, bytes([13]))
        self.assertIsNone(m.station_time)
        self.assertEqual(m.readings[1].temperature_c, 4.7)

    def test_pressure_and_forecast_sentinels(self):
        m = protocol.decode_measurement(MEASUREMENTS[-1])
        self.assertIsNone(m.pressure_hpa)
        self.assertIsNone(m.local_forecast_code)
        m = self.decode(47, bytes.fromhex('f50302'))
        self.assertEqual(m.pressure_hpa, 1013)
        self.assertEqual(m.local_forecast_code, 2)
        self.assertEqual(len(m.payload_hex), 100)


class AutomaticSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.info = control.StationInfo('192.168.1.2', MAC, 'HF-LPT230', '4.12.17',
                                       'UDP,Client,10000,app.emaxlife.net', 'NONE',
                                       'throughput', '9600,8,1,None,NFC')
        self.a = self.info.netp
        self.b = self.info.sockb
        self.writes = []
        self.restarts = 0
        self.ack_lost = False
        self.drop_write = False
        self.fail_restart = False

    def session(self, host, mac):
        owner = self
        class Session:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def query(self, name):
                return {'NETP': owner.a, 'SOCKB': owner.b, 'TCPLKB': 'on'}[name]
            def set_b(self, value):
                assert list(Path(owner.temp.name).glob('*_write_requested.json'))
                owner.writes.append(value)
                if not owner.drop_write: owner.b = value
                if owner.ack_lost: raise control.ControlError('ACK lost')
            def restart(self):
                assert list(Path(owner.temp.name).glob('*_restart_requested.json'))
                owner.restarts += 1
                if owner.fail_restart: raise control.ControlError('restart rejected')
        return Session()

    def run_setup(self, info=None, operation='test', **kwargs):
        return control.configure_station(info or self.info, '192.168.1.3', 10000,
                                         self.temp.name, operation, session_factory=self.session,
                                         settle_seconds=0, wait_seconds=0.01, **kwargs)

    def test_backup_precedes_write_and_restart_and_preserves_a(self):
        result = self.run_setup()
        self.assertEqual(result['result'], 'verified')
        self.assertEqual(self.writes, ['TCP,10000,192.168.1.3'])
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.a, self.info.netp)
        recovery = json.loads(Path(result['recovery_file']).read_text())
        self.assertEqual(recovery['original_sockb'], 'NONE')

    def test_lost_write_ack_readback_succeeds_without_repeated_write(self):
        self.ack_lost = True
        self.run_setup()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.restarts, 1)

    def test_failed_write_does_not_restart(self):
        self.drop_write = self.ack_lost = True
        with self.assertRaises(control.ControlError): self.run_setup()
        self.assertEqual(self.restarts, 0)

    def test_configuration_race_aborts_without_changes(self):
        self.a = 'UDP,Client,10000,other.example'
        with self.assertRaises(control.ConflictError): self.run_setup()
        self.assertFalse(self.writes)
        self.assertEqual(self.restarts, 0)

    def test_repeat_operation_id_cannot_restart_again(self):
        self.run_setup()
        with self.assertRaises(control.ControlError):
            self.run_setup(info=replace(self.info, sockb=self.b))
        self.assertEqual(self.restarts, 1)

    def test_restart_rejection_is_not_retried(self):
        self.fail_restart = True
        with self.assertRaises(control.ControlError): self.run_setup()
        self.assertEqual(self.restarts, 1)
        self.assertTrue(list(Path(self.temp.name).glob('*_unverified.json')))

    def test_restart_only_does_not_rewrite_b(self):
        self.run_setup(restart_only=True)
        self.assertFalse(self.writes)
        self.assertEqual(self.b, 'NONE')
        self.assertEqual(self.restarts, 1)

    def test_command_injection_and_destination_validation(self):
        for value in ['TCP,10000,example.com\rAT+Z', 'TCP,65535,example.com', 'X,10000,a']:
            with self.assertRaises(control.ControlError): control.socket_b(value)
        for value in ['0.0.0.0', '127.0.0.1', '255.255.255.255', '224.0.0.1']:
            with self.assertRaises(ValueError): control.ipv4(value)


class ControlUdpTests(unittest.TestCase):
    def test_real_udp_discovery_identity_queries_and_tainted_session(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(('127.0.0.1', 0))
        server.settimeout(0.1)
        done = threading.Event()
        commands = []
        def respond():
            while not done.is_set():
                try: data, address = server.recvfrom(4096)
                except socket.timeout: continue
                except OSError: return
                commands.append(data)
                if data == control.DISCOVERY:
                    server.sendto(f'127.0.0.1,{MAC},HF-LPT230'.encode(), address)
                elif data == b'AT+VER\r': server.sendto(b'+ok=4.12.17\r\n', address)
                elif data == b'AT+NETP\r': server.sendto(b'+ok=one\r\n+ok=two\r\n', address)
        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        try:
            with control.ControlSession('127.0.0.1', MAC, timeout=0.25, remote_port=server.getsockname()[1]) as session:
                self.assertEqual(session.query('VER'), '4.12.17')
                with self.assertRaises(control.ControlError): session.query('NETP')
                with self.assertRaises(control.ControlError): session.restart()
            with self.assertRaises(control.IdentityError):
                with control.ControlSession('127.0.0.1', '000000000000', timeout=0.25, remote_port=server.getsockname()[1]): pass
            self.assertNotIn(b'AT+Z\r', commands)
        finally:
            done.set()
            thread.join(1)
            server.close()


class AssetTests(unittest.TestCase):
    def test_translation_keys_match(self):
        def leaves(value, prefix=''):
            return {path for k, v in value.items() for path in
                    (leaves(v, prefix + '/' + k) if isinstance(v, dict) else [prefix + '/' + k])}
        base = json.loads((COMPONENT / 'strings.json').read_text(encoding='utf-8'))
        for lang in ['en', 'uk']:
            data = json.loads((COMPONENT / 'translations' / f'{lang}.json').read_text(encoding='utf-8'))
            self.assertEqual(leaves(base), leaves(data))
        self.assertNotIn('station_time', base['entity']['sensor'])

        self.assertNotIn('last_received', base['entity']['sensor'])
        self.assertEqual({p.stem for p in (COMPONENT / 'translations').glob('*.json')}, {'en', 'uk'})
