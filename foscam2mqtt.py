#!/usr/bin/env python3
from flask import Flask, request as req, Response as res, json
from waitress.server import create_server

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

import logging

from base64 import b64encode as b64enc, b64decode as b64dec
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
from urllib.parse import unquote
from json import loads as json_loads, dumps as json_dumps, JSONDecodeError
from xmltodict import parse as xmlparse

import argparse as ap
import random as rnd
import string
from datetime import datetime as dt
from signal import signal, Signals, SIGTERM, SIGINT

parser=ap.ArgumentParser()
parser.add_argument('--listen-address', type=str, default='0.0.0.0', help='Listening address (default: 0.0.0.0)')
parser.add_argument('--listen-port', type=int, default=5000, help='Listening port (default: 5000)')
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

class Foscam2MQTT:
    def __init__(self, listen_url = None, obfuscate = False, paranoid = False, quiet = False):
        # Own settings
        self.actions = 'button','motion','sound','face','human' #,'alarm'
        self.listen_url = listen_url
        self.paranoid = False
        self.date_format = '%Y-%m-%dT%H:%M:%SZ'
        self.obfuscate = obfuscate

        if self.obfuscate:
            self.action_keys = dict()
            self.paranoid = paranoid
            self.trigger_payload = ''.join(rnd.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=8))
        else:
            self.trigger_payload = 1

        # Foscam settings
        self.foscam_host = None
        self.foscam_port = None
        self.foscam_user = None
        self.foscam_pass = None

        # MQTT settings
        self.mqtt_host = 'localhost'
        self.mqtt_port = 1883
        self.mqtt_user = None
        self.mqtt_pass = None
        self.mqtt_client = None
        self.mqtt_client_id = 'foscam2mqtt'
        self.mqtt_settings = None
        self.mqtt_topic = 'foscam2mqtt'
        self.ha_discovery = False
        self.ha_discovery_topic = 'homeassistant'
        self.ha_device_name = 'Foscam VD1'

        log.info('Foscam2MQTT initialized')

    def verify_action(self, action):
        if self.obfuscate and action in (self.action_keys.keys()):
            return self.action_keys[action]
        elif not self.obfuscate and action in self.actions:
            return action
        else:
            return False

    def invoke_foscam(self, cmd, options = None, return_response = False):
        foscam_url = f"http://{self.foscam_host}:{str(self.foscam_port)}/cgi-bin/CGIProxy.fcgi?usr={self.foscam_user}&pwd={self.foscam_pass}&cmd={cmd}"
        if options:
            for key,value in options.items():
                foscam_url += f"&{key}={value}"
        try:
            response = requests.get(foscam_url, timeout = 15, verify = False)
            response.raise_for_status()
        except requests.exceptions.HTTPError as errh:
            if response.status_code == 404:
                log.warning('Remote server returned HTTP error code 404')
            else:
                log.warning(errh)
            return False
        except req.exceptions.ConnectionError as errc:
            log.warning(errc)
            return False
        except req.exceptions.Timeout as errt:
            log.warning(errt)
            return False
        except req.exceptions.RequestException as err:
            log.warning(err)
            return False
        if return_response: return response.content

    def snapshot(self):
        snapshot = self.invoke_foscam(cmd = 'snapPicture2', return_response = True)
        return snapshot

    def update_hooks(self, triggered_action = None):
        if self.listen_url is None:
            self.listen_url = 'http://localhost:5000/'
            log.debug(f"Assuming default listen url: {self.listen_url}")
        action_aliases = dict({'button':'BKLinkUrl','motion':'MDLinkUrl','sound':'SDLinkUrl','face':'FaceLinkUrl','human':'HumanLinkUrl'}) #,'alarm':'AlarmUrl'})
        # If an action_name was specified, only update that one.
        foscam_options = dict()
        alarm_urls = xmlparse(self.invoke_foscam(cmd='getAlarmHttpServer', return_response=True))['CGI_Result']

        for action_name, alias in action_aliases.items():
            if self.obfuscate:
                if triggered_action is None or triggered_action == action_name:
                    rnd.seed()
                    random_string = ''.join(rnd.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=24))
                    self.action_keys[random_string] = action_name
                    action = random_string
                else:
                    alarm_url_enc = unquote(alarm_urls[alias])
                    alarm_url_byte = b64dec(alarm_url_enc)
                    alarm_url = alarm_url_byte.decode('ascii')
                    log.debug(f"Retrieved existing URL for action {action}: {alarm_url}")
                    if self.listen_url in alarm_url:
                        action = alarm_url.replace((f"{self.listen_url}?action="), '')
                        log.debug(f"Extracted action: {action}")
                    else:
                        action = 'none'
            else:
                action = action_name
            action_url = f"{self.listen_url}?action={action}"
            log.debug(f"Generated URL for {action_name} - {action_url}")
            encoded_url = b64enc(action_url.encode('ascii'))
            foscam_options[alias] = encoded_url.decode('ascii')
        self.invoke_foscam(cmd = 'setAlarmHttpServer', options = foscam_options, return_response = True)

    def mqtt_gen_topic(self, sub_topic, main_topic = None):
        if main_topic is None: main_topic = self.mqtt_topic
        return f"{main_topic}/{sub_topic}"

    def mqtt_init(self, host, port = 1883, ssl = False, username = None, password = None, client_id = 'foscam2mqtt', topic = 'foscam2mqtt'):
        log.info(f"Initializing MQTT client with ID {client_id}")
        self.mqtt_client_id = client_id
        self.mqtt_client = mqtt.Client(protocol=mqtt.MQTTv311, client_id = client_id, clean_session = False)

        self.mqtt_host = host
        self.mqtt_port = port

        self.mqtt_topic = topic
        log.info(f"MQTT base topic is {topic}")

        self.mqtt_settings = {
            'protocol':  mqtt.MQTTv311,
            'client_id': client_id,
            'hostname': host,
            'port': port,
        }

        if username:
            self.mqtt_user = username
            self.mqtt_pass = password
            self.mqtt_client.username_pw_set(username, password)
            self.mqtt_settings['auth'] = {'username': username, 'password': password}

        if self.ha_discovery:
            log.info(f"Camera name is {self.ha_device_name}")

        self.mqtt_client.on_connect = self.mqtt_on_connect
        self.mqtt_client.on_message = self.mqtt_on_message

        log.info(f"Connect to MQTT broker {self.mqtt_host}:{str(self.mqtt_port)}")
        self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('snapshot/update')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('snapshot/update'), self.mqtt_on_snapshot_update)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('ring_volume/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('ring_volume/set'), self.mqtt_on_ring_volume_set)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('status_led/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('status_led/set'), self.mqtt_on_status_led_set)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('image/hdr/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/hdr/set'), self.mqtt_on_image_hdr_set)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('image/mirror/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/mirror/set'), self.mqtt_on_image_mirror_set)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('image/flip/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/flip/set'), self.mqtt_on_image_flip_set)

        log.debug(f"Add callback for topic {self.mqtt_gen_topic('reboot')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('reboot'), self.mqtt_on_reboot)

    def mqtt_disconnect(self):
        log.debug(f"Publish 0 to topic {self.mqtt_gen_topic('$state')}")
        self.mqtt_publish('$state', 0, qos = 2)
        log.info('Disconnect')
        self.mqtt_client.disconnect()

    def mqtt_publish(self, topic, payload, qos = 0, retain = True):
        if not self.mqtt_client:
            log.error('MQTT not initialized, run self.mqtt_init first!')
            return False

        topic = self.mqtt_gen_topic(topic)

        self.mqtt_client.publish(topic, payload, qos = qos, retain = retain)

        if log.level <= logging.DEBUG:
            if type(payload) is int:
                payload = str(payload)
            elif type(payload) is bytes:
                payload = f"of {str(len(payload))} bytes"
            elif type(payload) is not str:
                payload = f"of type {type(payload).__name__}"
            log.debug(f"Published payload {payload} to topic {topic}")

    def mqtt_gen_ha_entity(self, action, entity_type, availability_topic = None, name = None, params = None, device = None, icon = 'mdi:help-box'):
        unique_id = f"{self.mqtt_topic}_{action}"
        log.debug(f"Generated unique_id {unique_id}")

        # Set a sensible default for availability
        if not availability_topic:
            availability_topic = self.mqtt_gen_topic('$state')

        if not name:
            name = f"{self.ha_device_name} {action}"

        config_topic = f"{self.ha_discovery_topic}/{entity_type}/{unique_id}/config"

        # Create generic base
        entity = {
            'uniq_id': unique_id,
            'obj_id': unique_id,
            'name': name,
            'avty_t': self.mqtt_gen_topic('$state'),
            'pl_avail': 1,
            'pl_not_avail': 0,
        }

        # Add icon except for specific types
        if entity_type not in ['device_trigger']: entity['ic'] = icon

        # Add type specific params if specified
        if params: entity.update(params)

        if device: entity['dev'] = device
        entity_json = json_dumps(entity)
        log.debug(f"{action} entity_json {entity_json}")

        msg = {
            'topic': config_topic,
            'payload': entity_json,
            'qos': 0,
            'retain': True,
        }
        return msg

    def mqtt_publish_ha_entities(self):
        msgs = []

        dev_info = xmlparse(self.invoke_foscam(cmd='getDevInfo', return_response=True))['CGI_Result']

        device = {
            'manufacturer': 'Foscam',
            'model': 'VD1',
            'name': self.ha_device_name,
            'identifiers': [self.mqtt_topic],
            'sw_version': f"{dev_info['hardwareVer']}/{dev_info['firmwareVer']}",
        }

        # Publish raw image data to snapshot topic as camera entity
        action = 'snapshot'
        params = {
            'ic': 'mdi:doorbell-video',
            't': self.mqtt_gen_topic(action),
        }
        msg = self.mqtt_gen_ha_entity(action = action, entity_type = 'camera', device = device, params = params)
        msgs.append(msg)

        # Create sensor to show snapshot age
        params = { 'ic': 'mdi:clock', 'val_tpl': f"{{{{ strptime(value, '{self.date_format}') }}}}", 'stat_t': self.mqtt_gen_topic('snapshot/datetime') }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} snapshot date/time", action = 'snapshot_datetime', entity_type = 'sensor', device = device, params = params)
        msgs.append(msg)

        # Create sensor for last action
        action = 'action'
        params = { 'ic': 'mdi:bell-alert', 'stat_t': self.mqtt_gen_topic(action) }
        msg = self.mqtt_gen_ha_entity(action = action, entity_type = 'sensor', device = device, params = params)
        msgs.append(msg)

        # Publish timestamp of last action to its own topic
        for action in self.actions:
            params = {
                'ic': 'mdi:clock',
                'val_tpl': f"{{{{ strptime(value, '{self.date_format}') }}}}",
                'stat_t': self.mqtt_gen_topic(f"{action}_datetime"),
            }
            msg = self.mqtt_gen_ha_entity(action = action, entity_type = 'sensor', device = device, params = params)
            msgs.append(msg)

            # Publish trigger topic, this will receive button presses
            params = {
                't': self.mqtt_gen_topic(f"{action}/trigger"),
                'pl': self.trigger_payload,
                'atype': 'trigger',
                'type': 'trigger',
                'stype': action,
            }
            msg = self.mqtt_gen_ha_entity(action = f"trigger_{action}", entity_type = 'device_automation', device = device, params = params)
            msgs.append(msg)

        # Status LED switch
        action = 'status_led'
        params = {
            'ic': 'mdi:led-on',
            'stat_t': self.mqtt_gen_topic(action),
            'cmd_t': self.mqtt_gen_topic(f"{action}/set"),
            'pl_on': 1,
            'pl_off': 0,
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} status LED", action = action, entity_type = 'switch', device = device, params = params)
        msgs.append(msg)

        # HDR switch
        action = 'image_hdr'
        params = {
            'ic': 'mdi:camera-enhance',
            'stat_t': self.mqtt_gen_topic('image/hdr'),
            'cmd_t': self.mqtt_gen_topic('image/hdr/set'),
            'pl_on': 1,
            'pl_off': 0,
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} HDR", action = action, entity_type = 'switch', device = device, params = params)
        msgs.append(msg)

        # Mirror image switch
        action = 'image_mirror'
        params = {
            'ic': 'mdi:flip-horizontal',
            'stat_t': self.mqtt_gen_topic('image/mirror'),
            'cmd_t': self.mqtt_gen_topic('image/mirror/set'),
            'pl_on': 1,
            'pl_off': 0,
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} image mirror", action = action, entity_type = 'switch', device = device, params = params)
        msgs.append(msg)

        # Vertical image flip switch
        action = 'image_flip'
        params = {
            'ic': 'mdi:flip-vertical',
            'stat_t': self.mqtt_gen_topic('image/flip'),
            'cmd_t': self.mqtt_gen_topic('image/flip/set'),
            'pl_on': 1,
            'pl_off': 0,
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} image flip", action = action, entity_type = 'switch', device = device, params = params)
        msgs.append(msg)

        # Snapshot button
        action = 'update_snapshot'
        params = { 'ic': 'mdi:camera', 'cmd_t': self.mqtt_gen_topic('snapshot/update'), 'cmd_tpl': f"{{{{ now().strftime('{self.date_format}') }}}}" }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} update snapshot", action = action, entity_type = 'button', device = device, params = params)
        msgs.append(msg)

        action = 'reboot'
        params = { 'ic': 'mdi:restart-alert', 'cmd_t': self.mqtt_gen_topic(action), 'cmd_tpl': f"{{{{ now().strftime('{self.date_format}') }}}}" }
        msg = self.mqtt_gen_ha_entity(action = action, entity_type = 'button', device = device, params = params)
        msgs.append(msg)

        action = 'ring_volume'
        params = {
            'ic': 'mdi:bell-ring',
            'stat_t': self.mqtt_gen_topic(action),
            'cmd_t':  self.mqtt_gen_topic(f"{action}/set"),
            'min': 0,
            'max': 100,
            'step': 10,
            'unit_of_meas': '%',
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} ringer volume", action = action, entity_type = 'number', device = device, params = params)
        msgs.append(msg)

        # TODO: night mode on/off/auto select, ring/speaker volume numeric

        mqtt_auth = {'username': self.mqtt_user, 'password': self.mqtt_pass}
        mqtt_publish.multiple(msgs, hostname = self.mqtt_host, port = self.mqtt_port, auth = mqtt_auth)

    # The callback for when the client receives a CONNACK response from the server.
    def mqtt_on_connect(self, client, userdata, flags, rc):
        # Do this in on_connect so they will be re-subscribed on a reconnect
        log.info('MQTT Connected')

        client.subscribe(self.mqtt_gen_topic('ring_volume/set'))
        client.subscribe(self.mqtt_gen_topic('status_led/set'))
        client.subscribe(self.mqtt_gen_topic('image/hdr/set'))
        client.subscribe(self.mqtt_gen_topic('image/mirror/set'))
        client.subscribe(self.mqtt_gen_topic('image/flip/set'))
        client.subscribe(self.mqtt_gen_topic('hooks/update'))
        client.subscribe(self.mqtt_gen_topic('snapshot/update'))

        log.debug(f"Publish 1 to topic {self.mqtt_gen_topic('$state')}")
        self.mqtt_publish('$state', 1, qos = 2)

        status_led = int(xmlparse(self.invoke_foscam(cmd='getLedEnableState', return_response=True))['CGI_Result']['isEnable'])
        log.debug(f"Retrieved status LED setting from device: {str(status_led)}")
        self.mqtt_publish('status_led', status_led)

        ring_volume = int(xmlparse(self.invoke_foscam(cmd='getAudioVolume', return_response=True))['CGI_Result']['volume'])
        log.debug(f"Retrieved volume setting from device: {str(ring_volume)}")
        self.mqtt_publish('ring_volume', ring_volume)

        image_hdr = int(xmlparse(self.invoke_foscam(cmd='getHdrMode', return_response=True))['CGI_Result']['mode'])
        log.debug(f"Retrieved image HDR setting from device: {str(image_hdr)}")
        self.mqtt_publish('image/hdr', image_hdr)

        _image_mirror_flip = xmlparse(self.invoke_foscam(cmd='getMirrorAndFlipSetting', return_response=True))['CGI_Result']
        image_mirror = _image_mirror_flip['isMirror']
        image_flip = _image_mirror_flip['isFlip']
        log.debug(f"Retrieved image mirror/flip settings from device, mirror: {str(image_mirror)}, flip: {str(image_flip)}")
        self.mqtt_publish('image/mirror', image_mirror)
        self.mqtt_publish('image/flip', image_flip)

    # The callback for when a PUBLISH message is received from the server.
    def mqtt_on_message(self, client, userdata, msg):
        log.debug(f"MQTT message received {msg.topic} ({str(len(msg.payload))} bytes)")

    def mqtt_on_snapshot_update(self, client, userdata, msg):
        log.debug('Topic snapshot/update was triggered')
        self.mqtt_publish('snapshot', self.snapshot())
        self.mqtt_publish('snapshot/datetime', dt.strftime(dt.now(), self.date_format))

    def mqtt_on_ring_volume_set(self, client, userdata, msg):
        ring_volume = int(msg.payload)
        log.debug(f"Topic ring_volume/set was triggered, volume: {str(ring_volume)}")
        foscam_options = { 'volume': str(ring_volume) }
        self.invoke_foscam(cmd = 'setAudioVolume', options = foscam_options)
        self.mqtt_publish('ring_volume', ring_volume)

    def mqtt_on_status_led_set(self, client, userdata, msg):
        status_led = int(msg.payload)
        log.debug(f"Topic status_led/set was triggered, state: {str(status_led)}")
        foscam_options = { 'isEnable': str(status_led) }
        self.invoke_foscam(cmd = 'setLedEnableState', options = foscam_options)
        self.mqtt_publish('status_led', status_led)

    def mqtt_on_image_hdr_set(self, client, userdata, msg):
        image_hdr = int(msg.payload)
        log.debug(f"Topic image/hdr/set was triggered, state: {str(image_hdr)}")
        foscam_options = { 'mode': str(image_hdr) }
        self.invoke_foscam(cmd = 'setHdrMode', options = foscam_options)
        self.mqtt_publish('image/hdr', image_hdr)

    def mqtt_on_image_mirror_set(self, client, userdata, msg):
        image_mirror = int(msg.payload)
        log.debug(f"Topic image/mirror/set was triggered, state: {str(image_mirror)}")
        foscam_options = { 'isMirror': str(image_mirror) }
        self.invoke_foscam(cmd = 'mirrorVideo', options = foscam_options)
        self.mqtt_publish('image/mirror', image_mirror)

    def mqtt_on_image_flip_set(self, client, userdata, msg):
        image_flip = int(msg.payload)
        log.debug(f"Topic image/flip/set was triggered, state: {str(image_flip)}")
        foscam_options = { 'isFlip': str(image_flip) }
        self.invoke_foscam(cmd = 'flipVideo', options = foscam_options)
        self.mqtt_publish('image/flip', image_flip)

    def mqtt_on_reboot(self, client, userdata, msg):
        log.debug(f"Topic reboot was triggered")
        self.invoke_foscam(cmd = 'rebootSystem')

foscam = Foscam2MQTT(listen_url = config.listen_url, obfuscate = config.obfuscate, paranoid = config.paranoid)
foscam.date_format = config.date_format
foscam.foscam_host = config.foscam_host
foscam.foscam_port = config.foscam_port
foscam.foscam_user = config.foscam_user
foscam.foscam_pass = config.foscam_pass
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

    foscam.mqtt_publish('action', action, retain=False)
    foscam.mqtt_publish(f"{action}_datetime", dt.strftime(dt.now(), foscam.date_format))

    foscam.mqtt_publish('snapshot', foscam.snapshot())
    foscam.mqtt_publish('snapshot/datetime', dt.strftime(dt.now(), foscam.date_format))

    if foscam.ha_discovery:
        foscam.mqtt_publish(f"{action}/trigger", foscam.trigger_payload, retain = False)

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