from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak.exc import BleakError
from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

# GATT UUIDs
UUID_BTN   = "0000ffe1-0000-1000-8000-00805f9b34fb"  # notify (кнопка)
UUID_ALERT = "00002a06-0000-1000-8000-00805f9b34fb"  # write: 0x00=Off, 0x02=High
UUID_BATT  = "00002a19-0000-1000-8000-00805f9b34fb"  # read (батарея)

# Сигналы на шину HA
SIGNAL_BTN  = "itag_bt_button"
SIGNAL_CONN = "itag_bt_connected"
SIGNAL_DISC = "itag_bt_disconnected"

class ITagClient:
    def __init__(self, hass: HomeAssistant, mac: str) -> None:
        self.hass = hass
        self.mac = mac.upper()
        self.client: Optional[BleakClientWithServiceCache] = None
        self._connect_lock = asyncio.Lock()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._adv_remove = None
        self._last_attempt = 0.0
        self._attempt_min_interval = 3.0  # антишторм (сек)

    # -------- мониторинг рекламы и автоконнект --------
    def start_advert_watch(self) -> None:
        if self._adv_remove is not None:
            return

        def _adv_cb(dev, adv):
            addr = getattr(dev, "address", "")
            if not addr:
                return
            if addr.upper() != self.mac:
                return  # не наш адрес

            now = time.monotonic()
            if now - self._last_attempt < self._attempt_min_interval:
                return
            self._last_attempt = now

            if self.client and getattr(self.client, "is_connected", False):
                return

            _LOGGER.debug("ITag[%s] ADV seen, scheduling connect", self.mac)
            self.hass.async_create_task(self.connect())

        # слушаем весь эфир; фильтр по MAC в колбэке
        self._adv_remove = bluetooth.async_register_callback(self.hass, _adv_cb, {}, False)

    def stop_advert_watch(self) -> None:
        if self._adv_remove:
            try:
                self._adv_remove()
            except Exception:
                pass
            self._adv_remove = None

    # -------- служебное: записать во все 2A06 --------
    async def _write_alert_all(self, payload: bytes) -> None:
        if not self.client:
            return
        try:
            services = getattr(self.client, "services", None)
            targets = []
            if services is not None:
                for srv in services:
                    for ch in srv.characteristics:
                        if ch.uuid.lower() == UUID_ALERT:
                            targets.append(ch)
            if not targets:
                await self.client.write_gatt_char(UUID_ALERT, payload)  # type: ignore[attr-defined]
            else:
                for ch in targets:
                    await self.client.write_gatt_char(ch, payload)       # type: ignore[attr-defined]
        except Exception as e:
            _LOGGER.debug("ITag[%s] write alert failed (ignored): %s", self.mac, e)

    # -------- keepalive --------
    async def _keepalive_loop(self):
        _LOGGER.debug("ITag[%s] keepalive start", self.mac)
        try:
            while self.client and getattr(self.client, "is_connected", False):
                await self._write_alert_all(b"\x00")  # гасим Link Loss
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            pass
        finally:
            _LOGGER.debug("ITag[%s] keepalive stop", self.mac)

    def _start_keepalive(self):
        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = self.hass.loop.create_task(self._keepalive_loop())

    def _stop_keepalive(self):
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    # -------- connect / disconnect --------
    def _on_disconnected(self, _client):
        _LOGGER.debug("ITag[%s] disconnected", self.mac)
        self._stop_keepalive()
        try:
            self.hass.loop.call_soon_threadsafe(
                self.hass.bus.async_fire, f"{SIGNAL_DISC}_{self.mac}"
            )
        except Exception:
            pass
        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(self.connect()))

    async def connect(self):
        async with self._connect_lock:
            if self.client and getattr(self.client, "is_connected", False):
                return
            _LOGGER.debug("ITag[%s] connect() start", self.mac)

            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.mac, connectable=True)

            if ble_device:
                try:
                    self.client = await establish_connection(
                        BleakClientWithServiceCache, ble_device, self.mac, timeout=15.0
                    )
                    try:
                        self.client.set_disconnected_callback(self._on_disconnected)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    await self.client.start_notify(UUID_BTN, self._cb_notify)        # type: ignore[attr-defined]
                    await asyncio.sleep(0)
                    await self._write_alert_all(b"\x00")
                    self._start_keepalive()
                    _LOGGER.debug("ITag[%s] connected + notify", self.mac)
                    self.hass.bus.async_fire(f"{SIGNAL_CONN}_{self.mac}")
                    return
                except BleakError as e:
                    _LOGGER.debug("ITag[%s] manager connect failed: %s", self.mac, e)
                    self.client = None

            # Fallback: прямой Bleak без менеджера HA. Не бросаем исключение.
            try:
                direct = BleakClient(self.mac, timeout=15.0)
                await direct.__aenter__()
                self.client = direct  # type: ignore[assignment]
                try:
                    self.client.set_disconnected_callback(self._on_disconnected)  # type: ignore[attr-defined]
                except Exception:
                    pass
                await self.client.start_notify(UUID_BTN, self._cb_notify)          # type: ignore[attr-defined]
                await asyncio.sleep(0)
                await self._write_alert_all(b"\x00")
                self._start_keepalive()
                _LOGGER.debug("ITag[%s] connected (direct) + notify", self.mac)
                self.hass.bus.async_fire(f"{SIGNAL_CONN}_{self.mac}")
            except Exception as e:
                _LOGGER.debug("ITag[%s] direct connect failed: %s", self.mac, e)
                self.client = None

    async def disconnect(self):
        _LOGGER.debug("ITag[%s] disconnect()", self.mac)
        self._stop_keepalive()
        if self.client:
            try:
                await self._write_alert_all(b"\x00")
            except Exception:
                pass
            try:
                await self.client.stop_notify(UUID_BTN)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                await self.client.disconnect()           # type: ignore[attr-defined]
            except Exception:
                try:
                    await self.client.__aexit__(None, None, None)  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.client = None

    # -------- события --------
    def _cb_notify(self, _handle, _data: bytes):
        self.hass.loop.call_soon_threadsafe(
            self.hass.bus.async_fire, f"{SIGNAL_BTN}_{self.mac}"
        )

    async def beep(self, on: bool):
        if not self.client or not getattr(self.client, "is_connected", False):
            await self.connect()
        if not self.client or not getattr(self.client, "is_connected", False):
            return
        await self._write_alert_all(b"\x02" if on else b"\x00")

    async def read_battery(self) -> Optional[int]:
        if not self.client or not getattr(self.client, "is_connected", False):
            await self.connect()
        if not self.client or not getattr(self.client, "is_connected", False):
            return None
        v = await self.client.read_gatt_char(UUID_BATT)  # type: ignore[attr-defined]
        return int(v[0]) if v else None