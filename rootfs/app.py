#!/usr/bin/env python3

# Import our class
from foscam2mqtt import Foscam2MQTT

from flask import Flask, request as req, Response as res, json
from waitress.server import create_server

import logging

import argparse as ap
from datetime import datetime as dt
from signal import signal, Signals, SIGTERM, SIGINT

parser=ap.ArgumentParser()
parser.add_argument('--listen-address', type=str, default='0.0.0.0', help='Listening address (default: 0.0.0.0)')
parser.add_argument('--listen-port', type=int, default=5555, help='Listening port (default: 5555)')
parser.add_argument('--listen-url', required=True, type=str, help='URL we should advertise to Foscam device')
parser.add_argument('--obfuscate', action='store_true', help='Obfuscate webhook actions')
parser.add_argument('--paranoid', action='store_true', help='Cycle obfuscated webhook action after each trigger')
parser.add_argument('--foscam-host', type=str, help='Foscam VD1 IP/hostname to connect to for auto-configuration of webhooks')
parser.add_argument('--foscam-port', type=int, default=88, help='Foscam VD1 port to connect to (default: 88)')
parser.add_argument('--foscam-ssl', action='store_true', help='Enable SSL encryption on Foscam connection (default: false, use --foscam-port 443 for this)')
parser.add_argument('--foscam-user', type=str, default='admin', help='Username to use for connecting to Foscam device')
parser.add_argument('--foscam-pass', type=str, help='Password to use for connecting')
parser.add_argument('--mqtt-host', type=str, default='localhost', help='MQTT server to connect to')
parser.add_argument('--mqtt-port', type=int, default=1883, help='MQTT TCP port to connect to (default: 1883)')
parser.add_argument('--mqtt-ssl', action='store_true', help='Enable SSL encryption on MQTT connection (default: false)')
parser.add_argument('--mqtt-user', type=str, help='Username to use for connecting (default: none)')
parser.add_argument('--mqtt-pass', type=str, help='Password to use for connecting (only used when username is specified)')
parser.add_argument('--mqtt-topic', type=str, default='foscam2mqtt', help='MQTT topic to publish to (default: foscam2mqtt)')
parser.add_argument('--mqtt-client-id', type=str, default='foscam2mqtt', help='MQTT client ID to use for connecting (default: foscam2mqtt)')
parser.add_argument('--ha-discovery', action='store_true', help='Enable publishing to Home Assistant discovery topic (default: false)')
parser.add_argument('--ha-discovery-topic', type=str, default='homeassistant', help='MQTT topic to publish HA discovery information to (default: homeassistant)')
parser.add_argument('--ha-device-name', type=str, default='Foscam VD1', help='Friendly name of the entities to publish in HA (default: Foscam VD1)')
parser.add_argument('--ha-cleanup', action='store_true', help='Remove HA sensor config on exit (default: false)')
parser.add_argument('--deepstack-face', action='store_true', help='Send image to Deepstack on face detection for face recognition (default: false)')
parser.add_argument('--deepstack-object', action='store_true', help='Send image to Deepstack on motion detection for object detection (default: false)')
parser.add_argument('--deepstack-url', type=str, default='http://localhost:5000', help='The URL where Deepstack can be found')
parser.add_argument('--deepstack-api-key', type=str, help='API key to authenticate against Deepstack (default: none)')
parser.add_argument('--quiet', action='store_true', help='Only show error and critical messages in console (default: false)')
parser.add_argument('--log-level', type=str, default='warning', choices=['debug','info','warning','error'], help='Log level (default: warning)')
parser.add_argument('--date-format', type=str, default='%Y-%m-%d %H:%M:%S', help='Date/time format for logging (strftime template)')
config=parser.parse_args()

## Enable logging
log_level = getattr(logging, config.log_level.upper())
log_file = f"/log/foscam2mqtt_{dt.strftime(dt.now(), '%Y%m%d_%H%M%S')}.log"
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
log_date_format = config.date_format
logging.basicConfig(filename=log_file, filemode='w', level=log_level, format=log_format, datefmt=log_date_format)
formatter = logging.Formatter(fmt=log_format, datefmt=log_date_format)

log = logging.getLogger('foscam2mqtt')
#log.setFormatter(formatter)
log.setLevel(log_level)

# Create console handler
ch = logging.StreamHandler()
ch.setFormatter(formatter)

# If quiet is set, only log error and critical to console
if config.quiet:
    ch.setLevel(logging.ERROR)
else:
    ch.setLevel(log_level)

# Add ch to logger
log.addHandler(ch)

log.info(f"Log level: {config.log_level.upper()}")
log.info(f"Listening on {config.listen_address}:{str(config.listen_port)}")

foscam = Foscam2MQTT(listen_url = config.listen_url, obfuscate = config.obfuscate, paranoid = config.paranoid)
foscam.date_format = config.date_format
foscam.foscam_host = config.foscam_host
foscam.foscam_port = config.foscam_port
foscam.foscam_user = config.foscam_user
foscam.foscam_pass = config.foscam_pass

foscam.deepstack_url = config.deepstack_url
foscam.deepstack_api_key = config.deepstack_api_key

foscam.update_hooks()

# Build MQTT config
mqtt_config = {
    'host': config.mqtt_host,
    'port': config.mqtt_port,
    'client_id': config.mqtt_client_id,
    'topic': config.mqtt_topic,
}

# Add username/password if they're specified
if config.mqtt_user: mqtt_config.update({ 'username': config.mqtt_user, 'password': config.mqtt_pass })

# Initialize MQTT client with our config
log.debug('Initialize MQTT')
foscam.mqtt_init(**mqtt_config)

if config.ha_discovery:
    foscam.ha_discovery = config.ha_discovery
    foscam.ha_discovery_topic = config.ha_discovery_topic
    foscam.ha_device_name = config.ha_device_name
    foscam.mqtt_publish_ha_entities()

# Create snapshot for starters
foscam.mqtt_publish('snapshot', foscam.snapshot())
foscam.mqtt_publish('snapshot/datetime', dt.strftime(dt.now(), foscam.date_format))

foscam.mqtt_client.loop_start()

# Define Flask web app
app = Flask(__name__)

@app.route('/', methods=['GET', 'PUT', 'POST'])
def webhook():
    ct = req.content_type
    if req.method == 'GET':
        req_args = req.args
    elif req.method == 'POST' and ct == 'application/x-www-form-urlencoded':
        req_args = req.form
    elif req.method == 'POST' and ct == 'application/json':
        req_args = req.json

    date_time = dt.strftime(dt.now(), foscam.date_format)

    if 'action' in req_args:
        action = req_args['action']
    else:
        log.warning(f"No action specified - {req.remote_addr}")
        response = f"{date_time} ERROR - no action specified"
        return res(response=response, status=400)

    verified_action = foscam.verify_action(action)
    if not verified_action:
        log.warning(f"Unknown action {action} - {req.remote_addr}")
        response = f"{date_time} ERROR - unknown action"
        return res(response = response, status = 400)
    else:
        action = verified_action

    log.info(f"{req.method} {action} - {req.remote_addr}")

    image_data = foscam.snapshot()

    foscam.mqtt_publish('action', action, retain=False)
    foscam.mqtt_publish(f"{action}_datetime", dt.strftime(dt.now(), foscam.date_format))

    foscam.mqtt_publish('snapshot', image_data)
    foscam.mqtt_publish('snapshot/datetime', dt.strftime(dt.now(), foscam.date_format))

    if foscam.ha_discovery:
        foscam.mqtt_publish(f"{action}/trigger", foscam.trigger_payload, retain = False)

    if config.deepstack_face and verified_action in ['face', 'button']:
        foscam.deepstack_face(image_data, verified_action)

    elif config.deepstack_object and verified_action in ['motion', 'sound']:
        foscam.deepstack_object(image_data, verified_action)

    if foscam.obfuscate and foscam.paranoid:
        log.info('Paranoid enabled, cycling webhook')
        foscam.update_hooks(action = action)

    response = f"{date_time} OK"
    return res(response = response, status = 200)

def on_signal(x, y):
    log.debug(f"{Signals(x).name} received")
    app_server.close()

signal(SIGTERM, on_signal)
signal(SIGINT, on_signal)

app_server = create_server(app, host = config.listen_address, port = config.listen_port)
try:
    app_server.run()
except OSError:
    log.debug('Caught expected error on process termination')

# Set state to unavailable
foscam.mqtt_disconnect()