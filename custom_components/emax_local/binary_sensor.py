"""Battery warnings and radio reception, without invented charge percentages."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .const import CHANNELS
from .entity import EmaxEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities([
        *[EmaxBatterySensor(hub, channel) for channel in range(4)],
        *[EmaxReceptionSensor(hub, channel) for channel in range(1, 4)],
    ])


class EmaxBinarySensor(EmaxEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self):
        return self.hub.fresh and self.is_on is not None


class EmaxBatterySensor(EmaxBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, hub, channel):
        super().__init__(hub)
        self.channel = channel
        key = f"{CHANNELS[channel]}_battery_low"
        self._attr_translation_key = key
        self._attr_unique_id = f"{hub.mac}_{key}"

    @property
    def is_on(self):
        return self.hub.measurement.battery_low(self.channel) if self.hub.measurement else None


class EmaxReceptionSensor(EmaxBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, hub, channel):
        super().__init__(hub)
        self.channel = channel
        key = f"{CHANNELS[channel]}_reception"
        self._attr_translation_key = key
        self._attr_unique_id = f"{hub.mac}_{key}"

    @property
    def is_on(self):
        if self.hub.measurement is None:
            return None
        status = self.hub.measurement.radio_status(self.channel)
        return None if status == "unknown" else status == "receiving"

    @property
    def extra_state_attributes(self):
        return {"radio_status": self.hub.measurement.radio_status(self.channel)} if self.hub.measurement else {}
