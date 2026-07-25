# Reolink Passive Observer

> **Status:** Production ready. Successfully deployed as a persistent `systemd` 
service on a Raspberry Pi, publishing Home Assistant MQTT Discovery entities 
from passive observation of Reolink Battery Doorbell network activity.

Reolink Passive Observer is a small Linux daemon that creates a short-lived
Home Assistant MQTT binary sensor from an observable network behavior (activity/movement) produced
by a Reolink battery doorbell.

The observer does not log in to the device, request a video stream, decrypt
HTTPS traffic, or require Reolink credentials. It watches a mirrored copy of
the device's outbound traffic and looks for a configured TLS hostname during
connection setup. When matching activity is observed, it publishes `ON` to
MQTT, holds the state for a configurable period, and then publishes `OFF`.

> [!IMPORTANT]
> This project detects matching network activity. It does not prove whether
> the underlying Reolink event was motion, person, vehicle, animal, package,
> visitor, or another event category.

## Why this project exists

Some Reolink battery devices do not expose all desired local events through
the same interfaces available on continuously powered cameras. The mobile
application may still react because the device contacts Reolink services when
activity occurs.

This project turns that observable outbound behavior into a local MQTT signal
without intercepting credentials or decrypting the connection.

## Features

- Passive observation of mirrored network traffic
- No Reolink username or password
- No connection initiated to the observed device
- No HTTPS decryption
- Home Assistant MQTT Discovery
- Retained activity and availability states
- MQTT Last Will for unexpected observer failure
- Configurable event hold and debounce timing
- Automatic restart of `tcpdump` after unexpected capture failure
- Graceful shutdown under `systemd`
- Configurable capture and MQTT parameters
- Detailed journald logging

## Data flow

```text
Reolink battery device
        |
        | outbound TLS connection
        v
Managed switch / mirrored traffic source
        |
        | copied packets
        v
Linux observer host running tcpdump
        |
        | matching hostname detected
        v
Reolink Passive Observer
        |
        | MQTT Discovery, availability, ON/OFF state
        v
MQTT broker
        |
        v
Home Assistant binary sensor
```

The observer host must actually receive the relevant packets. Merely placing
the host on the same VLAN is normally not enough on a switched network.

## Requirements

- A Linux host that can run Python 3 and `tcpdump`
- A managed switch or equivalent mechanism capable of delivering mirrored
  traffic to the observer host
- A stable DHCP reservation for the observed Reolink device
- An MQTT broker reachable from the observer host
- Home Assistant with MQTT integration for automatic entity discovery
- Permission to run `tcpdump`, commonly by running the service as `root` or by
  assigning suitable Linux capabilities

## Activity detection

The observer does not inspect or decrypt encrypted TLS traffic. Instead, it 
passively monitors the doorbell's outbound DNS queries and TLS connection 
establishment to identify activity.

The current detection model, validated using a Reolink Battery Doorbell D340B 
running firmware **v3.0.0.6543_26052720**, is:

1. The doorbell performs a DNS lookup for 
      `pushx.reolink.com`.
2. The doorbell immediately establishes a new TLS connection to 
      `pushx.reolink.com:443`.
3. Approximately 1 to 1.4 seconds later, the doorbell performs a DNS lookup for 
      `devices-apis.reolink.com`.
4. The doorbell then establishes a TLS connection to 
      `devices-apis.reolink.com:443`.

The first DNS lookup and subsequent TLS connection to `pushx.reolink.com` are 
used as the activity trigger because they occur immediately when the doorbell 
detects motion or other activity. The later connections to 
`devices-apis.reolink.com` appear to be associated with uploading or 
synchronizing video-related data and are intentionally ignored by the observer.

This design minimizes processing, avoids decrypting traffic, and allows 
activity to be detected using only passive observation of the mirrored network 
stream. If future firmware versions continue using the initial 
`pushx.reolink.com` connection as the activity trigger, the observer is 
expected to remain compatible even if later network behavior changes.

The current implementation uses the Paho MQTT Python client.

## Quick start

Clone or copy the repository to the Linux observer host:

```bash
cd /home/pi
git clone <repository-url> reolink_passive_observer
cd reolink_passive_observer
```

Install operating-system and Python requirements:

```bash
sudo apt update
sudo apt install -y tcpdump python3 python3-pip
python3 -m pip install paho-mqtt
```

Create the private production configuration:

```bash
cp examples/config.example.json config.json
chmod 600 config.json
nano config.json
```

At minimum, update:

- `capture_interface`
- `doorbell_ip`
- `mqtt_host`
- `mqtt_username`
- `mqtt_password`
- MQTT topic names
- `device_name`
- `device_identifier`

Validate the JSON and perform a foreground test:

```bash
python3 -m json.tool config.json >/dev/null
sudo /usr/bin/python3 reolink_passive_observer.py
```

Generate activity in front of the Reolink device and confirm that the log
reports detection and MQTT publication. Stop the foreground test with
`Ctrl+C`.

For a persistent installation, continue with
[Installation](docs/Installation.md).

## Documentation

- [Installation](docs/Installation.md)
- [Configuration reference](docs/Configuration.md)
- [Architecture and behavior](docs/Architecture.md)

## Home Assistant behavior

The daemon publishes a retained MQTT Discovery document for one binary sensor.
By default:

- the entity name is `Activity`
- the device class is `motion`
- `ON` indicates matching observed activity
- `OFF` indicates that the configured hold period has expired
- `online` and `offline` are published to the availability topic

Repeated matching activity while the sensor is already `ON` restarts the hold
timer. A generation counter prevents an older cancelled timer from clearing a
newer event during a race.

## Security and privacy

The observer reads only the mirrored packet stream selected by the capture
filter. It does not decrypt payloads and does not require camera credentials.

Use a dedicated MQTT account with access limited to the required discovery,
activity, and availability topics. See the above documentation for details on 
how to set that up.

## Limitations

- The signal represents matching outbound network activity, not a confirmed
  Reolink event classification.
- Detection depends on the configured hostname remaining observable during TLS
  connection setup.
- Network topology and mirror configuration are essential parts of the
  solution.
- Roaming between access points can cause missed events if not every relevant
  uplink is included in the mirror source.
- The observer does not provide video, snapshots, recordings, or doorbell
  press classification.

## Troubleshooting

For recent service logs:

```bash
sudo journalctl -u reolink-passive-observer -n 100 --no-pager
```

For live logs:

```bash
sudo journalctl -u reolink-passive-observer -f
```

Confirm that the expected process and capture filter are running:

```bash
sudo systemctl status reolink-passive-observer --no-pager -l
```

Additional diagnosis is covered in
[Installation](docs/Installation.md#troubleshooting).

## Project status

The configuration-driven implementation has been validated as a persistent
`systemd` service on a Raspberry Pi Linux host using mirrored Reolink battery
doorbell traffic, MQTT, and Home Assistant MQTT Discovery.

## License

See [LICENSE](LICENSE).
