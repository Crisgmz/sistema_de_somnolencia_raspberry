"""Configuracion de app para Raspberry Pi Somnolencia."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    camera_index: int = 0
    mqtt_host: str = ""
    mqtt_port: int = 8883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic: str = "test/connection"
    mqtt_supervisor_topic: str = ""  # vacio = topic principal + "/supervisor"
    mqtt_transport: str = "tcp"
    mqtt_ws_path: str = "/mqtt"
    mqtt_tls: bool = True
    mqtt_qos: int = 1
    mqtt_client_id: str = "raspi-somnoalert"
    mqtt_ca_cert: str = ""
    vehicle_id: str = "vehicle_unknown"
    driver_id: str = "driver_unknown"
    supabase_url: str = ""
    supabase_key: str = ""
    sqlite_queue_path: str = "somnolencia_queue.db"

    @classmethod
    def from_env(cls) -> "AppConfig":
        tls_raw = os.getenv("EMQX_TLS", "true").strip().lower()
        return cls(
            camera_index=int(os.getenv("CAMERA_INDEX", "0")),
            mqtt_host=os.getenv("EMQX_HOST", ""),
            mqtt_port=int(os.getenv("EMQX_PORT", "8883")),
            mqtt_username=os.getenv("EMQX_USERNAME", ""),
            mqtt_password=os.getenv("EMQX_PASSWORD", ""),
            mqtt_topic=os.getenv("MQTT_TOPIC", "test/connection"),
            mqtt_supervisor_topic=os.getenv("MQTT_SUPERVISOR_TOPIC", ""),
            mqtt_transport=os.getenv("MQTT_TRANSPORT", "tcp").strip().lower(),
            mqtt_ws_path=os.getenv("MQTT_WS_PATH", "/mqtt"),
            mqtt_tls=tls_raw in {"1", "true", "yes", "on"},
            mqtt_qos=int(os.getenv("MQTT_QOS", "1")),
            mqtt_client_id=os.getenv("MQTT_CLIENT_ID", "raspi-somnoalert"),
            mqtt_ca_cert=os.getenv("MQTT_CA_CERT_PATH", ""),
            vehicle_id=os.getenv("VEHICLE_ID", "vehicle_unknown"),
            driver_id=os.getenv("DRIVER_ID", "driver_unknown"),
            supabase_url=os.getenv("SUPABASE_URL", ""),
            supabase_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_ANON_KEY", "")),
            sqlite_queue_path=os.getenv("SQLITE_QUEUE_PATH", "somnolencia_queue.db"),
        )
