import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.preparacion import cuentas_destino

PATRON_MEDICION = re.compile(r"^\s*(IG|INSTAGRAM)\s+(\S+?)\s*:\s*([\d.,]+)\s*/\s*(\d+)\s*%?\s*/\s*(s[ií]|no)\s*$",
                             re.I | re.M)


@dataclass
class Servicios:
    preparador: object
    youtube: dict = field(default_factory=dict)


def encolar_momentos(bd, momentos, ahora_s):
    for m in momentos:
        bd.encolar(m["representante"], m["momento_id"], m.get("nota") or m.get("nota_maxima"), ahora_s)


def _cuenta(config, cuenta_id):
    return next(c for c in config.cuentas if c["id"] == cuenta_id)


def _pie(cuenta, version, cola, youtube_listo, aviso_youtube):
    lineas = [f"Cuenta: {cuenta['id']} | viralidad {cola['nota'] or '?'}/10 | gancho ({version['origen_gancho']}): "
              f"{version['gancho']}"]
    if version["con_aviso"]:
        lineas.append("Version de CAMPANA: lleva '#Ad' en el video y en el texto.")
    for aviso in json.loads(version["avisos"] or "[]"):
        lineas.append(f"ATENCION: {aviso}")
    if youtube_listo:
        lineas.append("YouTube: subido en PRIVADO. Pulsa 'Publicar en YouTube' para hacerlo publico.")
    else:
        lineas.append("YouTube: subelo tu a mano (publicacion automatica desactivada o sin autorizar).")
    if aviso_youtube:
        lineas.append(f"ATENCION: {aviso_youtube}")
    return "\n".join(lineas)


def _texto_para_copiar(cuenta, textos, con_aviso, recordatorio):
    partes = [f"Textos para la cuenta {cuenta['id']}", "", "INSTAGRAM (copia todo esto):", textos["instagram_texto"], "",
              "YOUTUBE (solo si lo subes a mano):", f"Titulo: {textos['youtube_titulo']}", textos["youtube_descripcion"]]
    if con_aviso:
        partes += ["", "Instagram: activa tambien 'Etiqueta de colaboracion pagada' si la marca te lo permite."]
    return "\n".join(partes + ["", recordatorio])


def procesar_cola(config, bd, telegram, servicios, ahora_s) -> int:
    enviados = 0
    lanzamiento = date.fromisoformat(config.fecha_lanzamiento_gta_vi)
    for cola in bd.pendientes_de_preparar(config.max_videos_por_ejecucion):
        clip = bd.clip(cola["clip_id"])
        destino, motivo = cuentas_destino(clip, config.cuentas, config.juegos, lanzamiento)
        if not destino:
            bd.marcar_cola(cola["clip_id"], "bloqueado", motivo)
            telegram.enviar(f"No preparo el video de {clip.get('url', cola['clip_id'])}: {motivo}")
            bd.guardar()
            enviados += 1
            continue
        bd.marcar_cola(cola["clip_id"], "en_curso")
        bd.guardar()
        try:
            resultado = servicios.preparador.preparar(clip, destino, config.juegos)
        except Exception as e:
            bd.marcar_cola(cola["clip_id"], "error", str(e))
            bd.guardar()
            telegram.enviar(f"No he podido preparar el video de {clip.get('url', '')}:\n{e}\n"
                            f"Puedes reintentarlo con: python3 clipper.py preparar-clip {cola['clip_id']}")
            enviados += 1
            continue
        for v in resultado["versiones"]:
            enviados += _enviar_version(config, bd, telegram, servicios, cola, clip, v, resultado, ahora_s)
        bd.marcar_cola(cola["clip_id"], "preparado")
        bd.guardar()
    return enviados


def _enviar_version(config, bd, telegram, servicios, cola, clip, v, resultado, ahora_s) -> int:
    cuenta = _cuenta(config, v["cuenta"])
    version = {"clip_id": clip["clip_id"], "momento_id": cola["momento_id"], "cuenta": v["cuenta"],
               "con_aviso": int(v["con_aviso"]), "video": v["video"], "textos": json.dumps(v["textos"], ensure_ascii=False),
               "gancho": resultado["gancho"], "origen_gancho": resultado["origen_gancho"],
               "avisos": json.dumps(resultado["avisos"], ensure_ascii=False), "nota": cola["nota"],
               "estado": "preparado", "creado_s": ahora_s}
    version_id = bd.nueva_version(version)
    youtube = servicios.youtube.get(v["cuenta"]) if cuenta.get("youtube_automatico") else None
    aviso_youtube = ""
    if youtube:
        try:
            youtube_id, aviso_youtube = youtube.subir(Path(v["video"]), v["textos"]["youtube_titulo"],
                                                      v["textos"]["youtube_descripcion"], bool(v["con_aviso"]))
            bd.actualizar_version(version_id, youtube_id=youtube_id)
        except Exception as e:
            youtube, aviso_youtube = None, f"no se pudo subir a YouTube ({e})"
    botones = [[{"text": "Publicar en YouTube", "callback_data": f"p:{version_id}"},
                {"text": "Descartar", "callback_data": f"d:{version_id}"}]] if youtube else None
    mensaje = telegram.enviar_video(Path(v["video"]), _pie(cuenta, version, cola, bool(youtube), aviso_youtube), botones)
    telegram.enviar(_texto_para_copiar(cuenta, v["textos"], v["con_aviso"], config.recordatorio_antes_de_publicar))
    bd.actualizar_version(version_id, estado="enviado", mensaje_id=(mensaje or {}).get("message_id"),
                          detalle=aviso_youtube)
    bd.guardar()
    return 2


def procesar_telegram(config, estado, bd, telegram, servicios, ahora_s) -> int:
    procesados = 0
    actualizaciones = telegram.actualizaciones(estado.get("telegram_siguiente_update", 0))
    for u in actualizaciones:
        estado["telegram_siguiente_update"] = u["update_id"] + 1
        boton = u.get("callback_query")
        mensaje = u.get("message")
        if boton and str(((boton.get("message") or {}).get("chat") or {}).get("id")) == str(telegram.chat_id):
            _pulsar_boton(bd, telegram, servicios, boton, ahora_s)
            procesados += 1
        elif mensaje and str((mensaje.get("chat") or {}).get("id")) == str(telegram.chat_id):
            procesados += _leer_medicion(config, estado, bd, telegram, mensaje.get("text") or "", ahora_s)
    bd.guardar()
    return procesados


def _responder(telegram, callback_id, texto):
    try:
        telegram.responder_boton(callback_id, texto)
    except Exception:
        pass


def _pulsar_boton(bd, telegram, servicios, boton, ahora_s):
    accion, _, identificador = (boton.get("data") or "").partition(":")
    version = bd.version(int(identificador)) if identificador.isdigit() else {}
    if not version or version["estado"] != "enviado":
        _responder(telegram, boton["id"], "Ya estaba resuelto")
        estado = version.get("estado", "desconocido") if version else "desconocido"
        telegram.enviar(f"Ese video ya estaba resuelto (estado: {estado}). No he hecho nada.")
        mensaje_id = (boton.get("message") or {}).get("message_id")
        if mensaje_id:
            try:
                telegram.quitar_botones(mensaje_id)
            except Exception:
                pass
        return
    youtube = servicios.youtube.get(version["cuenta"])
    try:
        if accion == "p":
            youtube.hacer_publico(version["youtube_id"])
            bd.actualizar_version(version["version_id"], estado="publicado", publicado_s=ahora_s)
            bd.guardar()
            _responder(telegram, boton["id"], "Publicado en YouTube")
            telegram.enviar(f"Publicado en YouTube ({version['cuenta']}): https://youtube.com/shorts/{version['youtube_id']}")
        else:
            if youtube and version["youtube_id"]:
                youtube.borrar(version["youtube_id"])
            bd.actualizar_version(version["version_id"], estado="descartado")
            bd.guardar()
            _responder(telegram, boton["id"], "Descartado")
            telegram.enviar(f"Descartado ({version['cuenta']}): el video se ha borrado de YouTube.")
    except Exception as e:
        _responder(telegram, boton["id"], "Ha fallado; mira el mensaje")
        telegram.enviar(f"No he podido completar la accion en YouTube ({version['cuenta']}): {e}")
        return
    if version["mensaje_id"]:
        try:
            telegram.quitar_botones(version["mensaje_id"])
        except Exception:
            pass


def _leer_medicion(config, estado, bd, telegram, texto, ahora_s) -> int:
    filas = PATRON_MEDICION.findall(texto)
    if not filas:
        return 0
    semana = estado.get("ultimo_resumen_semanal") or date.fromtimestamp(ahora_s).isoformat()
    guardadas = []
    for _, cuenta, vistas, porcentaje, no_recomendable in filas:
        numero = int(re.sub(r"[.,]", "", vistas))
        bd.guardar_medicion_manual(semana, "instagram", cuenta.lower(), numero, int(porcentaje),
                                   "si" if no_recomendable.lower().startswith("s") else "no", ahora_s)
        guardadas.append(f"{cuenta}: {numero} vis, {porcentaje}% EE. UU., no recomendable: {no_recomendable}")
    telegram.enviar("Guardado (semana del " + semana + "):\n" + "\n".join(guardadas))
    return 1


def medir(bd, servicios, ahora_s) -> int:
    pendientes = bd.versiones_para_medir(ahora_s)
    por_cuenta = {}
    for v in pendientes:
        por_cuenta.setdefault(v["cuenta"], []).append(v)
    medidos = 0
    for cuenta, versiones in por_cuenta.items():
        youtube = servicios.youtube.get(cuenta)
        if not youtube:
            continue
        vistas = youtube.visualizaciones([v["youtube_id"] for v in versiones])
        for v in versiones:
            if v["youtube_id"] not in vistas:
                continue
            edad = ahora_s - v["publicado_s"]
            cambios = {}
            for columna, segundos in (("vistas_1h", 3600), ("vistas_24h", 86400), ("vistas_7d", 7 * 86400)):
                if v[columna] is None and edad >= segundos:
                    cambios[columna] = vistas[v["youtube_id"]]
            if cambios:
                bd.actualizar_version(v["version_id"], **cambios)
                medidos += 1
    bd.guardar()
    return medidos


def resumen_semanal(config, estado, bd, telegram, servicios, ahora: datetime) -> int:
    local = ahora.astimezone(ZoneInfo(config.zona_horaria))
    hoy = local.date().isoformat()
    if local.weekday() != config.resumen_semanal_dia or local.hour < config.resumen_semanal_hora \
            or estado.get("ultimo_resumen_semanal") == hoy:
        return 0
    lineas = ["Resumen semanal (ultimos 7 dias)", "", "YOUTUBE:"]
    for cuenta in config.cuentas:
        youtube = servicios.youtube.get(cuenta["id"])
        if not youtube:
            lineas.append(f"- {cuenta['id']}: sin autorizar")
            continue
        try:
            paises = youtube.paises_ultimos_dias()
            porcentaje = "?" if paises["porcentaje_tier1"] is None else f"{paises['porcentaje_tier1']}%"
            lineas.append(f"- {cuenta['id']}: {paises['visualizaciones']} visualizaciones | publico Tier 1: {porcentaje}")
        except Exception as e:
            lineas.append(f"- {cuenta['id']}: no se pudieron leer los datos ({e})")
    publicados = bd.versiones_publicadas_desde(int((ahora - timedelta(days=7)).timestamp()))
    lineas.append(f"Shorts publicados esta semana: {len(publicados)}")
    lineas += ["", "INSTAGRAM: responde a este mensaje copiando y rellenando estas lineas:"]
    lineas += [f"IG {c['id']}: visualizaciones / % EE. UU. / no recomendable si-no" for c in config.cuentas]
    lineas += ["Ejemplo:  IG kai: 12500 / 45 / no",
               "Donde mirarlo: Perfil > Panel profesional > Estadisticas (visualizaciones y paises).",
               "'No recomendable': Configuracion > Estado de la cuenta."]
    telegram.enviar("\n".join(lineas))
    estado["ultimo_resumen_semanal"] = hoy
    return 1


def limpiar_videos(config, bd, carpeta_videos: Path, ahora_s):
    for v in bd.videos_antiguos(ahora_s - config.borrar_videos_preparados_tras_dias * 86400):
        carpeta = Path(v["video"]).parent
        if carpeta.is_dir() and carpeta.parent == carpeta_videos:
            shutil.rmtree(carpeta, ignore_errors=True)
        bd.actualizar_version(v["version_id"], video="")
