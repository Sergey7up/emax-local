"""An explicit, verified Wi-Fi module restart (not a factory reset)."""

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory

from .entity import EmaxEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EmaxRestartButton(entry)])


class EmaxRestartButton(EmaxEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "restart_module"

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self.entry = entry
        self._attr_unique_id = f"{self.hub.mac}_restart_module"

    @property
    def available(self):
        return self.hub.setup_status != "configuring"

    async def async_press(self):
        await self.hub.async_restart_module(self.entry)
