#! /usr/bin/env python3
#
# Experiments with talking to Rollease Acmeda hub over RS485.
# Not very tidy yet.
#
# Requires Python 3.7 or later

import asyncio
import rollease
import logging
import os
import sys
import time

import configargparse

from hbmqtt.client import MQTTClient, ClientException
from hbmqtt.mqtt.constants import QOS_1, QOS_2

from typing import Optional, Tuple, List, Dict

# Many of these are default settings that can be overridden
# in the config, through command line, or with environment
# variables.

DEVICE = "/dev/ttyUSB0"
MQTT_URL = "mqtt://user:password@localhost"
MQTT_TOPIC_ROOT = "home-assistant/cover"  # followed by /motor_addr/command
MQTT_COMMAND_TOPIC = "set"
MQTT_POSITION_TOPIC = "position"
MQTT_SET_POSITION_TOPIC = "set_position"

logging.basicConfig(format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


async def monitor_mqtt_requests(hub, mqtt_client, options):
    # Subscribe to mqtt topics for motors

    topics = [
        (f"{options.mqtt_topic_root}/{motor_addr}/{options.mqtt_command_topic}", QOS_1)
        for motor_addr in hub.motors
    ]
    + [
        (f"{options.mqtt_topic_root}/{motor_addr}/{options.mqtt_set_position_topic}", QOS_1)
        for motor_addr in hub.motors
    ]
    log.info("Subscribing to MQTT topics:")
    for t in topics:
        log.info("  " + t)
    await mqtt_client.subscribe(topics)

    while True:
        log.debug("Waiting for MQTT messages")

        # wait for an MQTT message on our topics
        message = await mqtt_client.deliver_message()
        packet = message.publish_packet
        topic = packet.variable_header.topic_name
        payload = packet.payload.data.decode()

        log.info("MQTT received %s => %s", topic, payload)
        if topic.startswith(options.mqtt_topic_root):
            motor_addr, subtopic = topic[(len(options.mqtt_topic_root) + 1):].split("/")
            log.info(f"  motor {motor_addr} subtopic {subtopic}")
            if motor_addr in hub.motors:
                motor = hub.motors[motor_addr]
                if subtopic == options.mqtt_command_topic:
                    if payload == "CLOSE":
                        await motor.request_close()
                    elif payload == "OPEN":
                        await motor.request_open()
                    elif payload == "STOP":
                        await motor.request_stop()
                elif subtopic == options.mqtt_set_position_topic:
                    await motor.request_move_percent(int(payload))
                else:
                    log.warning("Unexpected topic: %s, payload %s", topic, payload)
            else:
                log.warning("Request for unknown motor {motor}")
        else:
            log.error("Topic not under expected topic root")


async def update_mqtt_positions(hub, mqtt_client, options):
    # Update the positions once per minute
    while True:
        await asyncio.sleep(60)

        for motor_addr in hub.motors:
            travel_pc = hub.motors[motor_addr].travel_pc
            if travel_pc is not None:
                position = str(travel_pc).encode()
                topic = f"{options.mqtt_topic_root}/{motor_addr}/{options.mqtt_position_topic}"
                log.debug("  Sending position %s to topic %s", position, topic)
                await mqtt_client.publish(topic, position)


async def main():

    # Handle the configuration - see ConfigArgParse docs
    # for details of how things can be specified.
    parser = configargparse.ArgParser(
        default_config_files=[
            '/etc/rollease2mqtt.conf', 
            'rollease2mqtt.conf'
        ],
        config_file_parser_class = configargparse.YAMLConfigFileParser,
        formatter_class = configargparse.ArgumentDefaultsHelpFormatter 
    )
    parser.add(
        '-c', '--config', 
        required=False, is_config_file=True, 
        help='alternative config file'
    )
    parser.add(
        '-d', '--device',
        default=DEVICE,
        help="RS485 serial device"
    )
    parser.add(
        '-m', '--mqtt_url',
        default=MQTT_URL,
        help="mqtt: URL, possibly including username & password"
    )
    parser.add(
        '-t', '--mqtt_topic_root',
        default=MQTT_TOPIC_ROOT,
        help="MQTT topic root."
    )
    parser.add(
        '-tc', '--mqtt_command_topic',
        default=MQTT_COMMAND_TOPIC,
        help="MQTT command topic, under [topic_root]/[motor]/."
    )
    parser.add(
        '-ts', '--mqtt_set_position_topic',
        default=MQTT_SET_POSITION_TOPIC,
        help="MQTT setposition topic, under [topic_root]/[motor]/."
    )
    parser.add(
        '-tp', '--mqtt_position_topic',
        default=MQTT_POSITION_TOPIC,
        help="MQTT position-reporting topic, under [topic_root]/[motor]/."
    ) 
    
    options = parser.parse_args()

    ## Now connect to MQTT

    mqtt_client = MQTTClient(client_id="rollease2mqtt")
    log.info("Connecting to MQTT broker on %s", options.mqtt_url)
    await mqtt_client.connect(options.mqtt_url)

    # What shall we do when we get news from the hub?

    async def update_callback(hub: rollease.Hub, motor: rollease.Motor):
        travel_pc = motor.travel_pc
        if travel_pc is not None:
            position = str(travel_pc).encode()
            topic = f"{options.mqtt_topic_root}/{motor.addr}/{options.mqtt_position_topic}"
            log.debug("Sending updated position %s to topic %s", position, topic)
            await mqtt_client.publish(topic, position)

    # Now connect to the hub

    log.info("Connecting to hub using device %s", options.device)
    conn = rollease.AcmedaConnection(device=options.device, callback=update_callback)
    await conn.request_hub_info()

    # Give the system a chance to read initial data
    log.info("Pausing for 10 secs to allow blinds to respond")
    await asyncio.sleep(10)
    hub_addr, hub = next(iter(conn.hubs.items()))

    asyncio.create_task(update_mqtt_positions(hub, mqtt_client, options))
    asyncio.create_task(monitor_mqtt_requests(hub, mqtt_client, options))

    while True:
        log.info("rollease2mqtt alive and waiting")
        await asyncio.sleep(300)


asyncio.run(main())
