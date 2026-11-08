"""Full measurement diagnostics; transport identity and destinations omitted."""

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import EmaxConfigEntry
from .const import STALE_AFTER_SECONDS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EmaxConfigEntry
) -> dict[str, Any]:
    """Expose bounded counters to distinguish connectivity from valid readings."""
    hub = entry.runtime_data
    receiver = hub.receiver
    return {
        "listener": {
            "listening": receiver.listening,
            "port": receiver.listen_port,
            "active_connections": receiver.active_connections,
            "connections_accepted": receiver.connections_accepted,
            "connections_rejected": receiver.connections_rejected,
        },
        "traffic": {
            "frame_types": dict(receiver.frame_types),
            "bytes_received": receiver.bytes_received,
            "valid_frames": receiver.frames_received,
            "accepted_measurements": receiver.measurements_received,
            "foreign_mac_frames": receiver.foreign_mac_frames,
            "invalid_candidates": receiver.invalid_candidates,
            "discarded_bytes": receiver.discarded_bytes,
            "last_activity_utc": (
                receiver.last_activity_utc.isoformat()
                if receiver.last_activity_utc else None
            ),
        },
        "measurement": {
            "fresh": hub.fresh,
            "age_seconds": hub.measurement_age_seconds,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "last_received_utc": hub.received_utc.isoformat() if hub.received_utc else None,
            "present_fields": [
                {"temperature": r.temperature_c is not None,
                 "humidity": r.humidity_pct is not None}
                for r in hub.measurement.readings
            ] if hub.measurement else [],
        },
        "decoded_upload": {
            key: value for key, value in asdict(hub.measurement).items() if key != "mac"
        } if hub.measurement else None,
        "control": {
            "status": hub.setup_status,
            "result": hub.last_control_result.get("result"),
            "tcp_link": hub.last_control_result.get("tcp_link"),
            "firmware": hub.last_control_result.get("firmware"),
            "recovery_directory": "emax_local_backups",
            # Error text can contain hostnames and local file paths. Logs retain
            # details; downloaded diagnostics report only whether an error exists.
            "has_error": hub.control_error is not None,
        },
    }
