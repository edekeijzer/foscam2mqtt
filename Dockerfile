FROM python:alpine

RUN python3 -m pip install flask waitress paho-mqtt requests xmltodict && \
    python3 -m pip uninstall --yes setuptools wheel pip

COPY foscam2mqtt.py /foscam2mqtt.py

ENTRYPOINT ["/usr/local/bin/python3","/foscam2mqtt.py"]
