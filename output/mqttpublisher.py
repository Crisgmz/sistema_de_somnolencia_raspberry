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
        self._stop_event = threading.Event()
        self._client: Optional[mqtt_client.Client] = None
        self._last_publish = 0.0
        self._level = 0
        self._latest_telemetry: Dict = {}
        self._missing_host_warned = False
        self._connected = False
        self._last_error = ""
        self._published_count = 0
        self._dropped_count = 0
        self._delivered_count = 0
        self._last_log_ts = 0.0
        self._LOG_INTERVAL_S = 5.0

    def _on_connect(self, _client, _userdata, _flags, rc) -> None:
        self._connected = rc == 0
        if rc == 0:
            self._last_error = ""
            topic = self.config.mqtt_topic.format(vehicle_id=self.config.vehicle_id)
            print(f"[MQTT] Conectado a {self.config.mqtt_host}:{self.config.mqtt_port} | topic={topic}")
        else:
            self._last_error = f"connect_rc={rc}"
            print(f"[MQTT] Conexion rechazada, rc={rc}")

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            self._last_error = f"disconnect_rc={rc}"
            print(f"[MQTT] Desconectado inesperadamente, rc={rc}. Reintentando...")

    def _on_publish(self, _client, _userdata, mid) -> None:
        self._delivered_count += 1

    def connect_client(self) -> None:
        if not self.config.mqtt_host:
            if not self._missing_host_warned:
                print("[MQTT] EMQX_HOST vacio. Configura .env para habilitar envio MQTT.")
                self._missing_host_warned = True
            raise RuntimeError("missing mqtt host")

        client = mqtt_client.Client(client_id=self.config.mqtt_client_id, protocol=mqtt_client.MQTTv311)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        if self.config.mqtt_username:
            client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        if self.config.mqtt_tls:
            if self.config.mqtt_ca_cert:
                client.tls_set(ca_certs=self.config.mqtt_ca_cert, cert_reqs=ssl.CERT_REQUIRED)
            else:
                client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        client.loop_start()
        self._client = client

    def enqueue(self, payload: Dict) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            self._dropped_count += 1
            if payload.get("kind") != "immediate":
                self._latest_telemetry = payload.get("payload", {})

    def set_level(self, level: int) -> None:
        self._level = max(0, min(4, int(level)))

    def stats(self) -> Dict:
        return {
            "connected": self._connected,
            "last_publish_ts": self._last_publish,
            "published_count": self._published_count,
            "delivered_count": self._delivered_count,
            "dropped_count": self._dropped_count,
            "queue_size": self.queue.qsize(),
            "level": self._level,
            "last_error": self._last_error,
        }

    def _publish_now(self, payload: Dict, is_immediate: bool = False) -> None:
        if not self._client:
            return
        topic = self.config.mqtt_topic.format(vehicle_id=self.config.vehicle_id)
        try:
            data = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._last_error = f"json_encode: {exc}"
            return
        result = self._client.publish(topic, data, qos=self.config.mqtt_qos)
        now = time.time()
        if result.rc != mqtt_client.MQTT_ERR_SUCCESS:
            self._last_error = f"publish_rc={result.rc}"
            print(f"[MQTT] Error publicando en {topic}, rc={result.rc}")
        else:
            self._published_count += 1
            self._last_error = ""
            if is_immediate or (now - self._last_log_ts) >= self._LOG_INTERVAL_S:
                msg_kind = "emergencia" if is_immediate else "telemetria"
                print(f"[MQTT] Publicado ({msg_kind}) topic={topic} pub={self._published_count} dlv={self._delivered_count}")
                self._last_log_ts = now
        self._last_publish = now

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not self._client:
                try:
                    self.connect_client()
                except Exception as exc:
                    self._last_error = str(exc)
                    self._stop_event.wait(1.5)
                    continue

            try:
                msg = self.queue.get(timeout=0.2)
                if msg.get("kind") == "immediate":
                    self._publish_now(msg["payload"], is_immediate=True)
                else:
                    self._latest_telemetry = msg.get("payload", {})
            except queue.Empty:
                pass

            interval = self.LEVEL_INTERVAL.get(self._level, 5.0)
            if self._latest_telemetry and (time.time() - self._last_publish) >= interval:
                self._publish_now(self._latest_telemetry)
                self._latest_telemetry = {}

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=1.5)
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
