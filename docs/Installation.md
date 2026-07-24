# Installation

This guide installs Reolink Passive Observer as a persistent Linux `systemd`
service.

## 1. Prepare the network path

The observer cannot see ordinary unicast traffic merely because it shares a
VLAN with the Reolink device. Configure a managed switch, access point, router,
or other capture mechanism so the observer interface receives a copy of the
device's relevant outbound traffic.

A typical design is:

```text
Reolink device -> access point -> managed switch -> network
                                      |
                                      +-> mirrored destination port
                                             |
                                             +-> observer host eth0
```

Create a DHCP reservation for the Reolink device. The configured
`doorbell_ip` is embedded in the `tcpdump` capture filter, so an unexpected
address change stops detection.

For a device that can roam among access points, ensure that every possible
uplink path is included in the mirror source or constrain the device to a path
that is always mirrored.

## 2. Install operating-system requirements

On Debian, Raspberry Pi OS, Ubuntu, or a related distribution:

```bash
sudo apt update
sudo apt install -y tcpdump python3 python3-pip
```

Install Paho MQTT:

```bash
python3 -m pip install paho-mqtt
```

Where the distribution enforces externally managed Python environments, use a
virtual environment instead:

```bash
sudo apt install -y python3-venv
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install paho-mqtt
```

When using a virtual environment, substitute its Python executable in the
service file.

## 3. Install the project

Example location:

```bash
sudo mkdir -p /opt/reolink-passive-observer
sudo cp reolink_passive_observer.py /opt/reolink-passive-observer/
sudo cp examples/config.example.json /opt/reolink-passive-observer/config.json
sudo chown -R root:root /opt/reolink-passive-observer
sudo chmod 755 /opt/reolink-passive-observer/reolink_passive_observer.py
sudo chmod 600 /opt/reolink-passive-observer/config.json
```

A user-owned installation such as
`/home/pi/reolink_passive_observer` also works. Keep the script and its default
`config.json` together unless the service uses `--config` or the
`REOLINK_OBSERVER_CONFIG` environment variable.

## 4. Configure the observer

Edit the private production file:

```bash
sudo nano /opt/reolink-passive-observer/config.json
```

Validate its JSON syntax:

```bash
python3 -m json.tool   /opt/reolink-passive-observer/config.json >/dev/null
```

See the complete [configuration reference](Configuration.md).

## 5. Test in the foreground

Run the observer directly before creating the service:

```bash
sudo /usr/bin/python3   /opt/reolink-passive-observer/reolink_passive_observer.py
```

Expected startup messages include:

```text
Using configuration file ...
Connecting to MQTT broker ...
Connected to MQTT broker ...
Starting passive capture on ...
Watching for TLS hostname ...
```

Generate activity at the Reolink device. A successful event produces messages
similar to:

```text
REOLINK ACTIVITY DETECTED at ...
Published ON to MQTT topic ...
Clearing Reolink activity state
```

Stop the test with `Ctrl+C`.

## 6. Create the systemd service

Create:

```bash
sudo nano /etc/systemd/system/reolink-passive-observer.service
```

Example service:

```ini
[Unit]
Description=Reolink Battery Device Passive Observer
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/reolink-passive-observer
ExecStart=/usr/bin/python3 /opt/reolink-passive-observer/reolink_passive_observer.py
Restart=on-failure
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

Using `root` is straightforward because packet capture normally requires
elevated privileges. A hardened non-root installation may instead assign
appropriate capabilities to `tcpdump`; test that design carefully on the
target distribution.

For a virtual environment, use for example:

```ini
ExecStart=/opt/reolink-passive-observer/venv/bin/python /opt/reolink-passive-observer/reolink_passive_observer.py
```

For a configuration stored elsewhere:

```ini
ExecStart=/usr/bin/python3 /opt/reolink-passive-observer/reolink_passive_observer.py --config /etc/reolink-passive-observer/config.json
```

## 7. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reolink-passive-observer
```

Check the complete unellipsized status:

```bash
sudo systemctl status reolink-passive-observer --no-pager -l
```

Follow logs:

```bash
sudo journalctl -u reolink-passive-observer -f
```

## 8. Verify Home Assistant

With MQTT Discovery enabled, Home Assistant should create the configured
device and binary sensor automatically.

Confirm:

- the device is available
- activity changes to `ON`
- the state returns to `OFF` after `event_hold_seconds`
- restarting the service produces a clean `offline`/`online` availability
  transition
- the discovery metadata shows the expected name, manufacturer, and model

## Updating an existing installation

Back up the current script and configuration before replacement:

```bash
cd /opt/reolink-passive-observer
STAMP=$(date +%Y%m%d_%H%M%S)
sudo cp reolink_passive_observer.py   reolink_passive_observer.py.bak_$STAMP
sudo cp config.json config.json.bak_$STAMP
```

Replace the files, validate JSON, and restart:

```bash
python3 -m json.tool config.json >/dev/null
sudo systemctl restart reolink-passive-observer
sudo systemctl status reolink-passive-observer --no-pager -l
```

## Troubleshooting

### Service starts but no activity is detected

Check the effective capture process:

```bash
sudo systemctl status reolink-passive-observer --no-pager -l
```

Run a temporary manual capture using the same interface, source address, and
destination port:

```bash
sudo tcpdump -i eth0 -nn -s 0 -A   'src host 192.168.1.100 and tcp dst port 443'
```

Generate activity and verify that the configured hostname is visible. Do not
publish packet-capture output because it may contain private network details.

Investigate:

- incorrect `capture_interface`
- stale `doorbell_ip`
- mirror source or destination configured incorrectly
- device roaming to an unmirrored access point
- VLAN or switch path different from the assumed topology
- hostname or destination-port behavior changed by firmware or service changes

### tcpdump exits immediately

Read the full journal:

```bash
sudo journalctl -u reolink-passive-observer -n 100 --no-pager
```

Common causes include:

- incorrect `tcpdump_path`
- nonexistent interface
- insufficient capture privileges
- malformed capture values

### MQTT connection fails

Verify broker reachability and credentials from the observer host. Confirm
that the dedicated account may publish to:

- the discovery topic under `home_assistant_discovery_prefix`
- `mqtt_topic`
- `availability_topic`

### Duplicate or overly long events

`event_hold_seconds` controls the time from the newest matching activity until
`OFF`. Repeated matching activity while active restarts this timer.

`debounce_seconds` suppresses a new event immediately after the previous event
has completed. It does not shorten an event that is already active.

### Home Assistant retains an old device

Changing `device_identifier` creates a new MQTT Discovery identity. Remove the
old retained discovery document or delete the obsolete MQTT device from Home
Assistant after confirming it is no longer being published.
