import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

from base64 import b64encode as b64enc, b64decode as b64dec
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
from urllib.parse import unquote
from json import loads as json_loads, dumps as json_dumps, JSONDecodeError
from xmltodict import parse as xmlparse

import random as rnd
import string
from datetime import datetime as dt

import logging
logger = logging.getLogger(__name__).addHandler(logging.NullHandler())

class Foscam2MQTT:
    def __init__(self, listen_url, obfuscate = False, paranoid = False, quiet = False):
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


        # Deepstack settings
        self.deepstack_url = None
        self.deepstack_api_key = None

        logger.info('Foscam2MQTT initialized')

    def verify_action(self, action):
        if self.obfuscate and action in (self.action_keys.keys()):
            return self.action_keys[action]
        elif not self.obfuscate and action in self.actions:
            return action
        else:
            return False

    def invoke_foscam(self, cmd, options = None, return_response = False):
        foscam_url = f"http://{self.foscam_host}:{str(self.foscam_port)}/cgi-bin/CGIProxy.fcgi"
        params = {
            'usr': self.foscam_user,
            'pwd': self.foscam_pass,
            'cmd': cmd,
        }
        if options:
            params.update(options)
        try:
            response = requests.get(foscam_url, params = params, timeout = 15, verify = False)
            logger.debug(f"Request URL: {response.url}")
            response.raise_for_status()
        except requests.exceptions.HTTPError as errh:
            if response.status_code == 404:
                logger.warning('Remote server returned HTTP error code 404')
            else:
                logger.warning(errh)
            return False
        except req.exceptions.ConnectionError as errc:
            logger.warning(errc)
            return False
        except req.exceptions.Timeout as errt:
            logger.warning(errt)
            return False
        except req.exceptions.RequestException as err:
            logger.warning(err)
            return False
        if return_response: return response.content

    def snapshot(self):
        snapshot = self.invoke_foscam(cmd = 'snapPicture2', return_response = True)
        return snapshot

    def update_hooks(self, triggered_action = None):
        action_aliases = dict({'button':'BKLinkUrl','motion':'MDLinkUrl','sound':'SDLinkUrl','face':'FaceLinkUrl','human':'HumanLinkUrl'}) #,'alarm':'AlarmUrl'})
        # If an action_name was specified, only update that one.
        foscam_options = dict()
        alarm_urls = xmlparse(self.invoke_foscam(cmd='getAlarmHttpServer', return_response=True))['CGI_Result']

        # For each detection option, retrieve settings, enable URL trigger and push settings
        for foscam_cmd in ('MotionDetect', 'FaceDetect', 'AudioAlarm'):
            foscam_options = xmlparse(self.invoke_foscam(cmd=f"get{foscam_cmd}Config", return_response=True))['CGI_Result']
            foscam_options['result'] = None
            is_enabled = foscam_options['isEnable']
            # URL is bit 9 in the linkage option.
            # If this detection is already enabled, bitwise OR the URL option into it. Otherwise, enable it and set linkage to only URL.
            if is_enabled:
                linkage = int(foscam_options['linkage']) | 512
            else:
                foscam_options['isEnable'] = 1
                linkage = 512
            foscam_options['linkage'] = linkage
            self.invoke_foscam(cmd=f"set{foscam_cmd}Config", options = foscam_options)

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
                    logger.debug(f"Retrieved existing URL for action {action}: {alarm_url}")
                    if self.listen_url in alarm_url:
                        action = alarm_url.replace((f"{self.listen_url}?action="), '')
                        logger.debug(f"Extracted action: {action}")
                    else:
                        action = 'none'
            else:
                action = action_name
            action_url = f"{self.listen_url}?action={action}"
            logger.debug(f"Generated URL for {action_name} - {action_url}")
            encoded_url = b64enc(action_url.encode('ascii'))
            foscam_options[alias] = encoded_url.decode('ascii')
        self.invoke_foscam(cmd = 'setAlarmHttpServer', options = foscam_options, return_response = True)

    def mqtt_gen_topic(self, sub_topic, main_topic = None):
        if main_topic is None: main_topic = self.mqtt_topic
        return f"{main_topic}/{sub_topic}"

    def mqtt_init(self, host, port = 1883, ssl = False, username = None, password = None, client_id = 'foscam2mqtt', topic = 'foscam2mqtt'):
        logger.info(f"Initializing MQTT client with ID {client_id}")
        self.mqtt_client_id = client_id
        self.mqtt_client = mqtt.Client(protocol=mqtt.MQTTv311, client_id = client_id, clean_session = False)

        self.mqtt_host = host
        self.mqtt_port = port

        self.mqtt_topic = topic
        logger.info(f"MQTT base topic is {topic}")

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
            logger.info(f"Camera name is {self.ha_device_name}")

        self.mqtt_client.on_connect = self.mqtt_on_connect
        # self.mqtt_client.on_message = self.mqtt_on_message

        logger.info(f"Connect to MQTT broker {self.mqtt_host}:{str(self.mqtt_port)}")
        self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('snapshot/update')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('snapshot/update'), self.mqtt_on_snapshot_update)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('ring_volume/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('ring_volume/set'), self.mqtt_on_ring_volume_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('status_led/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('status_led/set'), self.mqtt_on_status_led_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('image/hdr/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/hdr/set'), self.mqtt_on_image_hdr_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('image/mirror/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/mirror/set'), self.mqtt_on_image_mirror_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('image/flip/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('image/flip/set'), self.mqtt_on_image_flip_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('night_mode/set')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('night_mode/set'), self.mqtt_on_night_mode_set)

        logger.debug(f"Add callback for topic {self.mqtt_gen_topic('reboot')}")
        self.mqtt_client.message_callback_add(self.mqtt_gen_topic('reboot'), self.mqtt_on_reboot)

    def mqtt_disconnect(self):
        logger.debug(f"Publish 0 to topic {self.mqtt_gen_topic('$state')}")
        self.mqtt_publish('$state', 0, qos = 2)
        logger.info('Disconnect')
        self.mqtt_client.disconnect()

    def mqtt_publish(self, topic, payload, qos = 0, retain = True):
        if not self.mqtt_client:
            logger.error('MQTT not initialized, run self.mqtt_init first!')
            return False

        topic = self.mqtt_gen_topic(topic)

        self.mqtt_client.publish(topic, payload, qos = qos, retain = retain)

        if type(payload) is int:
            payload = str(payload)
        elif type(payload) is bytes:
            payload = f"of {str(len(payload))} bytes"
        elif type(payload) is not str:
            payload = f"of type {type(payload).__name__}"
        logger.debug(f"Published payload {payload} to topic {topic}")

    def mqtt_gen_ha_entity(self, action, entity_type, availability_topic = None, name = None, params = None, device = None, icon = 'mdi:help-box'):
        unique_id = f"{self.mqtt_topic}_{action}"
        logger.debug(f"Generated unique_id {unique_id}")

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
        if entity_type not in ['device_automation']: entity['ic'] = icon

        # Add type specific params if specified
        if params: entity.update(params)

        if device: entity['dev'] = device
        entity_json = json_dumps(entity)
        logger.debug(f"{action} entity_json {entity_json}")

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

        # Night mode select
        action = 'night_mode'
        params = {
            'ic': 'mdi:led-on',
            'stat_t': self.mqtt_gen_topic(action),
            'cmd_t': self.mqtt_gen_topic(f"{action}/set"),
            'options': ('on', 'off', 'auto')
        }
        msg = self.mqtt_gen_ha_entity(name = f"{self.ha_device_name} night mode", action = action, entity_type = 'select', device = device, params = params)
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
        logger.info('MQTT Connected')

        # client.subscribe(self.mqtt_gen_topic('ring_volume/set'))
        # client.subscribe(self.mqtt_gen_topic('status_led/set'))
        # client.subscribe(self.mqtt_gen_topic('image/hdr/set'))
        # client.subscribe(self.mqtt_gen_topic('image/mirror/set'))
        # client.subscribe(self.mqtt_gen_topic('image/flip/set'))
        # client.subscribe(self.mqtt_gen_topic('hooks/update'))
        # client.subscribe(self.mqtt_gen_topic('snapshot/update'))

        logger.debug(f"Publish 1 to topic {self.mqtt_gen_topic('$state')}")
        self.mqtt_publish('$state', 1, qos = 2)

        status_led = int(xmlparse(self.invoke_foscam(cmd='getLedEnableState', return_response=True))['CGI_Result']['isEnable'])
        logger.debug(f"Retrieved status LED setting from device: {str(status_led)}")
        self.mqtt_publish('status_led', status_led)

        infra_led_mode = int(xmlparse(self.invoke_foscam(cmd='getInfraLedConfig', return_response=True))['CGI_Result']['mode'])
        logger.debug(f"Retrieved infra LED mode from device: {str(infra_led_mode)} (0 = auto, 1 = manual)")
        if infra_led_mode == 0:
            night_mode = 'auto'
        else:
            infra_led_state = int(xmlparse(self.invoke_foscam(cmd='getDevState', return_response=True))['CGI_Result']['infraLedState'])
            logger.debug(f"Retrieved infra LED state from device: {str(infra_led_state)}")
            if infra_led_state == 1:
                night_mode = 'on'
            else:
                night_mode = 'off'
        logger.debug(f"Night mode: {night_mode}")
        self.mqtt_publish('night_mode', night_mode)

        ring_volume = int(xmlparse(self.invoke_foscam(cmd='getAudioVolume', return_response=True))['CGI_Result']['volume'])
        logger.debug(f"Retrieved volume setting from device: {str(ring_volume)}")
        self.mqtt_publish('ring_volume', ring_volume)

        image_hdr = int(xmlparse(self.invoke_foscam(cmd='getHdrMode', return_response=True))['CGI_Result']['mode'])
        logger.debug(f"Retrieved image HDR setting from device: {str(image_hdr)}")
        self.mqtt_publish('image/hdr', image_hdr)

        _image_mirror_flip = xmlparse(self.invoke_foscam(cmd='getMirrorAndFlipSetting', return_response=True))['CGI_Result']
        image_mirror = _image_mirror_flip['isMirror']
        image_flip = _image_mirror_flip['isFlip']
        logger.debug(f"Retrieved image mirror/flip settings from device, mirror: {str(image_mirror)}, flip: {str(image_flip)}")
        self.mqtt_publish('image/mirror', image_mirror)
        self.mqtt_publish('image/flip', image_flip)

    # The callback for when a PUBLISH message is received from the server.
    def mqtt_on_message(self, client, userdata, msg):
        logger.debug(f"MQTT message received {msg.topic} ({str(len(msg.payload))} bytes)")

    def mqtt_on_snapshot_update(self, client, userdata, msg):
        logger.debug('Topic snapshot/update was triggered')
        self.mqtt_publish('snapshot', self.snapshot())
        self.mqtt_publish('snapshot/datetime', dt.strftime(dt.now(), self.date_format))

    def mqtt_on_ring_volume_set(self, client, userdata, msg):
        ring_volume = int(msg.payload)
        logger.debug(f"Topic ring_volume/set was triggered, volume: {str(ring_volume)}")
        foscam_options = { 'volume': str(ring_volume) }
        self.invoke_foscam(cmd = 'setAudioVolume', options = foscam_options)
        self.mqtt_publish('ring_volume', ring_volume)

    def mqtt_on_status_led_set(self, client, userdata, msg):
        status_led = int(msg.payload)
        logger.debug(f"Topic status_led/set was triggered, state: {str(status_led)}")
        foscam_options = { 'isEnable': str(status_led) }
        self.invoke_foscam(cmd = 'setLedEnableState', options = foscam_options)
        self.mqtt_publish('status_led', status_led)

    def mqtt_on_image_hdr_set(self, client, userdata, msg):
        image_hdr = int(msg.payload)
        logger.debug(f"Topic image/hdr/set was triggered, state: {str(image_hdr)}")
        foscam_options = { 'mode': str(image_hdr) }
        self.invoke_foscam(cmd = 'setHdrMode', options = foscam_options)
        self.mqtt_publish('image/hdr', image_hdr)

    def mqtt_on_image_mirror_set(self, client, userdata, msg):
        image_mirror = int(msg.payload)
        logger.debug(f"Topic image/mirror/set was triggered, state: {str(image_mirror)}")
        foscam_options = { 'isMirror': str(image_mirror) }
        self.invoke_foscam(cmd = 'mirrorVideo', options = foscam_options)
        self.mqtt_publish('image/mirror', image_mirror)

    def mqtt_on_image_flip_set(self, client, userdata, msg):
        image_flip = int(msg.payload)
        logger.debug(f"Topic image/flip/set was triggered, state: {str(image_flip)}")
        foscam_options = { 'isFlip': str(image_flip) }
        self.invoke_foscam(cmd = 'flipVideo', options = foscam_options)
        self.mqtt_publish('image/flip', image_flip)

    def mqtt_on_night_mode_set(self, client, userdata, msg):
        night_mode = msg.payload.decode('ascii')
        logger.debug(f"Topic night_mode/set was triggered, state: {night_mode}")
        if night_mode == 'auto':
            foscam_options = { 'mode': 0 }
            self.invoke_foscam(cmd = 'setInfraLedConfig', options = foscam_options)
        else:
            foscam_options = { 'mode': 1 }
            self.invoke_foscam(cmd = 'setInfraLedConfig', options = foscam_options)
            if night_mode == 'on':
                self.invoke_foscam(cmd = 'openInfraLed')
            else:
                self.invoke_foscam(cmd = 'closeInfraLed')
        self.mqtt_publish('night_mode', night_mode)

    def mqtt_on_reboot(self, client, userdata, msg):
        logger.debug(f"Topic reboot was triggered")
        self.invoke_foscam(cmd = 'rebootSystem')

    def _invoke_deepstack(self, endpoint, image_data):
        deepstack_url = f"{self.deepstack_url}/v1/vision/{endpoint}"
        logger.debug(f"Deepstack API endpoint: {deepstack_url}")
        try:
            request_args = {
                'timeout': 5,
                'files': {'image': image_data}
            }
            if self.deepstack_api_key:
                request_args['data'] = {'api_key': self.deepstack_api_key}
            response = requests.post(deepstack_url, **request_args)
            response.raise_for_status()
        except requests.exceptions.HTTPError as errh:
            if response.status_code == 404:
                logger.warning('Remote server returned HTTP error code 404')
            else:
                logger.warning(errh)
            return False
        except req.exceptions.ConnectionError as errc:
            logger.warning(errc)
            return False
        except req.exceptions.Timeout as errt:
            logger.warning(errt)
            return False
        except req.exceptions.RequestException as err:
            logger.warning(err)
            return False

        response = response.json()

        if len(response['predictions']) > 0:
            logger.debug(f"Deepstack returned {str(response['predictions'])} predictions for {endpoint}.")
            return response['predictions']
        else:
            logger.warning(f"No predictions returned for {endpoint}.")
            return False

    def deepstack_object(self, image_data, action = None):
        predictions = self._invoke_deepstack('detection', image_data)
        if predictions:
            date_time = dt.strftime(dt.now(), self.date_format)
            image = Image.open(BytesIO(image_data))
            image_payload = BytesIO()
            images = {}
            font = ImageFont.truetype(font='/fonts/noto.ttf', size=24)
            color = (255, 255, 255, 128)
            for entity in predictions:
                label = entity['label']
                if not label in images.keys():
                    images[label] = image
                draw = ImageDraw.Draw(images[label])
                confidence = entity['confidence']
                logger.info(f"A {label} was detected by Deepstack with {str(round(confidence, 2))} confidence.")
                x_min = max(int(entity["x_min"]) - 10, 0)
                y_min = max(int(entity["y_min"]) - 10, 0)
                x_max = min(int(entity["x_max"]) + 10, image.width)
                y_max = min(int(entity["y_max"]) + 10, image.height)
                draw.rectangle((x_min, y_min, x_max, y_max), outline=color)
                draw.text((x_min + 10, y_min + 10), text=f"{label} ({str(round(confidence, 2))})", font=font, fill=color)
            draw.text((8, 8), text=date_time, font=font, fill=color)
            for label in images.keys():
                images[label].save(image_payload, 'JPEG')
                self.mqtt_publish(f"{action}/{label}/snapshot", image_payload.read())
                self.mqtt_publish(f"{action}/{label}/datetime", date_time)

    def deepstack_face(self, image_data, action = None):
        predictions = self._invoke_deepstack('face/recognize', image_data)
        if predictions:
            date_time = dt.strftime(dt.now(), self.date_format)
            image = Image.open(BytesIO(image_data))
            image_payload = BytesIO()
            font = ImageFont.truetype(font='/fonts/noto.ttf', size=24)
            for entity in predictions:
                user_id = entity['userid']
                confidence = entity['confidence']
                logger.info(f"{user_id} was detected by Deepstack with {str(round(confidence, 2))} confidence.")
                x_min = max(int(entity["x_min"]) - 10, 0)
                y_min = max(int(entity["y_min"]) - 10, 0)
                x_max = min(int(entity["x_max"]) + 10, image.width)
                y_max = min(int(entity["y_max"]) + 10, image.height)
                image_crop = image.crop((x_min, y_min, x_max, y_max))
                image_crop.save(image_payload, 'JPEG')
                self.mqtt_publish(f"{action}/{user_id}/snapshot", image_payload.read())
                self.mqtt_publish(f"{action}/{user_id}/confidence", confidence)
                self.mqtt_publish(f"{action}/{user_id}/datetime", date_time)
