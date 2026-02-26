"""Publicador MQTT no bloqueante con frecuencia dinamica por nivel."""

from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from typing import Dict, Optional

from paho.mqtt import client as mqtt_client

from core.config import AppConfig


class MqttPublisher(threading.Thread):
    LEVEL_INTERVAL = {0: 10.0, 1: 5.0, 2: 5.0, 3: 2.0, 4: 1.0}

    def __init__(self, config: AppConfig) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.queue: "queue.Queue[Dict]" = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._client: Optional[mqtt_client.Client] = None
        self._last_publish = 0.0
        self._level = 0
        self._latest_telemetry: Dict = {}

    def connect_client(self) -> None:
        client = mqtt_client.Client(client_id=self.config.mqtt_client_id, protocol=mqtt_client.MQTTv311)
        if self.config.mqtt_username:
            client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        if self.config.mqtt_tls:
            if self.config.mqtt_ca_cert:
                client.tls_set(ca_certs=self.config.mqtt_ca_cert, cert_reqs=ssl.CERT_REQUIRED)
            else:
                client.tls_set()
        client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        client.loop_start()
        self._client = client

    def enqueue(self, payload: Dict) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass

    def set_level(self, level: int) -> None:
        self._level = max(0, min(4, int(level)))

    def _publish_now(self, payload: Dict) -> None:
        if not self._client:
            return
        topic = self.config.mqtt_topic.format(vehicle_id=self.config.vehicle_id)
        self._client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=self.config.mqtt_qos)
        self._last_publish = time.time()

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._client:
                try:
                    self.connect_client()
                except Exception:
                    self._stop.wait(1.5)
                    continue

            try:
                msg = self.queue.get(timeout=0.2)
                if msg.get("kind") == "immediate":
                    self._publish_now(msg["payload"])
                else:
                    self._latest_telemetry = msg.get("payload", {})
            except queue.Empty:
                pass

            interval = self.LEVEL_INTERVAL.get(self._level, 5.0)
            if self._latest_telemetry and (time.time() - self._last_publish) >= interval:
                self._publish_now(self._latest_telemetry)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=1.5)
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
