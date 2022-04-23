#!/usr/bin/env python3
"""Host Statistic Information to MQTT."""
__author__ = "Diogo Gomes"
__version__ = "0.0.3"
__email__ = "diogogomes@gmail.com"

import argparse
import json
import logging
import platform
import sched
import time
from datetime import datetime
from decimal import Decimal, getcontext

import paho.mqtt.client as mqtt
import psutil
import yaml
from slugify import slugify
from yaml import Dumper, Loader

getcontext().prec = 2

MQTT_BASE_TOPIC = f"ps2mqtt/{slugify(platform.node())}"
MQTT_PS2MQTT_STATUS = "{}/status"
MQTT_STATE_TOPIC = "{}/{}"
MQTT_AVAILABLE = "online"
MQTT_NOT_AVAILABLE = "offline"
HA_DISCOVERY_PREFIX = "{}/sensor/ps2mqtt_{}/{}/config"

OPTIONAL_ATTR = ["device_class", "icon", "unit_of_measurement"]

log_format = "%(asctime)s %(levelname)s: %(message)s"
logging.basicConfig(format=log_format, level=logging.DEBUG)
logger = logging.getLogger(__name__)

last = {}


def rate(key, value):
    """Calculate running rates."""
    rate = 0
    now = time.time()
    if key in last:
        ltime, lvalue = last[key]
        rate = Decimal(value - lvalue) / Decimal(now - ltime)
    last[key] = now, value

    return float(rate)


def load_properties():
    """Define which properties to publish."""
    properties = {
        "cpu_percent": {
            "unit_of_measurement": "%",
            "icon": "mdi:chip",
            "call": lambda: psutil.cpu_percent(interval=None),
        },
        "virtual_memory": {
            "unit_of_measurement": "%",
            "icon": "mdi:memory",
            "call": lambda: psutil.virtual_memory().percent,
        },
        "uptime": {
            "device_class": "timestamp",
            "call": lambda: datetime.fromtimestamp(psutil.boot_time())
            .astimezone()
            .isoformat(),
        },
        "bytes_sent": {
            "unit_of_measurement": "MiB",
            "icon": "mdi:upload-network",
            "call": lambda: int(psutil.net_io_counters().bytes_sent / 1000000),
        },
        "bytes_recv": {
            "unit_of_measurement": "MiB",
            "icon": "mdi:download-network",
            "call": lambda: int(psutil.net_io_counters().bytes_recv / 1000000),
        },
        "upload": {
            "unit_of_measurement": "kbps",
            "icon": "mdi:upload-network",
            "call": lambda: rate("upload", psutil.net_io_counters().bytes_sent / 1000),
        },
        "download": {
            "unit_of_measurement": "kbps",
            "icon": "mdi:download-network",
            "call": lambda: rate(
                "download", psutil.net_io_counters().bytes_recv / 1000
            ),
        },
    }

    if hasattr(psutil, "sensors_temperatures"):
        for temp_sensor in psutil.sensors_temperatures():
            properties[temp_sensor] = {
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "call": lambda: psutil.sensors_temperatures()[temp_sensor][0].current,
            }

    return properties


def gen_ha_config(sensor, properties, base_topic):
    """Generate Home Assistant Configuration."""
    json_config = {
        "name": sensor,
        "unique_id": slugify(f"{platform.node()} {sensor}"),
        "object_id": slugify(f"{platform.node()} {sensor}"),
        "state_topic": MQTT_STATE_TOPIC.format(base_topic, sensor),
        "availability_topic": MQTT_PS2MQTT_STATUS.format(base_topic),
        "payload_available": MQTT_AVAILABLE,
        "payload_not_available": MQTT_NOT_AVAILABLE,
        "device": {
            "identifiers": f"{platform.node()}_ps2mqtt",
            "name": f"{platform.node()}",
            "sw_version": platform.platform(),
            "model": platform.system(),
            "manufacturer": f"ps2mqtt {__version__}",
        },
    }
    for attr in OPTIONAL_ATTR:
        if attr in properties[sensor]:
            json_config[attr] = properties[sensor][attr]

    return json.dumps(json_config)


def status(mqttc, properties, s, period, base_topic):
    """Publish status and schedule the next."""
    for p in properties.keys():
        mqttc.publish(MQTT_STATE_TOPIC.format(base_topic, p), properties[p]["call"]())
    s.enter(period, 1, status, (mqttc, properties, s, period, base_topic))


def on_connect(client, userdata, flags, result):
    """MQTT Connect callback."""

    properties, ha_prefix, base_topic = userdata

    client.publish(MQTT_PS2MQTT_STATUS.format(base_topic), MQTT_AVAILABLE, retain=True)
    for p in properties.keys():
        logger.debug("Adding %s", p)
        client.publish(
            HA_DISCOVERY_PREFIX.format(ha_prefix, slugify(platform.node()), p),
            gen_ha_config(p, properties, base_topic),
            retain=True,
        )


def main():
    """Start main daemon."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="configuration file", default="config.yaml")
    parser.add_argument(
        "--period", help="updates period in seconds", type=int, default=60
    )
    parser.add_argument("--mqtt-server", help="MQTT server", default="localhost")
    parser.add_argument("--mqtt-port", help="MQTT port", type=int, default=1883)
    parser.add_argument(
        "--mqtt-base-topic", help="MQTT base topic", default=MQTT_BASE_TOPIC
    )
    parser.add_argument(
        "--ha-discover-prefix", help="HA discover mqtt prefix", default="homeassistant"
    )

    args = parser.parse_args()

    config = {
        "mqtt_server": args.mqtt_server,
        "mqtt_port": args.mqtt_port,
        "mqtt_base_topic": args.mqtt_base_topic,
        "ha_discover_prefix": args.ha_discover_prefix,
    }

    try:
        with open(args.config, "r") as stream:
            logger.debug("Loading configuration from <%s>", args.config)
            config = yaml.load(stream, Loader=Loader)

        properties = load_properties()

        logger.debug("Connecting to %s:%s", config["mqtt_server"], config["mqtt_port"])
        mqttc = mqtt.Client(
            client_id=slugify(f"ps2mqtt {platform.node()}"),
            userdata=(
                properties,
                config["ha_discover_prefix"],
                config["mqtt_base_topic"],
            ),
        )
        mqttc.will_set(
            MQTT_PS2MQTT_STATUS.format(config["mqtt_base_topic"]),
            MQTT_NOT_AVAILABLE,
            retain=True,
        )
        mqttc.on_connect = on_connect

        mqttc.connect(config["mqtt_server"], config["mqtt_port"], 60)

        mqttc.loop_start()

        s = sched.scheduler(time.time, time.sleep)
        status(mqttc, properties, s, config["period"], config["mqtt_base_topic"])

        s.run()

    except FileNotFoundError as e:
        logger.info("Configuration file %s created, please reload daemon", args.config)
    except KeyError as e:
        missing_key = e.args[0]
        config[missing_key] = args.__dict__[missing_key]
        logger.info("Configuration file updated, please reload daemon")
    finally:
        with open(args.config, "w", encoding="utf8") as outfile:
            yaml.dump(
                config,
                outfile,
                default_flow_style=False,
                allow_unicode=True,
                Dumper=Dumper,
            )


if __name__ == "__main__":
    main()