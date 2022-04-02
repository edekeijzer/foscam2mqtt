# Foscam2MQTT

An attempt to connect my Foscam VD1 doorbell to MQTT.

## About

It's pretty much one single script that will start an API listener with one webhook and publish to MQTT if the webhook has been triggered. If hostname and credentials for the Foscam device are specified, it will automatically configure the webhooks for the various events supported by Foscam.

## Installation

* Clone repository
* Build Docker image

**OR**

* ```docker pull edekeijzer/foscam2mqtt:dev```

## Usage

Run the Docker container and make sure you either forward TCP port 5000 or run host networking.
Most of the options have a default value, but it's unlikely to work with all settings at default.


| Option                 | Type     | Example                  | Description                                                                                                                                      |
| ------------------------ | ---------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--listen-url`         | `string` | `http://10.10.0.1:5000/` | **REQUIRED** Listen URL where the Foscam device can connect to this server (default: http://localhost:5000)                                      |
| `--mqtt-host`          | `string` | `10.0.0.1`               | IP address or hostname of the MQTT broker to connect to (default: none)                                                                          |
| `--mqtt-port`          | `int`    | `1883`                   | TCP port for the MQTT broker to connect to (default: 1883)                                                                                       |
| `--mqtt-ssl`           | `switch` | -                        | Enable SSL encryption on MQTT connection                                                                                                         |
| `--mqtt-client-id`     | `string` | `foscam2mqtt`            | Client ID to use for MQTT (default: foscam2mqtt)                                                                                                 |
| `--mqtt-topic`         | `string` | `foscam2mqtt`            | Base topic to use for MQTT (default: foscam2mqtt)                                                                                                |
| `--mqtt-user`          | `string` | `mqtt`                   | Username to use for MQTT (default: none)                                                                                                         |
| `--mqtt-pass`          | `string` | `supersecret`            | Password to use for MQTT (only when --mqtt-user is specified, default: none)                                                                     |
| `--foscam-host`        | `string` | `10.0.0.2`               | IP address or hostname of the Foscam device to connect to (default: none)                                                                        |
| `--foscam-port`        | `int`    | `88`                     | TCP port for the Foscam device to connect to (default: 88)                                                                                       |
| `--foscam-ssl`         | `switch` | -                        | Enable SSL encryption on Foscam connection (specify --foscam-port 443 when using this)                                                           |
| `--foscam-user`        | `string` | `fozzie`                 | Username to use for Foscam device (default: none)                                                                                                |
| `--foscam-pass`        | `string` | `anothersecret`          | Password to use for Foscam device (default: none)                                                                                                |
| `--obfuscate`          | `switch` | -                        | Obfuscate actions, use random strings instead of plain action names                                                                              |
| `--paranoid`           | `switch` | -                        | Enable paranoid mode - randomize action URL after each invocation (only when --obfuscate is specified)                                           |
| `--ha-discovery`       | `switch` | -                        | Enable Home Assistant autodiscovery                                                                                                              |
| `--ha-discovery-topic` | `string` | `homeassistant`          | Home Assistant autodiscovery topic                                                                                                               |
| `--ha-device-name`     | `string` | `Foscam VD1`             | The name of the device being published in Home Assistant                                                                                         |
| `--log-level`          | `choice` | `info`                   | Log level, options: debug, info, warning, error                                                                                                  |
| `--date-format'`       | `string` | `%Y-%m-%d %H:%M:%S'`     | Date/time format for logging and MQTT payloads ([strftime](https://docs.python.org/3/library/datetime.html#strftime-strptime-behavior) template) |
| `--quiet`              | `switch` | -                        | Show only error and critical messages in console, regardless of the log level                                                                    |

If you want the server to auto-configure your Foscam device, please make sure you can reach the device: ```http://10.0.0.2:88/cgi-bin/CGIProxy.fcgi?usr=mqtt&pwd=supersecret&cmd=getDevInfo``` Also make sure you can reach the webhook: ```http://10.10.0.1:5000/``` should show something like: *1970-01-01T13:37:59Z ERROR - no action specified*

## Known issues
- Alerting to URLs should already be enabled for this to work. Automating this is on the to do list below.
- Switch states are only retrieved at startup. If you change image settings or volume through the app, this will not be reflected in MQTT/HA.

## To do

- [x] Build and publish to a Docker repository
- [ ] Check/adjust if alerting to URL is enabled in Foscam device
- [ ] Poll for device settings periodically
- [ ] Add more settings (in order of appearance)
  - [x] Reboot button
  - [x] Mirror/flip screen
  - [x] Status LED
  - [ ] Night mode (on/off/auto)
  - [ ] Speaker volume (if anybody has a clue about the CGI cmd, please let me know!)
  - [ ] Sensitivity for detection options
- [ ] Send detected faces to Deepstack