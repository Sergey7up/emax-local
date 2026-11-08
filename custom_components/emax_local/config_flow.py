"""Native HA wizard: station IP -> discovered connection -> automatic setup."""

from dataclasses import asdict
import socket
from uuid import uuid4

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME

from .const import (
    CONF_EXPECTED_MAC, CONF_LISTEN_PORT, CONF_STATION_IP, CONF_RECEIVER_IP,
    CONF_SETUP_REQUEST, DEFAULT_LISTEN_PORT, DEFAULT_NAME, DOMAIN,
)
from .control import ControlError, IdentityError, inspect_station, ipv4, normalize


def _can_bind(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


def _is_local_address(address: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


class EmaxConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._info = None
        self._entry = None
        self._receiver_ip = ""
        self._port = DEFAULT_LISTEN_PORT
        self._name = DEFAULT_NAME

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return await self._address_step("user", user_input)

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        self._entry = self._get_reconfigure_entry()
        hub = getattr(self._entry, "runtime_data", None)
        if hub and hub.setup_status == "configuring":
            return self.async_abort(reason="setup_in_progress")
        self._port = self._entry.data[CONF_LISTEN_PORT]
        self._name = self._entry.data[CONF_NAME]
        return await self._address_step("reconfigure", user_input)

    async def _address_step(self, step_id, user_input):
        errors = {}
        if user_input is not None:
            try:
                host = ipv4(user_input[CONF_STATION_IP])
            except (ValueError, TypeError, AttributeError, KeyError):
                errors[CONF_STATION_IP] = "invalid_ipv4"
            else:
                try:
                    expected = self._entry.data[CONF_EXPECTED_MAC] if self._entry else None
                    self._info = await self.hass.async_add_executor_job(inspect_station, host, expected)
                    await self.async_set_unique_id(self._info.mac)
                    if self._entry is None:
                        self._abort_if_unique_id_configured()
                    self._receiver_ip = ipv4(await async_get_source_ip(self.hass, target_ip=host))
                    if self._receiver_ip == host:
                        raise ValueError("Station cannot be the receiver")
                    if self._entry is None:
                        for candidate in range(DEFAULT_LISTEN_PORT, DEFAULT_LISTEN_PORT + 100):
                            if await self._port_error(candidate) is None:
                                self._port = candidate
                                break
                        else:
                            errors["base"] = "port_unavailable"
                    if not errors:
                        return await self.async_step_confirm()
                except IdentityError:
                    errors["base"] = "wrong_device"
                except (ControlError, OSError):
                    errors["base"] = "cannot_connect"
                except ValueError:
                    errors["base"] = "no_receiver_ip"
        default = self._entry.data[CONF_STATION_IP] if self._entry else ""
        if user_input:
            default = user_input.get(CONF_STATION_IP, default)
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({
            vol.Required(CONF_STATION_IP, default=default): str,
        }), errors=errors)

    async def _port_error(self, port):
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data[CONF_LISTEN_PORT] == port:
                if self._entry and entry.entry_id == self._entry.entry_id:
                    return None
                return "port_already_configured"
        if not await self.hass.async_add_executor_job(_can_bind, port):
            return "port_unavailable"
        return None

    async def async_step_confirm(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        defaults = {CONF_NAME: self._name, CONF_RECEIVER_IP: self._receiver_ip,
                    CONF_LISTEN_PORT: self._port}
        defaults.update(user_input or {})
        expected = f"TCP,{self._port},{self._receiver_ip}"
        occupied = normalize(self._info.sockb) not in {"NONE", normalize(expected)}
        if user_input is not None:
            try:
                receiver_ip = ipv4(user_input[CONF_RECEIVER_IP])
                if receiver_ip == self._info.host:
                    raise ValueError
                if not await self.hass.async_add_executor_job(_is_local_address, receiver_ip):
                    errors[CONF_RECEIVER_IP] = "not_local_address"
            except (ValueError, TypeError, AttributeError, KeyError):
                errors[CONF_RECEIVER_IP] = "invalid_ipv4"
            port = user_input.get(CONF_LISTEN_PORT)
            if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65534:
                errors[CONF_LISTEN_PORT] = "invalid_port"
            elif reason := await self._port_error(port):
                errors[CONF_LISTEN_PORT] = reason
            name = str(user_input.get(CONF_NAME, "")).strip()
            if not 1 <= len(name) <= 64:
                errors[CONF_NAME] = "invalid_name"
            if occupied and not user_input.get("replace_existing"):
                errors["replace_existing"] = "replacement_required"
            if not errors:
                data = dict(self._entry.data) if self._entry else {}
                data.update({CONF_NAME: name, CONF_STATION_IP: self._info.host,
                             CONF_EXPECTED_MAC: self._info.mac, CONF_LISTEN_PORT: port,
                             CONF_RECEIVER_IP: receiver_ip, "firmware": self._info.firmware,
                             CONF_SETUP_REQUEST: {"id": uuid4().hex, "state": "pending",
                                                  "station": asdict(self._info)}})
                if self._entry:
                    return self.async_update_reload_and_abort(self._entry, title=name, data=data)
                return self.async_create_entry(title=name, data=data)
        fields = {vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                  vol.Required(CONF_RECEIVER_IP, default=defaults[CONF_RECEIVER_IP]): str,
                  vol.Required(CONF_LISTEN_PORT, default=defaults[CONF_LISTEN_PORT]): int}
        if occupied:
            fields[vol.Required("replace_existing", default=False)] = bool
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"station_ip": self._info.host, "mac": self._info.mac,
                                      "firmware": self._info.firmware, "sockb": self._info.sockb,
                                      "receiver_ip": self._receiver_ip, "port": str(self._port)},
        )
