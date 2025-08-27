# iTag BLE — кастомная интеграция для Home Assistant

Интеграция для работы с BLE‑брелками семейства **iTag** (в т.ч. клоны вроде **PALMEXX iTag Key Finder**). Даёт кнопку, сирену и уровень батареи; поддерживает «мгновенное» подключение при появлении рекламы и авто‑переподключение.

---

## Возможности

* **`binary_sensor`** — нажатие кнопки (сервис FFE0, характеристика **FFE1/notify**).
* **`switch`** — управление писком (сервис **Immediate Alert 0x1802**, характеристика **0x2A06**: `0x02` — писк, `0x00` — тишина).
* **`switch` (Link Alert)** — управление писком при потере связи (сервис **Link Loss 0x1803**, характеристика **0x2A06**: `0x00`/`0x01`/`0x02`). Записывается строго в 0x1803 с подтверждением (write-with-response) и проверкой чтением.
* **`sensor`** — уровень батареи (сервис **Battery 0x180F**, характеристика **0x2A19**).
* Пассивный **мониторинг BLE‑рекламы** выбранного MAC и **автоподключение** при первом ADV.
* Подписка на уведомления **FFE1**; события коннекта/дисконнекта на шину HA.
* Защита от ложного писка (keepalive — периодическая запись `0x00` в Immediate Alert **0x1802:2A06**).

---

## Требования

* Home Assistant Core с включённой встроенной интеграцией **Bluetooth**.
* Хост/контейнер с **BlueZ** и доступом к BLE‑адаптеру.

  * Для Docker: `network_mode: host`, `privileged: true`.
* Достаточно свободных GATT‑подключений на адаптере (не держите одновременно множество активных BLE‑сессий).

---

## Установка

1. Скопируйте папку `custom_components/itag_bt` в каталог конфигурации Home Assistant:
   `/config/custom_components/itag_bt/`
   (например, `/home/homeassistant/.homeassistant/custom_components/itag_bt/`).
2. Перезапустите Home Assistant.
3. В интерфейсе: **Настройки → Устройства и службы → Добавить интеграцию → iTag BLE**.
4. Укажите **MAC‑адрес** брелка в верхнем регистре (например, `FF:05:24:18:0D:CB`).

> Интеграцию можно добавить несколько раз — по одному устройству на запись.

---

## Что появляется в HA

* **Устройство**: `iTag <MAC>`
* **Сущности**:

  * `binary_sensor.iTag Button <MAC>` — мигает при нажатии.
  * `switch.iTag Beep <MAC>` — включает/выключает писк.
  * `switch.iTag Link Alert <MAC>` — управляет писком при разрыве (Link Loss).
  * `sensor.iTag Battery` — процент заряда.

Сущности имеют уникальные ID с суффиксом `_v2`.

---

## Как это работает

* Интеграция регистрирует **пассивный слушатель рекламы** BLE. Как только видит ADV нужного **MAC**, запускает попытку GATT‑подключения (через Bluetooth Manager HA).
* После соединения:

  * подписывается на **FFE1** (кнопка);
  * сбрасывает оповещение **2A06** в `0x00`, чтобы брелок не пищал при разрыве;
  * периодически отправляет `2A06=0x00` как **keepalive** (только для Immediate Alert 0x1802);
  * применяет политику Link Loss строго к `0x1803:0x2A06` (write-with-response + readback), сам keepalive **не трогает Link Loss**.
* События на шине HA:

  * Нажатие кнопки → `itag_bt_button_<MAC>`
  * Коннект → `itag_bt_connected_<MAC>`
  * Дисконнект → `itag_bt_disconnected_<MAC>`

---

## Поддерживаемые GATT UUID’ы

* **Кнопка**: `0000FFE1-0000-1000-8000-00805F9B34FB` (notify), сервис `FFE0`.
* **Сирена (немедленный писк)**: `00002A06-0000-1000-8000-00805F9B34FB` (write `0x00`/`0x02`), сервис `0x1802 Immediate Alert`.
* **Link Loss (писк при разрыве)**: сервис `0x1803` / характеристика `0x2A06` — уровень `0x00`/`0x01`/`0x02`.
* **Батарея**: `00002A19-0000-1000-8000-00805F9B34FB` (read), сервис `0x180F Battery`.

> Примечание: у большинства клонов iTag UUID одинаковые. У редких вариантов может отсутствовать Battery Service либо отличаться поведение `0x01/0x02` для Alert Level.

---

## Ограничения и нюансы

* **Первое нажатие после сна** может уйти на пробуждение/подключение. Обычно реакция — со второго клика; при удачной рекламе — с первого.
* Встроенный адаптер RPi4 стабильно держит **немного** одновременных GATT‑соединений. При переполнении — ошибки вида *“no connection slot”*.
* iTag пищит при **разрыве** (Link Loss). Интеграция гасит Immediate Alert `2A06=0x00` после коннекта, но короткий писк при перезагрузке HA возможен.
* **Клоны:** у части iTag значение `0x1803:2A06` **игнорируется** — устройство пищит при разрыве независимо от уровня (особенность прошивки). В таких случаях переключатель *Link Alert* не влияет на поведение.
* Если используете сторонние BLE‑интеграции, убедитесь, что они **не удерживают** GATT с тем же брелком.

---

## Отладка

### Включение подробного лога (пример `configuration.yaml`)

```yaml
logger:
  default: info
  logs:
    custom_components.itag_bt: debug
    homeassistant.components.bluetooth: debug
```

**Где смотреть:**
**Настройки → Система → Журналы** — строки вида `custom_components.itag_bt...` и `homeassistant.components.bluetooth...`.

**Если Home Assistant запущен в Docker:**

```bash
docker logs -f --tail=100 homeassistant
```

### Что полезно видеть в логах

* ADV обнаружен: `... ADV seen, scheduling connect`
* Установка соединения: `... connected + notify`
* Keepalive: `... keepalive start/stop`
* Чтение батареи: `... battery -> <value>` (если включено в коде)

---

## FAQ

**Почему при перезагрузке HA брелок может пикнуть?**
При разрыве соединения iTag запускает Link Loss. Интеграция гасит его записью `2A06=0x00` после коннекта, но короткий писк в момент разрыва возможен.

**Можно ли добиться мгновенной реакции всегда?**
Только держа постоянный GATT‑коннект, что увеличивает расход батареи. Интеграция балансирует: подключается по первому ADV и держит связь с умеренным keepalive.

**Как добавить второй брелок?**
Добавьте интеграцию ещё раз и укажите второй MAC.

**Почему «не хватает слотов» для коннекта?**
BLE‑адаптер ограничен по одновременным GATT‑сессиям. Закройте лишние соединения, отключите интеграции, удерживающие GATT на том же адаптере, или добавьте BLE‑прокси/второй адаптер.

---

## Структура

```
custom_components/itag_bt/
 ├─ __init__.py        # регистрация клиента, рекламный watcher, (un)load платформ
 ├─ manifest.json      # метаданные интеграции
 ├─ config_flow.py     # мастер добавления (ввод MAC)
 ├─ coordinator.py     # BLE‑клиент: connect/notify/keepalive/события, beep(), read_battery()
 ├─ binary_sensor.py   # кнопка: слушает события от coordinator
 ├─ switch.py          # сирена: 0x1802:2A06 (0x02/0x00); Link Alert: 0x1803:2A06 (0x00/0x01/0x02)
 └─ sensor.py          # батарея: читает 2A19
```

---

## Совместимость и тестирование

* Проверено на **Raspberry Pi 4B** со встроенным Bluetooth (BlueZ) и Home Assistant Core в Docker (`network_mode: host`, `privileged: true`).
* Типичные характеристики работы:

  * Первое нажатие после сна может только «будить» брелок; подключение стартует по первому ADV и занимает \~0.5–2.0 с.
  * Встроенный адаптер RPi4 выдерживает ограниченное число одновременных GATT‑соединений; стабильнее ≤2 активных сессий.
  * На брелке **PALMEXX iTag** подтверждены: кнопка FFE1/notify, Immediate Alert 0x1802/2A06, Battery 0x180F/2A19. Параметр Link Loss 0x1803/2A06 у отдельных клонов может игнорироваться (писк при разрыве остаётся включённым аппаратно).

## Лицензия

MIT License

Copyright (c) 2025 iTag BLE contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the “Software”), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
