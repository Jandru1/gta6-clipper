import json
import os
from dataclasses import dataclass
from pathlib import Path

MODOS_VALIDOS = {"silencio", "alertas"}
CAMPOS_CREDENCIALES = ("twitch_client_id", "twitch_client_secret", "telegram_bot_token", "telegram_chat_id")
VARIABLES_ENTORNO = {
    "twitch_client_id": "TWITCH_ID_CLIENTE",
    "twitch_client_secret": "TWITCH_SECRETO",
    "telegram_bot_token": "TELEGRAM_GTA6_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}


class ErrorConfiguracion(Exception):
    pass


@dataclass
class Franja:
    desde: str
    hasta: str
    modo: str


@dataclass
class Config:
    zona_horaria: str
    intervalo_minutos: int
    streamers: list
    ventana_clips_horas: int
    max_paginas_clips: int
    max_avisos_por_ejecucion: int
    franjas: list


@dataclass
class Credenciales:
    twitch_client_id: str
    twitch_client_secret: str
    telegram_bot_token: str
    telegram_chat_id: str


def _leer_json(ruta: Path) -> dict:
    if not ruta.exists():
        raise ErrorConfiguracion(f"No existe el fichero {ruta}")
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ErrorConfiguracion(f"El fichero {ruta.name} no es JSON valido (linea {e.lineno}): {e.msg}")


def cargar_config(ruta: Path) -> Config:
    datos = _leer_json(ruta)
    try:
        franjas = [Franja(f["desde"], f["hasta"], f["modo"]) for f in datos["franjas"]]
        config = Config(
            zona_horaria=datos["zona_horaria"],
            intervalo_minutos=int(datos["intervalo_minutos"]),
            streamers=[s.strip().lower() for s in datos["streamers"]],
            ventana_clips_horas=int(datos["ventana_clips_horas"]),
            max_paginas_clips=int(datos["max_paginas_clips"]),
            max_avisos_por_ejecucion=int(datos["max_avisos_por_ejecucion"]),
            franjas=franjas,
        )
    except KeyError as e:
        raise ErrorConfiguracion(f"Falta el campo {e} en {ruta.name}")
    for franja in config.franjas:
        if franja.modo not in MODOS_VALIDOS:
            raise ErrorConfiguracion(f"Modo '{franja.modo}' no valido. Usa: {', '.join(sorted(MODOS_VALIDOS))}")
    if not config.streamers:
        raise ErrorConfiguracion("La lista de streamers esta vacia")
    return config


def guardar_credenciales_desde_entorno(ruta: Path, entorno=os.environ) -> list:
    faltan = [var for var in VARIABLES_ENTORNO.values() if not entorno.get(var, "").strip()]
    if faltan:
        raise ErrorConfiguracion(f"No encuentro estas variables de entorno: {', '.join(faltan)}")
    datos = {campo: entorno[var].strip() for campo, var in VARIABLES_ENTORNO.items()}
    temporal = ruta.with_suffix(".tmp")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2)
    os.replace(temporal, ruta)
    os.chmod(ruta, 0o600)
    return list(VARIABLES_ENTORNO.values())


def cargar_credenciales(ruta: Path, exigir_telegram: bool = True) -> Credenciales:
    datos = _leer_json(ruta)
    faltan = [c for c in CAMPOS_CREDENCIALES if not str(datos.get(c, "")).strip()]
    if not exigir_telegram:
        faltan = [c for c in faltan if not c.startswith("telegram")]
    if faltan:
        raise ErrorConfiguracion(f"Faltan credenciales en {ruta.name}: {', '.join(faltan)}")
    return Credenciales(**{c: str(datos.get(c, "")).strip() for c in CAMPOS_CREDENCIALES})
