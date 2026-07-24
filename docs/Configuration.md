# Configuration Reference

The observer reads a standard JSON object. JSON does not support `#` or `//`
comments, so explanations belong in this document rather than inside
`config.json`.

By default, the program reads `config.json` beside
`reolink_passive_observer.py` in the same directory. A different file can be
selected with:

```bash
python3 reolink_passive_observer.py --config /path/to/config.json
```

or:

```bash
REOLINK_OBSERVER_CONFIG=/path/to/config.json   python3 reolink_passive_observer.py
```

The command-line option has priority over the environment variable.

The repository ignores both root-level `config.json` and the complete
`local/` directory because production files may contain credentials and
private network information.

Start from:

```bash
cp examples/config.example.json config.json
chmod 600 config.json
```

Validate the syntax after every edit:

```bash
python3 -m json.tool config.json >/dev/null
```

## Required settings

The program requires non-empty strings for:

- `capture_interface`
- `doorbell_ip`
- `trigger_hostname`
- `mqtt_host`
- `mqtt_topic`
- `availability_topic`
- `device_name`
- `device_identifier`

## Packet capture

### `capture_interface`

Linux interface receiving mirrored traffic, commonly `eth0`.

This must be the interface on which the observer host actually receives the
copied Reolink packets. Merely being on the same VLAN is not sufficient on a
normal switched network.

Example:

```json
"capture_interface": "eth0"
```

### `doorbell_ip`

Stable, preferably DHCP-reserved, address of the observed Reolink device.

The value is placed into the `tcpdump` source-host filter. If the device later
receives another address, detection stops until the configuration is updated.

Example:

```json
"doorbell_ip": "192.168.1.100"
```

No Reolink username or password is required.

### `trigger_hostname`

Hostname searched for in the printable TLS connection setup.

Tested value:

```json
"trigger_hostname": "pushx.reolink.com"
```

Changing this without packet-capture evidence may stop detection.

### `tcpdump_path`

Absolute path or executable name for `tcpdump`.

Default:

```json
"tcpdump_path": "/usr/bin/tcpdump"
```

When a command name rather than an absolute path is supplied, the observer
resolves it through `PATH`.

### `capture_destination_port`

Outbound TCP destination port included in the capture filter.

Default:

```json
"capture_destination_port": 443
```

### `capture_read_size`

Maximum bytes read from the `tcpdump` stdout pipe per operation.

Default:

```json
"capture_read_size": 4096
```

### `capture_buffer_size`

Maximum number of recent lowercased capture bytes retained for hostname
matching.

The rolling buffer allows a hostname divided between adjacent reads to be
recognized while preventing unbounded memory growth.

Default:

```json
"capture_buffer_size": 16384
```

### `capture_poll_seconds`

Maximum `select()` wait before checking shutdown state and unexpected
`tcpdump` exit.

Default:

```json
"capture_poll_seconds": 0.5
```

### `capture_restart_seconds`

Delay before restarting `tcpdump` after an unexpected capture failure.

Default:

```json
"capture_restart_seconds": 5
```

## MQTT

### `mqtt_host`

Address or hostname of the MQTT broker.

Example:

```json
"mqtt_host": "192.168.1.10"
```

The broker may run on Home Assistant or elsewhere.

### `mqtt_port`

Broker port.

Default:

```json
"mqtt_port": 1883
```

### `mqtt_username` and `mqtt_password`

MQTT broker credentials. They are not Reolink credentials.

A dedicated least-privilege account is recommended.

```json
"mqtt_username": "reolink-passive-observer",
"mqtt_password": "replace-with-a-dedicated-mqtt-password"
```

### `mqtt_keepalive_seconds`

Paho MQTT keepalive interval passed to the broker connection.

Default:

```json
"mqtt_keepalive_seconds": 60
```

### `mqtt_connect_timeout_seconds`

Maximum startup wait for the MQTT connect callback.

Default:

```json
"mqtt_connect_timeout_seconds": 10
```

### `mqtt_client_id`

MQTT client identifier.

Default:

```json
"mqtt_client_id": "reolink-passive-observer"
```

Each simultaneously running observer connected to the same broker should have
a distinct client ID.

### `mqtt_topic`

Retained activity state topic. Payloads are `ON` and `OFF`.

Example:

```json
"mqtt_topic": "reolink/battery_device/activity"
```

### `availability_topic`

Retained availability topic. Payloads are `online` and `offline`.

Example:

```json
"availability_topic": "reolink/battery_device/availability"
```

### `home_assistant_discovery_prefix`

Home Assistant MQTT Discovery prefix.

Default:

```json
"home_assistant_discovery_prefix": "homeassistant"
```

Change it only when the MQTT integration uses a non-default discovery prefix.

## Home Assistant metadata

### `device_name`

Display name of the MQTT device.

```json
"device_name": "Reolink Battery Device Passive Observer"
```

### `device_identifier`

Stable MQTT Discovery device identifier.

```json
"device_identifier": "reolink_battery_device_passive_observer"
```

Use lowercase letters, numbers, and underscores. Changing this value later
creates a different MQTT device in Home Assistant.

### `entity_name`

Name of the discovered binary sensor.

Default:

```json
"entity_name": "Activity"
```

### `device_manufacturer`

Manufacturer shown in Home Assistant.

Published example:

```json
"device_manufacturer": "Reolink Passive Observer"
```

A private installation may use different local metadata.

### `device_model`

Implementation description shown in Home Assistant.

Default:

```json
"device_model": "Battery device passive TLS observer"
```

### `device_class`

Home Assistant binary-sensor device class.

Default:

```json
"device_class": "motion"
```

The motion class provides familiar UI behavior. It does not mean the observer
has identified a Reolink motion event specifically; it detects matching
network activity.

## Timing

### `event_hold_seconds`

Number of seconds the activity sensor remains `ON` after the newest matching
activity.

Default:

```json
"event_hold_seconds": 10
```

Repeated activity while already active restarts the timer.

### `debounce_seconds`

Minimum separation between a completed event and a new event.

Default:

```json
"debounce_seconds": 5
```

This does not prevent activity from extending an event that is already `ON`.

## Logging

### `log_level`

Supported values:

- `DEBUG`
- `INFO`
- `WARNING`
- `ERROR`
- `CRITICAL`

Default:

```json
"log_level": "INFO"
```

Use `DEBUG` temporarily for diagnosis. Under `systemd`, output is collected by
journald.

## Complete example

See [`examples/config.example.json`](../examples/config.example.json).

Do not place real MQTT credentials, private addresses, or production topics in
the published example file.
