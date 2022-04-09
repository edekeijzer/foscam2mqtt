FROM python:alpine

RUN python3 -m pip install flask waitress paho-mqtt requests xmltodict Pillow && \
    python3 -m pip uninstall --yes setuptools wheel pip && \
    mkdir /log /fonts

ADD rootfs/ /

ENTRYPOINT ["/usr/local/bin/python3","/app.py"]
