import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

MODOS_VALIDOS = {"silencio", "alertas"}
CAMPOS_CREDENCIALES = ("twitch_client_id", "twitch_client_secret", "telegram_bot_token", "telegram_chat_id")
VARIABLES_ENTORNO = {
    "twitch_client_id": "TWITCH_ID_CLIENTE",
    "twitch_client_secret": "TWITCH_SECRETO",
    "telegram_bot_token": "TELEGRAM_GTA6_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}
MAXIMO_CLIPS_POR_PAGINA_TWITCH = 100


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
    streamers: list
    franjas: list
    ejecucion_automatica_cada_minutos: int
    repaso_reciente_cada_minutos: int
    horas_que_cubre_el_repaso_reciente: int
    repaso_completo_cada_minutos: int
    horas_que_cubre_el_repaso_completo: int
    minutos_por_trozo_de_consulta: int
    minutos_de_solape_entre_trozos: int
    clips_por_pagina: int
    max_paginas_de_100_clips_por_trozo: int
    avisar_si_tiene_al_menos_visualizaciones: int
    avisar_si_se_creo_hace_menos_de_minutos: int
    max_avisos_por_ejecucion: int
    resumen_de_funcionamiento_cada_horas: int
    clips_en_el_resumen: int
    viralidad_minutos_para_medir_crecimiento: int
    viralidad_minutos_minimos_de_calculo: int
    viralidad_dias_de_clips_para_comparar: int
    viralidad_clips_minimos_para_dar_nota: int
    no_limpiar_clips_con_nota_igual_o_mayor_que: int
    apuntar_cuando_llega_a_visualizaciones: int
    guardar_historial_desde_visualizaciones: int
    limpieza_cada_minutos: int
    limpieza_rapida_tras_horas: int
    limpieza_rapida_si_tiene_menos_de_visualizaciones: int
    limpieza_no_virales_tras_dias: int
    compactar_base_datos_cada_horas: int


CAMPOS_ENTEROS = [f.name for f in fields(Config) if f.type is int or f.type == "int"]


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
    valores = {}
    for campo in fields(Config):
        if campo.name not in datos:
            raise ErrorConfiguracion(f"Falta el parametro '{campo.name}' en {ruta.name}")
        valores[campo.name] = datos[campo.name]
    for nombre in CAMPOS_ENTEROS:
        valor = valores[nombre]
        if not isinstance(valor, int) or isinstance(valor, bool) or valor < 0:
            raise ErrorConfiguracion(f"'{nombre}' debe ser un numero entero mayor o igual que 0 (ahora: {valor!r})")
    valores["streamers"] = [s.strip().lower() for s in valores["streamers"]]
    try:
        valores["franjas"] = [Franja(f["desde"], f["hasta"], f["modo"]) for f in valores["franjas"]]
    except (KeyError, TypeError):
        raise ErrorConfiguracion("Cada franja necesita 'desde', 'hasta' y 'modo'")
    config = Config(**valores)
    _validar(config)
    return config


def _validar(c: Config):
    errores = []
    if not c.streamers:
        errores.append("la lista 'streamers' esta vacia")
    for franja in c.franjas:
        if franja.modo not in MODOS_VALIDOS:
            errores.append(f"modo '{franja.modo}' no valido (usa: {', '.join(sorted(MODOS_VALIDOS))})")
    for nombre in ("ejecucion_automatica_cada_minutos", "repaso_reciente_cada_minutos", "horas_que_cubre_el_repaso_reciente",
                   "repaso_completo_cada_minutos", "horas_que_cubre_el_repaso_completo", "minutos_por_trozo_de_consulta",
                   "clips_por_pagina", "max_paginas_de_100_clips_por_trozo", "viralidad_minutos_para_medir_crecimiento",
                   "viralidad_minutos_minimos_de_calculo", "limpieza_cada_minutos"):
        if getattr(c, nombre) < 1:
            errores.append(f"'{nombre}' debe ser 1 o mas")
    if c.clips_por_pagina > MAXIMO_CLIPS_POR_PAGINA_TWITCH:
        errores.append(f"'clips_por_pagina' no puede superar {MAXIMO_CLIPS_POR_PAGINA_TWITCH} (limite de Twitch)")
    if c.repaso_reciente_cada_minutos < c.ejecucion_automatica_cada_minutos:
        errores.append("'repaso_reciente_cada_minutos' no puede ser menor que 'ejecucion_automatica_cada_minutos'")
    if c.repaso_completo_cada_minutos < c.ejecucion_automatica_cada_minutos:
        errores.append("'repaso_completo_cada_minutos' no puede ser menor que 'ejecucion_automatica_cada_minutos'")
    if c.horas_que_cubre_el_repaso_completo < c.horas_que_cubre_el_repaso_reciente:
        errores.append("'horas_que_cubre_el_repaso_completo' no puede ser menor que 'horas_que_cubre_el_repaso_reciente'")
    if c.limpieza_rapida_tras_horas <= c.horas_que_cubre_el_repaso_completo:
        errores.append("'limpieza_rapida_tras_horas' debe ser mayor que 'horas_que_cubre_el_repaso_completo' "
                       "(si no, los clips borrados volverian a aparecer como nuevos)")
    if not 1 <= c.no_limpiar_clips_con_nota_igual_o_mayor_que <= 10:
        errores.append("'no_limpiar_clips_con_nota_igual_o_mayor_que' debe estar entre 1 y 10")
    if errores:
        raise ErrorConfiguracion("config.json tiene errores:\n  - " + "\n  - ".join(errores))


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
