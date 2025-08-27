from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, List, Any

from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak.exc import BleakError
from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

# Services
SVC_IMMEDIATE_ALERT = "00001802-0000-1000-8000-00805f9b34fb"  # писк по команде
SVC_LINK_LOSS       = "00001803-0000-1000-8000-00805f9b34fb"  # писк при потере связи
SVC_BATTERY         = "0000180f-0000-1000-8000-00805f9b34fb"  # справочно

# Characteristics
UUID_BTN   = "0000ffe1-0000-1000-8000-00805f9b34fb"  # notify (кнопка) — FFE0/FFE1
UUID_ALERT = "00002a06-0000-1000-8000-00805f9b34fb"  # Alert Level (write 0x00/0x01/0x02)
UUID_BATT  = "00002a19-0000-1000-8000-00805f9b34fb"  # Battery Level (read)

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

        # Политика Link Loss (по умолчанию — ВЫКЛ, чтобы не пищал на дисконнекте)
        self._link_alert_enabled: bool = False

    # -------- мониторинг рекламы и автоконнект --------
    def start_advert_watch(self) -> None:
        if self._adv_remove is not None:
            return

        def _adv_cb(dev, adv):
            addr = getattr(dev, "address", "")
            if not addr or addr.upper() != self.mac:
                return
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

    # -------- поиск характеристик внутри конкретного сервиса --------
    def _services(self) -> Any:
        return getattr(self.client, "services", None) if self.client else None

    async def _find_chars_in_service(self, service_uuid: str, char_uuid: str) -> List[Any]:
        """Найти ВСЕ характеристики char_uuid внутри конкретного service_uuid."""
        if not self.client or not getattr(self.client, "is_connected", False):
            return []
        chars: List[Any] = []
        try:
            services = self._services()
            if services is not None:
                for srv in services:
                    if str(srv.uuid).lower() == service_uuid:
                        for ch in srv.characteristics:
                            if ch.uuid.lower() == char_uuid:
                                chars.append(ch)
        except Exception:
            pass
        return chars

    # -------- точные операции над Immediate Alert и Link Loss --------
    async def _write_immediate_alert(self, payload: bytes) -> None:
        """Сброс/включение немедленного писка (0x1802:2A06). Фолбэк по UUID допустим."""
        if not self.client or not getattr(self.client, "is_connected", False):
            return
        try:
            targets = await self._find_chars_in_service(SVC_IMMEDIATE_ALERT, UUID_ALERT)
            if targets:
                for ch in targets:
                    # Для Immediate Alert обычно write without response; response=False
                    await self.client.write_gatt_char(ch, payload, response=False)  # type: ignore[attr-defined]
            else:
                # Если сервисы не распарсились — пишем по UUID характеристики
                await self.client.write_gatt_char(UUID_ALERT, payload, response=False)  # type: ignore[attr-defined]
        except Exception as e:
            _LOGGER.debug("ITag[%s] _write_immediate_alert failed (ignored): %s", self.mac, e)

    async def _write_link_loss_exact(self, level_byte: int) -> bool:
        """
        Строго записать уровень в Link Loss (0x1803:2A06) с write-with-response и прочитать обратно.
        Возвращает True, если запись подтверждена/совпала при чтении.
        """
        if not self.client or not getattr(self.client, "is_connected", False):
            return False
        payload = bytes([level_byte & 0xFF])
        try:
            targets = await self._find_chars_in_service(SVC_LINK_LOSS, UUID_ALERT)
            if not targets:
                _LOGGER.debug("ITag[%s] Link Loss 2A06 not found in services", self.mac)
                return False
            # Пишем на первую подходящую (обычно она одна)
            ch = targets[0]
            await self.client.write_gatt_char(ch, payload, response=True)  # type: ignore[attr-defined]
            # Читаем обратно
            read = await self.client.read_gatt_char(ch)                     # type: ignore[attr-defined]
            ok = bool(read) and read[0] == payload[0]
            _LOGGER.debug("ITag[%s] link-loss write %s, readback=%s ok=%s",
                          self.mac, payload.hex(), (read.hex() if read else "None"), ok)
            return ok
        except Exception as e:
            _LOGGER.debug("ITag[%s] _write_link_loss_exact failed: %s", self.mac, e)
            return False

    async def _apply_link_alert_policy(self) -> None:
        """Применить текущую политику к 0x1803:2A06 (строго)."""
        level = 0x02 if self._link_alert_enabled else 0x00
        ok = await self._write_link_loss_exact(level)
        if not ok:
            _LOGGER.debug("ITag[%s] failed to apply link-loss policy (enabled=%s)", self.mac, self._link_alert_enabled)

    # -------- keepalive --------
    async def _keepalive_loop(self):
        _LOGGER.debug("ITag[%s] keepalive start", self.mac)
        try:
            while self.client and getattr(self.client, "is_connected", False):
                # ТОЛЬКО Immediate Alert; Link Loss НЕ трогаем
                await self._write_immediate_alert(b"\x00")
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

                    # Сразу гасим Immediate Alert и применяем политику Link Loss (строго 0x1803)
                    await self._write_immediate_alert(b"\x00")
                    await self._apply_link_alert_policy()

                    self._start_keepalive()
                    _LOGGER.debug("ITag[%s] connected + notify", self.mac)
                    self.hass.bus.async_fire(f"{SIGNAL_CONN}_{self.mac}")
                    return
                except BleakError as e:
                    _LOGGER.debug("ITag[%s] manager connect failed: %s", self.mac, e)
                    self.client = None

            # Fallback: прямой Bleak без менеджера HA
            try:
                direct = BleakClient(self.mac, timeout=15.0)
                await direct.__aenter__()
                self.client = direct  # type: ignore[assignment]
                try:
                    self.client.set_disconnected_callback(self._on_disconnected)  # type: ignore[attr-defined]
                except Exception:
                    pass
                await self.client.start_notify(UUID_BTN, self._cb_notify)          # type: ignore[attr-defined]

                await self._write_immediate_alert(b"\x00")
                await self._apply_link_alert_policy()

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
                await self._write_immediate_alert(b"\x00")
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

    # -------- события / API --------
    def _cb_notify(self, _handle, _data: bytes):
        self.hass.loop.call_soon_threadsafe(
            self.hass.bus.async_fire, f"{SIGNAL_BTN}_{self.mac}"
        )

    async def beep(self, on: bool):
        if not self.client or not getattr(self.client, "is_connected", False):
            await self.connect()
        if not self.client or not getattr(self.client, "is_connected", False):
            return
        await self._write_immediate_alert(b"\x02" if on else b"\x00")

    async def set_link_alert(self, enabled: bool):
        """Включить/выключить писк при потере связи (строго 0x1803:2A06, с readback)."""
        self._link_alert_enabled = enabled
        if not self.client or not getattr(self.client, "is_connected", False):
            await self.connect()
        if self.client and getattr(self.client, "is_connected", False):
            level = 0x02 if enabled else 0x00
            ok = await self._write_link_loss_exact(level)
            # Если устройство не подтвердило — считаем выключенным (безопасно)
            if not ok:
                self._link_alert_enabled = False

    @property
    def link_alert_enabled(self) -> bool:
        return self._link_alert_enabled

    async def read_battery(self) -> Optional[int]:
        if not self.client or not getattr(self.client, "is_connected", False):
            await self.connect()
        if not self.client or not getattr(self.client, "is_connected", False):
            return None
        v = await self.client.read_gatt_char(UUID_BATT)  # type: ignore[attr-defined]
        return int(v[0]) if v else None