from __future__ import annotations
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from . import DOMAIN
from .coordinator import ITagClient

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    mac = entry.data["mac"].upper()
    store = hass.data[DOMAIN]
    clients = store.setdefault("clients", {})
    client: ITagClient | None = clients.get(mac)
    if client is None:
        client = clients[mac] = ITagClient(hass, mac)
    async_add_entities([
        ITagBeepSwitch(mac, client),
        ITagLinkAlertSwitch(mac, client),
    ])

class ITagBeepSwitch(SwitchEntity):
    """Немедленный писк (Immediate Alert / 0x1802:2A06)."""

    def __init__(self, mac: str, client: ITagClient):
        self._mac = mac
        self._client = client
        self._state = False
        self._attr_name = f"iTag Beep {mac}"
        self._attr_unique_id = f"itag_beep_{mac.replace(':','_')}_v2"

    async def async_turn_on(self, **kwargs):
        await self._client.beep(True)
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._client.beep(False)
        self._state = False
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._state

class ITagLinkAlertSwitch(SwitchEntity):
    """Писк при потере связи (Link Loss / 0x1803:2A06, write-with-response + readback)."""

    def __init__(self, mac: str, client: ITagClient):
        self._mac = mac
        self._client = client
        self._attr_name = f"iTag Link Alert {mac}"
        self._attr_unique_id = f"itag_linkalert_{mac.replace(':','_')}_v2"

    async def async_turn_on(self, **kwargs):
        await self._client.set_link_alert(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._client.set_link_alert(False)
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._client.link_alert_enabled