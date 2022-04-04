FROM python:alpine

RUN python3 -m pip install flask waitress paho-mqtt requests xmltodict Pillow && \
    python3 -m pip uninstall --yes setuptools wheel pip && \
    mkdir /log /fonts

COPY foscam2mqtt.py /foscam2mqtt.py
COPY noto.ttf /fonts/noto.ttf

ENTRYPOINT ["/usr/local/bin/python3","/foscam2mqtt.py"]
