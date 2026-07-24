#!/usr/bin/env python3
"""
Reolink Battery Device Passive Observer.

This daemon converts a very specific, observable network behavior into a
short-lived MQTT activity entity for Home Assistant.

It does NOT log in to the Reolink device, use a Reolink username or password,
decrypt HTTPS traffic, or determine whether an event was a person, package,
vehicle, visitor, or ordinary motion. Instead, tcpdump reads a mirrored copy
of the device's outbound network traffic and this program looks for a
configured hostname in the TLS connection setup.

The network design is therefore part of the application:

* The observed device should have a stable DHCP reservation.
* The observer interface must receive all relevant device traffic.
* In a multi-access-point installation, the device must remain on an access
  point whose uplink traffic is included in the configured mirror source.
* The destination/mirror port must deliver that traffic to the observer host.

Configuration defaults to config.json beside this script. A different file
may be selected with --config or the REOLINK_OBSERVER_CONFIG environment
variable. The command-line option has the highest priority.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


# Keep the default configuration beside the script so the public source is
# portable and is not tied to /home/pi or any other user's directory.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIRECTORY / "config.json"

# Administrators may override the default without modifying the source.
CONFIG_ENVIRONMENT_VARIABLE = "REOLINK_OBSERVER_CONFIG"

LOGGER = logging.getLogger("reolink_passive_observer")


class ReolinkPassiveObserver:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

        # Linux interface receiving the mirrored traffic. On a small
        # observer host this is commonly eth0, but it must be configurable.
        self.interface = str(config["capture_interface"])

        # Stable DHCP-reserved address of the Reolink device. tcpdump uses it
        # to discard unrelated traffic from the mirrored link.
        self.doorbell_ip = str(config["doorbell_ip"])

        # Hostname whose appearance in the outbound TLS setup is treated as
        # activity. Store it as lowercase bytes because tcpdump output is read
        # as bytes and packet text matching should be case-insensitive.
        self.trigger_hostname = str(
            config["trigger_hostname"]
        ).encode("ascii").lower()

        # Linux distributions may install tcpdump in different locations.
        self.tcpdump_path = str(
            config.get("tcpdump_path", "/usr/bin/tcpdump")
        )

        # The tested Reolink traffic uses outbound TCP port 443, but keeping
        # this configurable avoids embedding protocol assumptions in code.
        self.capture_destination_port = int(
            config.get("capture_destination_port", 443)
        )

        # Internal capture controls normally remain at their defaults. They are
        # exposed for troubleshooting unusual hosts or very busy mirror ports.
        self.capture_read_size = int(
            config.get("capture_read_size", 4096)
        )
        self.capture_buffer_size = int(
            config.get("capture_buffer_size", 16384)
        )
        self.capture_poll_seconds = float(
            config.get("capture_poll_seconds", 0.5)
        )
        self.capture_restart_seconds = float(
            config.get("capture_restart_seconds", 5)
        )

        self.mqtt_host = str(config["mqtt_host"])
        self.mqtt_port = int(config.get("mqtt_port", 1883))
        # MQTT authentication is optional. A dedicated broker account is
        # strongly recommended. These are MQTT credentials, not camera
        # credentials; the observer never logs in to the Reolink device.
        self.mqtt_username = str(config.get("mqtt_username", ""))
        self.mqtt_password = str(config.get("mqtt_password", ""))

        self.mqtt_keepalive_seconds = int(
            config.get("mqtt_keepalive_seconds", 60)
        )
        self.mqtt_connect_timeout_seconds = float(
            config.get("mqtt_connect_timeout_seconds", 10)
        )
        self.mqtt_client_id = str(
            config.get(
                "mqtt_client_id",
                "reolink-passive-observer",
            )
        )

        self.event_topic = str(config["mqtt_topic"])
        self.availability_topic = str(config["availability_topic"])
        self.discovery_prefix = str(
            config.get(
                "home_assistant_discovery_prefix",
                "homeassistant",
            )
        )

        # MQTT Discovery metadata. device_identifier and mqtt_client_id must
        # be unique when more than one observer instance uses the same broker.
        self.device_name = str(config["device_name"])
        self.device_identifier = str(config["device_identifier"])
        self.entity_name = str(config.get("entity_name", "Activity"))
        self.device_manufacturer = str(
            config.get("device_manufacturer", "Reolink")
        )
        self.device_model = str(
            config.get(
                "device_model",
                "Battery device passive TLS observer",
            )
        )
        self.device_class = str(
            config.get("device_class", "motion")
        )

        # Hold time controls how long the Home Assistant binary sensor stays
        # ON after the newest matching connection. Repeated activity while ON
        # replaces the pending OFF timer and therefore extends the event.
        self.event_hold_seconds = float(
            config.get("event_hold_seconds", 10)
        )

        # Debounce suppresses a brand-new event immediately after a completed
        # event. It does not prevent an active event from being extended.
        self.debounce_seconds = float(
            config.get("debounce_seconds", 5)
        )

        # Mutable state begins only after all configuration has been read.
        self.running = True

        # threading.Event safely communicates MQTT connection state to the
        # capture loop and the timer callback.
        self.mqtt_connected = threading.Event()

        self.capture_process: subprocess.Popen[bytes] | None = None
        self.last_trigger_monotonic = 0.0
        self.off_timer: threading.Timer | None = None
        self.event_active = False
        # A generation counter prevents an old, cancelled timer callback from
        # clearing a newer activity event if both race during rescheduling.
        self.event_generation = 0

        self.mqtt_client = mqtt.Client(
            client_id=self.mqtt_client_id,
            clean_session=True,
        )

        if self.mqtt_username:
            self.mqtt_client.username_pw_set(
                self.mqtt_username,
                self.mqtt_password,
            )

        self.mqtt_client.will_set(
            self.availability_topic,
            payload="offline",
            qos=1,
            retain=True,
        )

        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

    def _on_mqtt_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        result_code: int,
    ) -> None:
        if result_code != 0:
            LOGGER.error(
                "MQTT connection failed with result code %s",
                result_code,
            )
            self.mqtt_connected.clear()
            return

        LOGGER.info(
            "Connected to MQTT broker %s:%s",
            self.mqtt_host,
            self.mqtt_port,
        )

        self.mqtt_connected.set()
        self._publish_discovery()

        client.publish(
            self.availability_topic,
            payload="online",
            qos=1,
            retain=True,
        )

        client.publish(
            self.event_topic,
            payload="ON" if self.event_active else "OFF",
            qos=1,
            retain=True,
        )

    def _on_mqtt_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        result_code: int,
    ) -> None:
        self.mqtt_connected.clear()

        if self.running:
            LOGGER.warning(
                "Disconnected from MQTT broker; result code %s",
                result_code,
            )

    def _publish_discovery(self) -> None:
        discovery_topic = (
            f"{self.discovery_prefix}/binary_sensor/"
            f"{self.device_identifier}/activity/config"
        )

        payload = {
            "name": self.entity_name,
            "unique_id": f"{self.device_identifier}_activity",
            "state_topic": self.event_topic,
            "availability_topic": self.availability_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": self.device_class,
            "force_update": True,
            "device": {
                "identifiers": [self.device_identifier],
                "name": self.device_name,
                "manufacturer": self.device_manufacturer,
                "model": self.device_model,
            },
        }

        self.mqtt_client.publish(
            discovery_topic,
            payload=json.dumps(payload),
            qos=1,
            retain=True,
        )

    def connect_mqtt(self) -> None:
        LOGGER.info(
            "Connecting to MQTT broker %s:%s",
            self.mqtt_host,
            self.mqtt_port,
        )

        self.mqtt_client.connect(
            self.mqtt_host,
            self.mqtt_port,
            keepalive=self.mqtt_keepalive_seconds,
        )
        self.mqtt_client.loop_start()

        if not self.mqtt_connected.wait(
            timeout=self.mqtt_connect_timeout_seconds
        ):
            raise RuntimeError(
                "MQTT did not connect successfully within "
                f"{self.mqtt_connect_timeout_seconds:g} seconds"
            )

    def start_capture(self) -> None:
        capture_filter = (
            f"src host {self.doorbell_ip} "
            f"and tcp dst port {self.capture_destination_port}"
        )

        # Accept either an absolute path or a command name resolvable through
        # PATH. Fail before creating the subprocess if tcpdump cannot be found.
        tcpdump_executable = self.tcpdump_path
        if not Path(tcpdump_executable).is_absolute():
            resolved = shutil.which(tcpdump_executable)
            if resolved is None:
                raise RuntimeError(
                    f"tcpdump was not found in PATH: {tcpdump_executable}"
                )
            tcpdump_executable = resolved
        elif not Path(tcpdump_executable).is_file():
            raise RuntimeError(
                f"tcpdump does not exist: {tcpdump_executable}"
            )

        command = [
            tcpdump_executable,
            "-i",
            self.interface,
            "-nn",
            "-s",
            "0",
            "-l",
            "-A",
            capture_filter,
        ]

        LOGGER.info(
            "Starting passive capture on %s for source %s",
            self.interface,
            self.doorbell_ip,
        )
        LOGGER.info(
            "Watching for TLS hostname %s",
            self.trigger_hostname.decode("ascii"),
        )

        self.capture_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        if self.capture_process.stdout is None:
            raise RuntimeError("tcpdump stdout pipe was not created")

        stdout_fd = self.capture_process.stdout.fileno()
        rolling_buffer = b""

        while self.running:
            ready, _, _ = select.select(
                [stdout_fd],
                [],
                [],
                self.capture_poll_seconds,
            )

            if not ready:
                return_code = self.capture_process.poll()

                if return_code is not None and self.running:
                    stderr_text = self._read_tcpdump_stderr()

                    raise RuntimeError(
                        f"tcpdump stopped with return code "
                        f"{return_code}: {stderr_text}"
                    )

                continue

            chunk = os.read(stdout_fd, self.capture_read_size)

            if not chunk:
                return_code = self.capture_process.poll()

                if return_code is not None and self.running:
                    stderr_text = self._read_tcpdump_stderr()

                    raise RuntimeError(
                        f"tcpdump stopped with return code "
                        f"{return_code}: {stderr_text}"
                    )

                continue

            rolling_buffer = (
                rolling_buffer + chunk.lower()
            )[-self.capture_buffer_size:]

            if self.trigger_hostname in rolling_buffer:
                self.handle_activity()
                rolling_buffer = b""

    def _read_tcpdump_stderr(self) -> str:
        if (
            self.capture_process is None
            or self.capture_process.stderr is None
        ):
            return ""

        return (
            self.capture_process.stderr.read()
            .decode("utf-8", errors="replace")
            .strip()
        )

    def handle_activity(self) -> None:
        if not self.running:
            return

        now_monotonic = time.monotonic()
        timestamp = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        if self.event_active:
            LOGGER.debug(
                "Extending active event after repeated activity"
            )
        elif (
            now_monotonic - self.last_trigger_monotonic
            < self.debounce_seconds
        ):
            LOGGER.debug(
                "Ignoring duplicate activity inside debounce window"
            )
            return
        else:
            self.last_trigger_monotonic = now_monotonic

            LOGGER.info(
                "REOLINK ACTIVITY DETECTED at %s",
                timestamp,
            )

            if not self.mqtt_connected.is_set():
                LOGGER.error(
                    "Activity detected, but MQTT is not connected"
                )
                return

            result = self.mqtt_client.publish(
                self.event_topic,
                payload="ON",
                qos=1,
                retain=True,
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                LOGGER.error(
                    "Failed to publish ON event; MQTT result %s",
                    result.rc,
                )
                return

            self.event_active = True

            LOGGER.info(
                "Published ON to MQTT topic %s",
                self.event_topic,
            )

        self.event_generation += 1
        generation = self.event_generation

        if self.off_timer is not None:
            self.off_timer.cancel()

        self.off_timer = threading.Timer(
            self.event_hold_seconds,
            self.publish_off,
            args=(generation,),
        )
        self.off_timer.daemon = True
        self.off_timer.start()

    def publish_off(self, generation: int) -> None:
        if generation != self.event_generation:
            return

        if not self.running:
            return

        self.off_timer = None
        self.event_active = False

        if not self.mqtt_connected.is_set():
            LOGGER.warning(
                "Cannot clear activity because MQTT is disconnected"
            )
            return

        LOGGER.info("Clearing Reolink activity state")

        result = self.mqtt_client.publish(
            self.event_topic,
            payload="OFF",
            qos=1,
            retain=True,
        )

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error(
                "Failed to publish OFF event; MQTT result %s",
                result.rc,
            )

    def stop_capture(self) -> None:
        if self.capture_process is None:
            return

        if self.capture_process.poll() is None:
            self.capture_process.terminate()

            try:
                self.capture_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.capture_process.kill()
                self.capture_process.wait(timeout=3)

        self.capture_process = None

    def stop(self) -> None:
        if not self.running:
            return

        LOGGER.info("Stopping passive observer")
        self.running = False

        if self.off_timer is not None:
            self.off_timer.cancel()
            self.off_timer = None

        # Stop packet capture before disconnecting MQTT. This prevents
        # buffered capture data from becoming a false event during shutdown.
        self.stop_capture()

        if self.mqtt_connected.is_set():
            self.mqtt_client.publish(
                self.event_topic,
                payload="OFF",
                qos=1,
                retain=True,
            )

            self.mqtt_client.publish(
                self.availability_topic,
                payload="offline",
                qos=1,
                retain=True,
            )

            time.sleep(0.2)

        self.mqtt_client.disconnect()
        self.mqtt_client.loop_stop()

    def run(self) -> None:
        self.connect_mqtt()

        while self.running:
            try:
                self.start_capture()
            except Exception:
                if not self.running:
                    break

                LOGGER.exception("Capture process failed")
                self.stop_capture()

                LOGGER.info(
                    "Restarting capture in %.1f seconds",
                    self.capture_restart_seconds,
                )
                time.sleep(self.capture_restart_seconds)


def parse_arguments() -> argparse.Namespace:
    """Read command-line options without hard-coding a deployment path."""
    parser = argparse.ArgumentParser(
        description=(
            "Observe mirrored Reolink battery-device traffic and publish "
            "short-lived activity through MQTT."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "JSON configuration path. Overrides "
            f"${CONFIG_ENVIRONMENT_VARIABLE} and the default config.json "
            "beside this script."
        ),
    )
    return parser.parse_args()


def determine_config_path(arguments: argparse.Namespace) -> Path:
    """
    Select the configuration path in explicit priority order.

    1. --config command-line argument
    2. REOLINK_OBSERVER_CONFIG environment variable
    3. config.json beside this script
    """
    if arguments.config is not None:
        return arguments.config.expanduser().resolve()

    environment_path = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    if environment_path:
        return Path(environment_path).expanduser().resolve()

    return DEFAULT_CONFIG_PATH


def load_config(config_path: Path) -> dict[str, Any]:
    """Load a JSON object and provide useful errors for common mistakes."""
    try:
        with config_path.open(
            "r",
            encoding="utf-8",
        ) as config_file:
            loaded = json.load(config_file)

    except FileNotFoundError:
        LOGGER.error(
            "Configuration file not found: %s",
            config_path,
        )
        raise

    except json.JSONDecodeError:
        # JSON does not permit // or # comments. Keep detailed explanatory
        # comments in docs/Configuration.md rather than config.json.
        LOGGER.exception(
            "Invalid JSON in %s",
            config_path,
        )
        raise

    if not isinstance(loaded, dict):
        raise ValueError(
            f"Configuration root must be a JSON object: {config_path}"
        )

    return loaded


def configure_logging(config: dict[str, Any] | None = None) -> None:
    """
    Configure console logging for direct execution or systemd/journald.

    INFO is used before loading the file so startup errors remain visible.
    Once loaded, log_level may replace it with DEBUG, WARNING, and so on.
    """
    level_name = "INFO"
    if config is not None:
        level_name = str(config.get("log_level", "INFO")).upper()

    numeric_level = getattr(logging, level_name, None)
    if not isinstance(numeric_level, int):
        raise ValueError(
            "log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )

    logging.basicConfig(
        level=numeric_level,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
        force=True,
    )


def validate_config(config: dict[str, Any]) -> None:
    """
    Reject missing or plainly unsafe values before starting network services.

    Validation remains intentionally understandable rather than depending on
    an external schema package. More detailed operational guidance lives in
    docs/Configuration.md.
    """
    required_strings = (
        "capture_interface",
        "doorbell_ip",
        "trigger_hostname",
        "mqtt_host",
        "mqtt_topic",
        "availability_topic",
        "device_name",
        "device_identifier",
    )

    for key in required_strings:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Required configuration setting {key!r} "
                "must be a non-empty string"
            )

    numeric_positive = (
        "mqtt_port",
        "mqtt_keepalive_seconds",
        "mqtt_connect_timeout_seconds",
        "capture_destination_port",
        "capture_read_size",
        "capture_buffer_size",
        "capture_poll_seconds",
        "capture_restart_seconds",
        "event_hold_seconds",
        "debounce_seconds",
    )

    for key in numeric_positive:
        if key in config:
            value = config[key]
            if isinstance(value, bool):
                raise ValueError(
                    f"Configuration setting {key!r} must be numeric"
                )
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Configuration setting {key!r} must be numeric"
                ) from exc
            if number < 0:
                raise ValueError(
                    f"Configuration setting {key!r} cannot be negative"
                )


def main() -> int:
    """Load configuration, install signal handlers, and run the observer."""
    configure_logging()

    arguments = parse_arguments()
    config_path = determine_config_path(arguments)

    LOGGER.info("Using configuration file %s", config_path)

    try:
        config = load_config(config_path)
        validate_config(config)
        configure_logging(config)
        observer = ReolinkPassiveObserver(config)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        LOGGER.error("Observer startup failed: %s", exc)
        return 2

    def stop_handler(
        signum: int,
        frame: Any,
    ) -> None:
        # SIGTERM is used by systemd; SIGINT is used by Ctrl+C during testing.
        LOGGER.info(
            "Received signal %s",
            signum,
        )
        observer.stop()

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )
    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    try:
        observer.run()
    except Exception:
        LOGGER.exception(
            "Observer stopped because of an unrecoverable error"
        )
        return 1
    finally:
        observer.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
