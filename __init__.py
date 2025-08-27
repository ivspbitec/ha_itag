# custom_components/itag_bt/__init__.py
from __future__ import annotations
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "itag_bt"
PLATFORMS = ["binary_sensor", "switch", "sensor"]
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import ITagClient

    mac = entry.data["mac"].upper()
    store = hass.data.setdefault(DOMAIN, {})
    clients = store.setdefault("clients", {})
    forwarded: set[str] = store.setdefault("forwarded_entries", set())

    if mac not in clients:
        clients[mac] = ITagClient(hass, mac)

    # постоянный мониторинг рекламы + автоконнект при появлении ADV
    clients[mac].start_advert_watch()

    if entry.entry_id not in forwarded:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        forwarded.add(entry.entry_id)

        def _on_unload() -> None:
            store.get("forwarded_entries", set()).discard(entry.entry_id)
        entry.async_on_unload(_on_unload)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mac = entry.data["mac"].upper()
    store = hass.data.get(DOMAIN, {})
    clients = store.get("clients", {})
    forwarded: set[str] = store.get("forwarded_entries", set())

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    client = clients.pop(mac, None)
    if client:
        client.stop_advert_watch()
        await client.disconnect()

    forwarded.discard(entry.entry_id)
    if not clients:
        hass.data.pop(DOMAIN, None)
    return ok