"""Temperature, humidity, and setup entities for copied EMAX uploads."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfPressure
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EmaxConfigEntry
from .const import CHANNELS
from .entity import EmaxEntity
from .hub import EmaxHub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create all channels, including channels that are currently missing."""
    hub = entry.runtime_data
    entities: list[SensorEntity] = [
        EmaxReadingSensor(hub, index, field)
        for index in range(len(CHANNELS))
        for field in ("temperature", "humidity")
    ]
    entities.extend([EmaxSetupSensor(hub), EmaxPressureSensor(hub)])
    async_add_entities(entities)


class EmaxSensor(EmaxEntity, SensorEntity):
    """Shared sensor base."""


class EmaxReadingSensor(EmaxSensor):
    """One independent current temperature or humidity channel."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: EmaxHub, channel_index: int, field: str) -> None:
        super().__init__(hub)
        self._channel_index = channel_index
        self._temperature = field == "temperature"
        key = f"{CHANNELS[channel_index]}_{field}"
        self._attr_translation_key = key
        self._attr_unique_id = f"{hub.mac}_{key}"
        if self._temperature:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_suggested_display_precision = 1
        else:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_suggested_display_precision = 0

    @property
    def available(self) -> bool:
        """A missing field must not appear as zero or keep an old value."""
        measurement = self.hub.measurement
        return (self.hub.fresh and measurement is not None
                and measurement.radio_status(self._channel_index) in {"wired", "receiving"}
                and self.native_value is not None)

    @property
    def native_value(self) -> float | int | None:
        """Read from the latest upload; never restore a previous measurement."""
        if self.hub.measurement is None:
            return None
        reading = self.hub.measurement.readings[self._channel_index]
        return reading.temperature_c if self._temperature else reading.humidity_pct

    @property
    def extra_state_attributes(self):
        measurement = self.hub.measurement
        if measurement is None:
            return {}
        reading = measurement.readings[self._channel_index]
        return {
            "station_min": reading.temperature_min_c if self._temperature else reading.humidity_min_pct,
            "station_max": reading.temperature_max_c if self._temperature else reading.humidity_max_pct,
            "radio_status": measurement.radio_status(self._channel_index),
        }


class EmaxSetupSensor(EmaxSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "setup_status"
    _attr_options = ["configuring", "waiting_for_data", "ready", "error", "interrupted"]

    def __init__(self, hub):
        super().__init__(hub)
        self._attr_unique_id = f"{hub.mac}_setup_status"

    @property
    def native_value(self):
        return self.hub.setup_status


class EmaxPressureSensor(EmaxSensor):
    """Optional local pressure; never substitute pressure from a cloud forecast."""

    _attr_device_class = SensorDeviceClass.ATMOSPHERIC_PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPressure.HPA
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "local_pressure"

    def __init__(self, hub):
        super().__init__(hub)
        self._attr_unique_id = f"{hub.mac}_local_pressure"

    @property
    def native_value(self):
        return self.hub.measurement.pressure_hpa if self.hub.measurement else None

    @property
    def available(self):
        return self.hub.fresh and self.native_value is not None
