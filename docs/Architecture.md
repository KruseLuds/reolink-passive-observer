# Architecture

## Purpose

Reolink Passive Observer converts an observable outbound network behavior into
a short-lived MQTT activity state.

It is intentionally not a camera integration. It does not authenticate to the
device, request media, control the device, or decode Reolink event metadata.

## Observation model

During activity, the tested Reolink battery device creates outbound TLS
traffic containing a recognizable hostname during connection setup. Because
the observer sees a mirrored copy of that traffic, it can search the printable
`tcpdump` output for the configured hostname without decrypting the TLS
session.

The default capture filter is conceptually:

```text
src host <doorbell_ip> and tcp dst port <capture_destination_port>
```

The observer invokes `tcpdump` with:

- `-i` to select the mirror-receiving interface
- `-nn` to prevent name and service resolution
- `-s 0` to capture the full packet
- `-l` for line-buffered output
- `-A` to print packet payload as text

No packet-capture file is written during normal operation.

## Processing pipeline

1. Load and validate JSON configuration.
2. Configure logging.
3. Connect to MQTT and start the Paho network loop.
4. Publish Home Assistant MQTT Discovery.
5. Publish retained `online` availability.
6. Start `tcpdump` with the configured filter.
7. Read stdout in chunks.
8. Append lowercased bytes to a bounded rolling buffer.
9. Search for the configured lowercased hostname.
10. Publish retained `ON` when a new event begins.
11. Start or replace the event-clear timer.
12. Publish retained `OFF` after the newest event's hold period.
13. Restart capture after unexpected `tcpdump` failure.
14. On shutdown, stop capture, publish `OFF` and `offline`, and disconnect.

## Why a rolling buffer is required

A hostname may be divided between two reads from the `tcpdump` pipe. Searching
only the newest chunk could miss a match that crosses the read boundary.

The observer therefore appends each lowercased chunk to a rolling buffer and
retains only the newest `capture_buffer_size` bytes. This handles boundary
splits while preventing unbounded memory growth.

After a match, the buffer is cleared so one appearance does not repeatedly
trigger from retained bytes.

## Event state and timing

The observer maintains:

- `event_active`
- `last_trigger_monotonic`
- `off_timer`
- `event_generation`

When activity begins:

- the observer checks the debounce window
- it publishes retained `ON`
- it marks the event active
- it increments the generation number
- it schedules an `OFF` callback

When more activity arrives while active, the observer does not publish another
new `ON`; it replaces the pending timer so the active period extends from the
newest activity.

The timer receives the generation number that existed when it was created. If
an older cancelled callback still runs during a scheduling race, it sees that
its generation is stale and exits without clearing the newer event.

`time.monotonic()` is used for debounce measurement so clock corrections do
not distort elapsed-time calculations. Wall-clock local time is used only for
human-readable log timestamps.

## MQTT design

### Discovery

The observer publishes a retained Home Assistant MQTT Discovery configuration
for one binary sensor:

```text
<discovery_prefix>/binary_sensor/<device_identifier>/activity/config
```

The entity uses:

- `state_topic` for retained `ON` and `OFF`
- `availability_topic` for retained `online` and `offline`
- a stable `unique_id`
- configured device metadata

### Availability

Before connecting, the client registers an MQTT Last Will of retained
`offline`. If the process or host disappears without a clean disconnect, the
broker publishes that state.

After a successful connection, the observer publishes retained `online`.

During an orderly shutdown, it publishes retained `OFF` and retained
`offline` before disconnecting.

### Reconnection behavior

Paho's background network loop manages MQTT traffic after startup. On
reconnection, the connect callback republishes discovery, availability, and
the current activity state.

## Capture process supervision

`tcpdump` runs as a child process with stdout and stderr pipes.

The observer uses `select.select()` with `capture_poll_seconds` so it can:

- read available capture data without blocking forever
- notice shutdown requests
- detect an unexpected child-process exit

An unexpected failure is logged with captured stderr. The main loop then waits
`capture_restart_seconds` and starts a new capture process.

## Shutdown behavior

SIGTERM and SIGINT are handled so the service can stop cleanly.

Shutdown order is deliberate:

1. mark the observer as stopping
2. cancel the pending timer
3. terminate `tcpdump`
4. publish `OFF`
5. publish `offline`
6. disconnect MQTT
7. stop the MQTT loop

Stopping capture before MQTT prevents buffered packet output from creating a
new event while the service is already shutting down.

## Configuration selection

The configuration path is resolved in this order:

1. `--config /path/to/config.json`
2. `REOLINK_OBSERVER_CONFIG`
3. `config.json` beside the script

This keeps the simple single-directory deployment while allowing package-style
or hardened installations to store credentials elsewhere.

## Trust boundary and privacy

The observer has access to whatever traffic the mirror destination supplies.
The switch configuration should therefore mirror only what is operationally
necessary.

The program further limits processing with a source-IP and destination-port
capture filter. It does not save normal packet captures or decrypt TLS.

MQTT credentials remain the principal secret used by the daemon. Protect the
production configuration with restrictive filesystem permissions and a
dedicated, least-privilege MQTT account.

## Known limitations

The detection mechanism depends on network behavior rather than a documented
local event API. It may therefore be affected by:

- Reolink firmware changes
- DNS, TLS, or cloud endpoint changes
- device roaming
- mirror topology changes
- network offloading or capture-interface behavior
- events that do not generate the expected outbound connection
- multiple event categories producing indistinguishable traffic

The binary sensor should be interpreted as passive observed activity, not as a
forensically verified event type.
