"""Receive EMAX W0003 Socket B data locally without changing Socket A."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import CONF_EXPECTED_MAC, CONF_LISTEN_PORT, CONF_STATION_IP, DOMAIN, CONF_SETUP_REQUEST, CONF_LAST_SETUP
from .hub import EmaxHub

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
type EmaxConfigEntry = ConfigEntry[EmaxHub]


async def async_setup_entry(hass: HomeAssistant, entry: EmaxConfigEntry) -> bool:
    """Create a listener; entities wait for the next real measurement."""
    hub = EmaxHub(
        hass,
        entry.data[CONF_STATION_IP],
        entry.data[CONF_EXPECTED_MAC],
        entry.data[CONF_LISTEN_PORT],
        entry.data[CONF_NAME],
    )
    try:
        await hub.async_start()
    except OSError as error:
        raise ConfigEntryNotReady(
            f"Cannot listen on TCP port {entry.data[CONF_LISTEN_PORT]}: {error}"
        ) from error

    entry.runtime_data = hub
    hub.last_control_result = dict(entry.data.get(CONF_LAST_SETUP, {}))
    request = entry.data.get(CONF_SETUP_REQUEST, {})
    if request.get("state") == "in_progress":
        hub.setup_status = "interrupted"
        hub.control_error = "Previous setup was interrupted; use Reconfigure to verify and retry."
    elif hub.last_control_result.get("result") == "unverified":
        hub.setup_status = "error"
        hub.control_error = hub.last_control_result.get("error")

    async def stop_listener(_event: Event) -> None:
        await hub.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_listener)
    )
    # This also runs if platform setup fails or HA unloads this entry.
    entry.async_on_unload(hub.async_stop)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.async_stop()
        raise
    if request.get("state") == "pending":
        entry.async_create_background_task(
            hass, hub.async_configure(entry, request), "emax_station_setup"
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmaxConfigEntry) -> bool:
    """Unload entities; registered cleanup then closes the passive listener."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
