from __future__ import annotations
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from . import DOMAIN
from .coordinator import ITagClient, SIGNAL_BTN, SIGNAL_CONN, SIGNAL_DISC

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    mac = entry.data["mac"].upper()
    store = hass.data[DOMAIN]
    clients = store.setdefault("clients", {})
    client: ITagClient | None = clients.get(mac)
    if client is None:
        client = clients[mac] = ITagClient(hass, mac)
    async_add_entities([ITagButton(hass, mac, client)])

class ITagButton(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, mac: str, client: ITagClient):
        self.hass = hass
        self._mac = mac
        self._client = client
        self._attr_name = f"iTag Button {mac}"
        self._attr_unique_id = f"itag_btn_{mac.replace(':','_')}_v2"
        self._attr_is_on = False
        self._attr_available = False
        self._unsub_btn = None
        self._unsub_conn = None
        self._unsub_disc = None

    async def async_added_to_hass(self):
        self._unsub_btn = self.hass.bus.async_listen(f"{SIGNAL_BTN}_{self._mac}", self._on_press)
        self._unsub_conn = self.hass.bus.async_listen(f"{SIGNAL_CONN}_{self._mac}", self._on_connected)
        self._unsub_disc = self.hass.bus.async_listen(f"{SIGNAL_DISC}_{self._mac}", self._on_disconnected)
        try:
            await self._client.connect()
            if self._client.client and getattr(self._client.client, "is_connected", False):
                self._attr_available = True
                self.async_write_ha_state()
        except Exception:
            pass

    async def async_will_remove_from_hass(self):
        for u in (self._unsub_btn, self._unsub_conn, self._unsub_disc):
            if u:
                u()

    @callback
    def _on_connected(self, _):
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _on_disconnected(self, _):
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _on_press(self, _event):
        self._attr_is_on = True
        self.async_write_ha_state()
        self.hass.loop.call_later(0.2, self._auto_off)

    @callback
    def _auto_off(self):
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._mac)}, name=f"iTag {self._mac}")