import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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


def procesar_cola(config, bd, telegram, servicios, ahora_s, solo_clip=None) -> int:
    enviados = 0
    lanzamiento = date.fromisoformat(config.fecha_lanzamiento_gta_vi)
    for cola in bd.pendientes_de_preparar(config.max_videos_por_ejecucion, solo_clip):
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
    bd.guardar()
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


CLAVE_OFFSET = "telegram_offset"
CLAVE_LATIDO = "telegram_latido_s"
CLAVE_APLAZADOS = "telegram_aplazados"
CLAVE_FALLOS = "telegram_fallos"
CLAVE_ULTIMO_MEJORES = "telegram_ultimo_mejores_s"
MAX_FALLOS_POR_ACTUALIZACION = 3
MINUTOS_ESPERANDO_VIDEO = 10
SEGUNDOS_ENTRE_MEJORES = 180
SEGUNDOS_LATIDO_VALIDO = 180


def escucha_telegram_activa(bd, ahora_s) -> bool:
    return ahora_s - int(bd.valor(CLAVE_LATIDO) or 0) <= SEGUNDOS_LATIDO_VALIDO


def _registrar_boton(bd, ahora_s, update_id, datos, resultado):
    ruta = Path(bd.ruta).parent / "registro_botones.csv"
    nueva = not ruta.exists()
    with ruta.open("a", encoding="utf-8") as f:
        if nueva:
            f.write("hora_utc,update_id,boton,resultado\n")
        hora = datetime.fromtimestamp(ahora_s, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{hora},{update_id},{datos},{str(resultado).replace(',', ';')}\n")


def procesar_telegram(config, estado, bd, telegram, servicios, ahora_s, espera_s=0) -> int:
    guardado = bd.valor(CLAVE_OFFSET)
    offset = int(guardado) if guardado is not None else int(estado.get("telegram_siguiente_update", 0))
    procesados = _reintentar_aplazados(bd, telegram, servicios, ahora_s)
    for u in telegram.actualizaciones(offset, espera_s):
        if u["update_id"] < offset:
            continue
        try:
            procesados += _atender(config, estado, bd, telegram, servicios, u, ahora_s)
        except Exception as e:
            fallos = json.loads(bd.valor(CLAVE_FALLOS) or "{}").get(str(u["update_id"]), 0) + 1
            if fallos < MAX_FALLOS_POR_ACTUALIZACION:
                bd.fijar_valor(CLAVE_FALLOS, json.dumps({str(u["update_id"]): fallos}))
                bd.guardar()
                raise
            _registrar_boton(bd, ahora_s, u["update_id"], (u.get("callback_query") or {}).get("data", "mensaje"),
                             f"abandonado tras {fallos} fallos: {e}")
            try:
                telegram.enviar(f"No he podido atender tu ultima pulsacion o mensaje ({fallos} intentos): {e}\n"
                                f"Si era un boton, vuelve a pulsarlo.")
            except Exception:
                pass
        offset = u["update_id"] + 1
        bd.fijar_valor(CLAVE_FALLOS, "{}")
        bd.fijar_valor(CLAVE_OFFSET, str(offset))
        bd.guardar()
    return procesados


def _atender(config, estado, bd, telegram, servicios, u, ahora_s) -> int:
    boton = u.get("callback_query")
    mensaje = u.get("message")
    if boton:
        chat = str(((boton.get("message") or {}).get("chat") or {}).get("id"))
        if chat != str(telegram.chat_id):
            _registrar_boton(bd, ahora_s, u["update_id"], boton.get("data"), f"ignorado: otro chat ({chat})")
            return 0
        resultado = _pulsar_boton(bd, telegram, servicios, boton, ahora_s)
        _registrar_boton(bd, ahora_s, u["update_id"], boton.get("data"), resultado)
        return 1
    if mensaje and str((mensaje.get("chat") or {}).get("id")) == str(telegram.chat_id):
        texto = mensaje.get("text") or ""
        pedido = PATRON_MEJORES.match(texto)
        if pedido:
            return pedir_mejores(config, bd, telegram, ahora_s, pedido.group(1))
        return _leer_medicion(config, estado, bd, telegram, texto, ahora_s)
    return 0


def _reintentar_aplazados(bd, telegram, servicios, ahora_s) -> int:
    aplazados = json.loads(bd.valor(CLAVE_APLAZADOS) or "[]")
    if not aplazados:
        return 0
    quedan, hechos = [], 0
    for a in aplazados:
        version = bd.version(a["version_id"]) or {}
        caducado = ahora_s - a["desde_s"] > MINUTOS_ESPERANDO_VIDEO * 60
        if version.get("estado") == "preparado" and not caducado:
            quedan.append(a)
            continue
        resultado = _pulsar_boton(bd, telegram, servicios, a["boton"], ahora_s, aplazable=False)
        _registrar_boton(bd, ahora_s, "aplazado", a["boton"].get("data"), resultado)
        hechos += 1
    bd.fijar_valor(CLAVE_APLAZADOS, json.dumps(quedan))
    bd.guardar()
    return hechos


def _aplazar(bd, boton, version_id, ahora_s):
    aplazados = json.loads(bd.valor(CLAVE_APLAZADOS) or "[]")
    aplazados.append({"version_id": version_id, "desde_s": ahora_s, "boton": boton})
    bd.fijar_valor(CLAVE_APLAZADOS, json.dumps(aplazados))
    bd.guardar()


PATRON_MEJORES = re.compile(r"^\s*/?mejores(?:@\w+)?(?:\s+(\w+))?\s*$", re.I)
MEJORES_POR_PETICION = 3
DIAS_PARA_BUSCAR_MEJORES = 14


def pedir_mejores(config, bd, telegram, ahora_s, streamer=None) -> int:
    if ahora_s - int(bd.valor(CLAVE_ULTIMO_MEJORES) or 0) < SEGUNDOS_ENTRE_MEJORES and bd.cola_por_preparar():
        telegram.enviar("Ya estoy preparando los anteriores. Cuando te lleguen, pidemelo otra vez.")
        return 1
    bd.fijar_valor(CLAVE_ULTIMO_MEJORES, str(ahora_s))
    streamers = [c["streamer"] for c in config.cuentas if c["tipo"] == "streamer"]
    streamer = (streamer or (streamers[0] if streamers else "")).lower()
    momentos = bd.mejores_sin_usar(streamer, ahora_s - DIAS_PARA_BUSCAR_MEJORES * 86400, MEJORES_POR_PETICION)
    if not momentos:
        telegram.enviar(f"No quedan momentos de {streamer} sin usar en los ultimos {DIAS_PARA_BUSCAR_MEJORES} dias.")
        return 1
    zona = ZoneInfo(config.zona_horaria)
    lineas = [f"Los {len(momentos)} mejores momentos de {streamer} que aun no has usado. Preparo los videos: "
              f"te llegaran en los proximos minutos, con sus botones."]
    for i, m in enumerate(momentos, 1):
        encolar_momentos(bd, [m], ahora_s)
        cuando = datetime.fromtimestamp(m["inicio_s"], zona).strftime("%d/%m %H:%M")
        lineas.append(f"{i}. Nota {m['nota_maxima']}/10 | {m['vistas']} vis | {cuando} | {m['titulo'] or '(sin titulo)'}\n"
                      f"   {m['url']}")
    bd.guardar()
    telegram.enviar("\n".join(lineas))
    return 1


def _responder(telegram, callback_id, texto):
    try:
        telegram.responder_boton(callback_id, texto)
    except Exception:
        pass


def _quitar_botones(telegram, mensaje_id):
    if mensaje_id:
        try:
            telegram.quitar_botones(mensaje_id)
        except Exception:
            pass


def _pulsar_boton(bd, telegram, servicios, boton, ahora_s, aplazable=True) -> str:
    accion, _, identificador = (boton.get("data") or "").partition(":")
    mensaje_id = (boton.get("message") or {}).get("message_id")
    if accion not in ("p", "d") or not identificador.isdigit():
        _responder(telegram, boton["id"], "No reconozco este boton")
        return "boton desconocido"
    version = bd.version(int(identificador)) or {}
    estado = version.get("estado")
    if estado == "preparado" and aplazable and ahora_s - version["creado_s"] < MINUTOS_ESPERANDO_VIDEO * 60:
        _aplazar(bd, boton, version["version_id"], ahora_s)
        _responder(telegram, boton["id"], "El video se esta terminando de preparar: lo hago en cuanto este listo")
        return "aplazado"
    if estado in ("publicado", "descartado"):
        _responder(telegram, boton["id"], f"Ya estaba {estado}. No hago nada.")
        _quitar_botones(telegram, version.get("mensaje_id") or mensaje_id)
        return f"repetido ({estado})"
    if estado != "enviado":
        _responder(telegram, boton["id"], "No encuentro ese video")
        telegram.enviar(f"No puedo usar ese boton: el video no esta listo (estado: {estado or 'desconocido'}).")
        _quitar_botones(telegram, mensaje_id)
        return f"no disponible ({estado or 'desconocido'})"
    youtube = servicios.youtube.get(version["cuenta"])
    if not youtube:
        _responder(telegram, boton["id"], "Sin permiso de YouTube")
        telegram.enviar(f"No tengo permiso de YouTube para la cuenta {version['cuenta']}. Renuevalo con:\n"
                        f"python3 clipper.py autorizar-youtube --cuenta {version['cuenta']}\ny vuelve a pulsar el boton.")
        return "sin permiso de youtube"
    try:
        if accion == "p":
            youtube.hacer_publico(version["youtube_id"])
            bd.actualizar_version(version["version_id"], estado="publicado", publicado_s=ahora_s)
            bd.guardar()
            _responder(telegram, boton["id"], "Publicado en YouTube")
            telegram.enviar(f"Publicado en YouTube ({version['cuenta']}): https://youtube.com/shorts/{version['youtube_id']}")
            resultado = "publicado"
        else:
            if version["youtube_id"]:
                try:
                    youtube.borrar(version["youtube_id"])
                except Exception as e:
                    if "404" not in str(e):
                        raise
            bd.actualizar_version(version["version_id"], estado="descartado")
            bd.guardar()
            _responder(telegram, boton["id"], "Descartado")
            telegram.enviar(f"Descartado ({version['cuenta']}): el video se ha borrado de YouTube.")
            resultado = "descartado"
    except Exception as e:
        _responder(telegram, boton["id"], "Ha fallado; mira el mensaje")
        telegram.enviar(f"No he podido completar la accion en YouTube ({version['cuenta']}): {e}\n"
                        f"El video sigue como estaba: puedes volver a pulsar el boton.")
        return f"error: {e}"
    _quitar_botones(telegram, version["mensaje_id"] or mensaje_id)
    return resultado


def escuchar_telegram(config, bd, telegram, servicios, leer_estado, registro=print, reloj=time.time,
                      dormir=time.sleep, vueltas=None, espera_s=25):
    espera = 5
    while vueltas is None or vueltas > 0:
        if vueltas is not None:
            vueltas -= 1
        try:
            bd.fijar_valor(CLAVE_LATIDO, str(int(reloj())))
            bd.guardar()
            actuales = servicios() if callable(servicios) else servicios
            procesar_telegram(config, leer_estado(), bd, telegram, actuales, int(reloj()), espera_s=espera_s)
            espera = 5
        except Exception as e:
            registro(f"Telegram: {type(e).__name__}: {e}. Reintento en {espera} s.")
            dormir(espera)
            espera = min(espera * 2, 120)


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


def medir(bd, servicios, ahora_s, errores=None) -> int:
    pendientes = bd.versiones_para_medir(ahora_s)
    por_cuenta = {}
    for v in pendientes:
        por_cuenta.setdefault(v["cuenta"], []).append(v)
    medidos = 0
    for cuenta, versiones in por_cuenta.items():
        youtube = servicios.youtube.get(cuenta)
        if not youtube:
            continue
        try:
            vistas = youtube.visualizaciones([v["youtube_id"] for v in versiones])
        except Exception as e:
            if errores is not None:
                errores[cuenta] = str(e)
            continue
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
