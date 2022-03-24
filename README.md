# Foscam2MQTT
My feeble attempt to connect my Foscam VD1 doorbell to MQTT.

## About
It's pretty much one single script that will start an API listener with one webhook and publish to MQTT if the webhook has been triggered. If hostname and credentials for the Foscam device are specified, it will automatically configure the webhooks for the various events supported by Foscam.

## Installation
* Clone repository
* Build Docker image

## Usage
Run the Docker container and make sure you either forward TCP port 5000 or run host networking.
Option | Type | Required | Example | Description
-- | -- | -- | -- | --
`--mqtt-host` | `string` | `yes` |  `10.0.0.1` | IP address or hostname of the MQTT broker to connect to (default: none)
`--mqtt-port` | `int` | `no` | `1883` | TCP port for the MQTT broker to connect to (default: 1883)
`--mqtt-client-id` | `string` | `no` | `foscam2mqtt` | Client ID to use for MQTT (default: foscam2mqtt)
`--mqtt-topic` | `string` | `no` | `foscam2mqtt`| Base topic to use for MQTT (default: foscam2mqtt)
`--mqtt-user` | `string` | `no` | `mqtt` | Username to use for MQTT (default: none)
`--mqtt-pass` | `string` | `no` |  `supersecret` | Password to use for MQTT (only when --mqtt-user is specified, default: none)
`--foscam-host` | `string` | `no` |  `10.0.0.2` | IP address or hostname of the Foscam device to connect to (default: none)
`--foscam-port` | `int` | `no` |  `88` | TCP port for the Foscam device to connect to (default: 88)
`--foscam-user` | `string` | `no` |  `fozzie` | Username to use for Foscam device (default: none)
`--foscam-pass` | `string` | `no` |  `anothersecret` | Password to use for Foscam device (default: none)
`--listen-url` | `string` | `no` |  `http://10.10.0.1:5000/` | Listen URL where the Foscam device can connect to this server (default: none)
`--obfuscate` | `switch` | `no` |  - | Obfuscate actions, use random strings instead of plain action names
`--paranoid` | `switch` | `no` |  - | Enable paranoid mode - randomize action strings after each invocation (only when --obfuscate is specified)
`--ha-discovery` | `switch` | `no` |  - | Enable Home Assistant autodiscovery
`--verbose, -v[vv]` | `switch` | `no` |  - | Enable verbose output. Repeat up to 3 times to increase verbosity.

If you want the server to auto-configure your Foscam device, please make sure you can reach the device: ```http://10.0.0.1:88/cgi-bin/CGIProxy.fcgi?usr=mqtt&pwd=supersecret&cmd=getDevInfo``` Also make sure you can reach the webhook: ```http://10.10.0.1:5000/``` should show something like: *1970-01-01T13:37:59Z ERROR - no action specified*