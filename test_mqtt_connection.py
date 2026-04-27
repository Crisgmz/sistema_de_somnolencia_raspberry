"""Prueba rapida de conexion MQTT usando .env del proyecto."""

from __future__ import annotations

import json
import time
from pathlib import Path

from dotenv import load_dotenv
from paho.mqtt import client as mqtt_client

from core.config import AppConfig


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)
    cfg = AppConfig.from_env()

    if not cfg.mqtt_host:
        raise RuntimeError("EMQX_HOST esta vacio en .env")

    topic = cfg.mqtt_topic.format(vehicle_id=cfg.vehicle_id)
    payload = {
        "v": cfg.vehicle_id,
        "d": cfg.driver_id,
        "ts": int(time.time()),
        "session_id": "mqtt_test",
        "alerts": {"active": False, "level": 0, "reasons": []},
        "emergency": {"active": False, "type": None},
        "sys": {"status": "mqtt_test"},
    }

    transport = "websockets" if cfg.mqtt_transport in {"websocket", "websockets", "ws", "wss"} else "tcp"
    client = mqtt_client.Client(
        client_id=f"{cfg.mqtt_client_id}-probe",
        protocol=mqtt_client.MQTTv311,
        transport=transport,
    )
    if transport == "websockets":
        client.ws_set_options(path=cfg.mqtt_ws_path or "/mqtt")
    if cfg.mqtt_username:
        client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
    if cfg.mqtt_tls:
        if cfg.mqtt_ca_cert:
            client.tls_set(ca_certs=cfg.mqtt_ca_cert)
        else:
            client.tls_set()

    client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
    client.loop_start()
    info = client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=cfg.mqtt_qos)
    info.wait_for_publish(timeout=5.0)
    client.loop_stop()
    client.disconnect()

    if not info.is_published():
        raise RuntimeError("No se pudo confirmar publicacion MQTT")

    print(f"[OK] MQTT publicado en topic={topic} transport={transport}")


if __name__ == "__main__":
    main()
