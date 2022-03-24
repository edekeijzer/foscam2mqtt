#!/usr/bin/env python3
from flask import Flask, request as req, Response as res, json
from waitress.server import create_server

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

from base64 import b64encode as b64enc
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
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
parser.add_argument('--listen-url', type=str, help='URL we should advertise to Foscam device')
parser.add_argument('--obfuscate', action=ap.BooleanOptionalAction, help='Obfuscate webhook actions')
parser.add_argument('--paranoid', action=ap.BooleanOptionalAction, help='Cycle obfuscated webhook action after each trigger')
parser.add_argument('--foscam-host', type=str, help='Foscam VD1 IP/hostname to connect to for auto-configuration of webhooks')
parser.add_argument('--foscam-port', type=int, default=88, help='Foscam VD1 port to connect to (default: 88)')
parser.add_argument('--foscam-ssl', action=ap.BooleanOptionalAction, help='Enable SSL encryption on Foscam connection (default: false, use --foscam-port 443 for this)')
parser.add_argument('--foscam-user', type=str, default='admin', help='Username to use for connecting to Foscam device')
parser.add_argument('--foscam-pass', help='Password to use for connecting')
parser.add_argument('--mqtt-host', required=True, help='MQTT server to connect to')
parser.add_argument('--mqtt-port', type=int, default=1883, help='MQTT TCP port to connect to (default: 1883)')
parser.add_argument('--mqtt-ssl', action=ap.BooleanOptionalAction, help='Enable SSL encryption on MQTT connection (default: false)')
parser.add_argument('--mqtt-user', help='Username to use for connecting (default: none)')
parser.add_argument('--mqtt-pass', help='Password to use for connecting (only used when username is specified)')
parser.add_argument('--mqtt-topic', default='foscam2mqtt', help='MQTT topic to publish to (default: foscam2mqtt)')
parser.add_argument('--mqtt-client-id', default='foscam2mqtt', help='MQTT client ID to use for connecting (default: foscam2mqtt)')
parser.add_argument('--ha-discovery', action=ap.BooleanOptionalAction, help='Enable publishing to Home Assistant discovery topic (default: false)')
parser.add_argument('--ha-discovery-topic', default='homeassistant', help='MQTT topic to publish HA discovery information to (default: homeassistant)')
parser.add_argument('--ha-camera-name', default='Foscam VD1', help='Friendly name of the entities to publish in HA (default: Foscam VD1)')
parser.add_argument('--ha-cleanup', action=ap.BooleanOptionalAction, help='Remove HA sensor config on exit (default: false)')
parser.add_argument('-q', '--quiet', action=ap.BooleanOptionalAction, help='Do not show warning messages (default: false)')
parser.add_argument('-v', '--verbose', action='count', help='Show verbose output (repeat up to 3 times to increase verbosity)')
parser.add_argument('--date-format', type=str, default='%Y-%m-%dT%H:%M:%SZ', help='Date/time format for logging (strftime template)')
args=parser.parse_args()

if not args.verbose:
    args.verbose = 0

if args.verbose:
    date_time = dt.strftime(dt.now(), args.date_format)
    print(date_time + ' V   Verbose output enabled')
    print(date_time + ' V   Listening on ' + args.listen_address + ':' + str(args.listen_port))

class Foscam2MQTT:
    def __init__(self, listen_url = None, obfuscate = False, paranoid = False, quiet = False, verbose = False):
        # Own settings
        self.actions = 'button','motion','sound','face','human' #,'alarm'
        self.listen_url = listen_url
        self.obfuscate = obfuscate
        if self.obfuscate:
            self.action_keys = dict()
            self.paranoid = paranoid
        self.date_format = '%Y-%m-%dT%H:%M:%SZ'

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
        self.ha_camera_name = 'Foscam VD1'

        self.verbose = verbose
        if self.verbose:
            date_time = dt.strftime(dt.now(), self.date_format)
            print(date_time + ' V   Foscam2MQTT initialized')

    def verify_action(self, action):
        if self.obfuscate and action in (self.action_keys.keys()):
            return self.action_keys[action]
        elif not self.obfuscate and action in self.actions:
            return action
        else:
            return False

    def invoke_foscam(self, cmd, options = None, return_response = False):
        foscam_url = 'http://' + self.foscam_host + ':' + str(self.foscam_port) + '/cgi-bin/CGIProxy.fcgi?usr=' + self.foscam_user + '&pwd=' + self.foscam_pass + '&cmd=' + cmd
        if options:
            for key,value in options.items():
                foscam_url += '&' + key + '=' + value
        try:
            response = requests.get(foscam_url, timeout = 15, verify = False)
            response.raise_for_status()
        except requests.exceptions.HTTPError as errh:
            if response.status_code == 404:
                print('Remote server returned HTTP error code 404')
            else:
                print(errh)
            return False
        except req.exceptions.ConnectionError as errc:
            print(errc)
            return False
        except req.exceptions.Timeout as errt:
            print(errt)
            return False
        except req.exceptions.RequestException as err:
            print(err)
            return False
        if return_response: return response.content

    def snapshot(self, publish = False):
        snapshot = self.invoke_foscam(cmd = 'snapPicture2', return_response = True)
        if publish:
            date_time = dt.strftime(dt.now(), self.date_format)
            self.mqtt_publish('snapshot', snapshot)
            self.mqtt_publish('snapshot_datetime', date_time)
        else:
            return snapshot

    def update_hooks(self):
        if self.listen_url is None:
            print('Assuming default listen url')
            self.listen_url = 'http://localhost:5000/'
        action_aliases = dict({'button':'BKLinkUrl','motion':'MDLinkUrl','sound':'SDLinkUrl','face':'FaceLinkUrl','human':'HumanLinkUrl'}) #,'alarm':'AlarmUrl'})
        # If an action_name was specified, only update that one.
        foscam_options = dict()
        for action_name, alias in action_aliases.items():
            if self.obfuscate:
                rnd.seed()
                random_string = ''.join(rnd.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=24))
                self.action_keys[random_string] = action_name
                action = random_string
            else:
                action = action_name
            action_url = self.listen_url + '?action=' + action
            if self.verbose >= 3:
                date_time = dt.strftime(dt.now(), self.date_format)
                print(date_time + ' VVV Generated URL for ' + action_name + ' - ' + action_url)
            encoded_url = b64enc(action_url.encode('ascii'))
            foscam_options[alias] = encoded_url.decode('ascii')
        self.invoke_foscam(cmd = 'setAlarmHttpServer', options = foscam_options, return_response = True)

    def mqtt_gen_topic(self, sub_topic, main_topic = None):
        if main_topic is None: main_topic = self.mqtt_topic
        return main_topic + '/' + sub_topic

    def mqtt_init(self, host, port = 1883, ssl = False, username = None, password = None, client_id = 'foscam2mqtt', topic = 'foscam2mqtt', camera_name = 'Foscam VD1'):
        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose: print(date_time + ' V   Initializing MQTT client with ID ' + client_id)
        self.mqtt_client_id = client_id
        self.mqtt_client = mqtt.Client(protocol=mqtt.MQTTv311, client_id = client_id, clean_session = False)

        self.mqtt_host = host
        self.mqtt_port = port

        self.mqtt_topic = topic
        if self.verbose: print(date_time + ' V   MQTT base topic is ' + topic)

        self.mqtt_settings = {
            'protocol':  mqtt.MQTTv311,
            'client_id': client_id,
            'hostname': host,
            'port': port
        }

        if username:
            self.mqtt_user = username
            self.mqtt_pass = password
            self.mqtt_client.username_pw_set(username, password)
            self.mqtt_settings['auth'] = {'username': username, 'password': password}

        if self.ha_discovery:
            self.ha_camera_name = camera_name
            if self.verbose: print(date_time + ' V   Camera name is ' + self.ha_camera_name)

        self.mqtt_client.on_connect = self.mqtt_on_connect
        self.mqtt_client.on_message = self.mqtt_on_message

        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose: print(date_time + ' V   Connect to MQTT broker ' + self.mqtt_host + ':' + str(self.mqtt_port))
        self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)

        # Publish this one before creating callback to prevent recursive loop
        self.mqtt_publish('verbosity', self.verbose)

        if self.verbose >= 2: print(date_time + ' VV  Add callback for topic ' + self.mqtt_gen_topic('verbosity'))
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('verbosity'), self.mqtt_on_verbosity_update)

        if self.verbose >= 2: print(date_time + ' VV  Add callback for topic ' + self.mqtt_gen_topic('hooks_update'))
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('hooks_update'), self.mqtt_on_hooks_update)

        if self.verbose >= 2: print(date_time + ' VV  Add callback for topic ' + self.mqtt_gen_topic('snapshot_update'))
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('snapshot_update'), self.mqtt_on_snapshot_update)

    def mqtt_disconnect(self):
        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose >= 2: print(date_time + ' VV  Publish 0 to topic ' + self.mqtt_gen_topic('state'))
        self.mqtt_publish('state', 0)
        if self.verbose: print(date_time + ' V   Disconnect')
        self.mqtt_client.disconnect()

    def mqtt_publish(self, topic, payload, time_to_action = False, qos = 0, retain = True):
        if not self.mqtt_client:
            date_time = dt.strftime(dt.now(), self.date_format)
            print(date_time + ' E   MQTT not initialized, run self.mqtt_init first!')
            return False
        topic = self.mqtt_gen_topic(topic)
        date_time = dt.strftime(dt.now(), self.date_format)
        self.mqtt_client.publish(topic, payload, qos = qos, retain = retain)
        if self.verbose >= 2:
            if type(payload) is int:
                payload = str(payload)
            elif type(payload) is bytes:
                payload = 'of ' + str(len(payload)) + ' bytes'
            else:
                payload = 'of type ' + type(payload).__name__
            print(date_time + ' VV  Published payload ' + payload + ' to topic ' + topic)
        if time_to_action and type(payload) is str:
            topic = self.mqtt_gen_topic(payload)
            self.mqtt_client.publish(topic, date_time, qos = qos, retain = True)

    def mqtt_gen_ha_entity(self, action, type, availability_topic, name = 'Foscam VD1', device = None, icon = 'mdi:doorbell-video'):
        unique_id = self.mqtt_topic + '_' + action
        if self.verbose >= 2:
            date_time = dt.strftime(dt.now(), self.date_format)
            print(date_time + ' VV  Generated unique_id ' + unique_id)

        config_topic = self.ha_discovery_topic + '/' + type + '/' + unique_id + '/config'

        entity = {
            'uniq_id': unique_id,
            'obj_id': unique_id,
            'name': name,
            'ic': icon,
            'avty_t': self.mqtt_gen_topic('state'),
            'pl_avail': 1,
            'pl_not_avail': 0,
        }

        if type == 'binary_sensor':
            entity['stat_t'] = self.mqtt_gen_topic(action)
            entity['val_tpl'] = '{{ value }}'
        elif type == 'sensor':
            entity['stat_t'] = self.mqtt_gen_topic(action)
            entity['val_tpl'] = '{{ value }}'
        elif type == 'camera':
            entity['t'] = self.mqtt_gen_topic(action)
            entity['val_tpl'] = '{{ value }}'
        else:
            print(date_time + ' W   Unknown entity type ' + type + ', unable to generate autodiscovery config.')
            return False

        if device is not None: entity['dev'] = device
        entity_json = json_dumps(entity)
        if self.verbose >= 3: print(date_time + ' VVV ' + action + ' entity_json ' + entity_json)

        msg = {
            'topic': config_topic,
            'payload': entity_json,
            'qos': 0,
            'retain': True
        }
        return msg

    def mqtt_publish_ha_entities(self):
        msgs = []

        dev_info = xmlparse(self.invoke_foscam(cmd='getDevInfo', return_response=True))['CGI_Result']

        device = {
            'manufacturer': 'Foscam',
            'model': 'VD1',
            'name': self.ha_camera_name,
            'identifiers': [self.mqtt_topic],
            'sw_version': dev_info['hardwareVer'] + '_' + dev_info['firmwareVer'],
        }

        msg = self.mqtt_gen_ha_entity(action = 'snapshot', type = 'camera', availability_topic = self.mqtt_gen_topic('state'), device = device, icon = 'mdi:doorbell-video')
        msgs.append(msg)

        for action in self.actions:
            msg = self.mqtt_gen_ha_entity(action = action, type = 'binary_sensor', availability_topic = self.mqtt_gen_topic('state'), device = device, icon = 'mdi:doorbell-video')
            msgs.append(msg)

        mqtt_auth = {'username':self.mqtt_user, 'password': self.mqtt_pass}
        mqtt_publish.multiple(msgs, hostname = self.mqtt_host, port = self.mqtt_port, auth = mqtt_auth)

    # The callback for when the client receives a CONNACK response from the server.
    def mqtt_on_connect(self, client, userdata, flags, rc):
        # Do this in on_connect so they will be re-subscribed on a reconnect
        if self.verbose:
            date_time = dt.strftime(dt.now(), self.date_format)
            print(date_time + ' V   MQTT Connected')

        client.subscribe(self.mqtt_gen_topic('hooks_update'))
        client.subscribe(self.mqtt_gen_topic('snapshot_update'))
        client.subscribe(self.mqtt_gen_topic('verbosity'))

        if self.verbose >= 2: print(date_time + ' VV  Publish 1 to topic ' + self.mqtt_gen_topic('state'))
        self.mqtt_publish('state', 1)

    # The callback for when a PUBLISH message is received from the server.
    def mqtt_on_message(self, client, userdata, msg):
        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose >= 2: print(date_time + ' VV  MQTT message received ' + msg.topic + ' ' + str(msg.payload))

    def mqtt_on_verbosity_update(self, client, userdata, msg):
        date_time = dt.strftime(dt.now(), self.date_format)
        self.verbose = int(msg.payload)
        if self.verbose: print(date_time + ' V   Verbosity level changed to ' + str(self.verbose))

    def mqtt_on_hooks_update(self, client, userdata, msg):
        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose >= 2: print(date_time + ' VV  Topic hooks_update was triggered')
        self.update_hooks()

    def mqtt_on_snapshot_update(self, client, userdata, msg):
        date_time = dt.strftime(dt.now(), self.date_format)
        if self.verbose >= 2: print(date_time + ' VV  Topic snapshot_update was triggered')
        self.snapshot(publish = True)

foscam = Foscam2MQTT(listen_url = args.listen_url, obfuscate = args.obfuscate, paranoid = args.paranoid, verbose = args.verbose)
foscam.date_format = args.date_format
foscam.foscam_host = args.foscam_host
foscam.foscam_port = args.foscam_port
foscam.foscam_user = args.foscam_user
foscam.foscam_pass = args.foscam_pass
foscam.update_hooks()

date_time = dt.strftime(dt.now(), args.date_format)
if args.mqtt_user:
    if args.verbose: print(date_time + ' V   initialize MQTT with user/pass')
    foscam.mqtt_init(host = args.mqtt_host, port = args.mqtt_port, client_id = args.mqtt_client_id, topic = args.mqtt_topic, username = args.mqtt_user, password = args.mqtt_pass)
else:
    if args.verbose: print(date_time + ' V   initialize MQTT without user/pass')
    foscam.mqtt_init(host = args.mqtt_host, port = args.mqtt_port, client_id = args.mqtt_client_id, topic = args.mqtt_topic)

if args.ha_discovery:
    foscam.ha_discovery_topic = args.ha_discovery_topic
    foscam.mqtt_publish_ha_entities()

# Create snapshot for starters
foscam.snapshot(publish = True)

foscam.mqtt_client.loop_start()

# Define Flask web app
app = Flask(__name__)

@app.route('/', methods=['GET', 'PUT', 'POST'])
def webhook():
    date_time = dt.strftime(dt.now(), args.date_format)
    ct = req.content_type
    if req.method == 'GET':
        req_args = req.args
    elif req.method == 'POST' and ct == 'application/x-www-form-urlencoded':
        req_args = req.form
    elif req.method == 'POST' and ct == 'application/json':
        req_args = req.json

    if 'action' in req_args:
        action = req_args['action']
    else:
        if not args.quiet: print(date_time + ' W   No action specified - ' + req.remote_addr)
        response = date_time + ' ERROR - no action specified'
        return res(response=response, status=400)

    verified_action = foscam.verify_action(action)
    if not verified_action:
        if not args.quiet: print(date_time + ' W   Unknown action ' + action + ' - ' + req.remote_addr)
        response = date_time + ' ERROR - unknown action'
        return res(response = response, status = 400)
    else:
        action = verified_action

    if args.verbose: print(date_time + ' V   ' + req.method + ' ' + action + ' - ' + req.remote_addr)

    foscam.mqtt_publish('action', action)
    foscam.snapshot(publish = True)

    if foscam.paranoid:
        if args.verbose >= 2: print(date_time + ' VV  Paranoid enabled, cycling webhooks')
        foscam.update_hooks()

    response = date_time + ' OK'
    return res(response = response, status = 200)

def on_signal(x, y):
    if args.verbose:
        date_time = dt.strftime(dt.now(), args.date_format)
        print('\n' + date_time + ' V ' + Signals(x).name + ' received')
    app_server.close()

signal(SIGTERM, on_signal)
signal(SIGINT, on_signal)

app_server = create_server(app, host = args.listen_address, port = args.listen_port)
try:
    app_server.run()
except OSError:
    if args.verbose >= 2:
        date_time = dt.strftime(dt.now(), args.date_format)
        print(date_time + ' VV  Caught expected error on process termination')

# Set state to unavailable
foscam.mqtt_disconnect()
