import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import momentos as mom
from app import videos
from app.basedatos import texto_local
from app.franjas import modo_actual

MARGEN_PUNTUALIDAD_S = 30


def iso_utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hora_local(momento, zona: str) -> str:
    if isinstance(momento, str):
        if not momento:
            return ""
        momento = datetime.fromisoformat(momento.replace("Z", "+00:00"))
    return momento.astimezone(ZoneInfo(zona)).strftime("%d/%m/%Y %H:%M:%S")


def tramos(ahora: datetime, horas: int, tramo_minutos: int, solape_minutos: int) -> list:
    inicio_total = ahora - timedelta(hours=horas)
    resultado, hasta = [], ahora
    while hasta > inicio_total:
        desde = max(hasta - timedelta(minutes=tramo_minutos + solape_minutos), inicio_total)
        resultado.append((iso_utc(desde), iso_utc(hasta)))
        hasta -= timedelta(minutes=tramo_minutos)
    return resultado


def _toca(estado, clave, cada_minutos, ahora_s):
    ultimo = estado.get(clave)
    return ultimo is None or ahora_s - ultimo >= cada_minutos * 60 - MARGEN_PUNTUALIDAD_S


def _texto_nota(nota):
    return "sin datos suficientes" if nota is None else f"{nota}/10"


def _texto_aviso(momento, clip, ahora_s, zona):
    temprana = momento["velocidad_temprana"]
    return (
        f"Clip con potencial de {momento['streamer']}\n"
        f"{clip.get('titulo') or '(sin titulo)'}\n"
        f"Viralidad: {_texto_nota(momento.get('nota'))} | {clip.get('visualizaciones', 0)} visualizaciones "
        f"(momento: {momento['clips']} clips, {momento['vistas']} vis)\n"
        f"Por que: {'?' if temprana is None else round(temprana, 1)} vis/min al principio | "
        f"{round(momento['velocidad_actual'])} vis/hora ahora | clipeado por {momento['consenso']} persona(s)\n"
        f"Inicio del momento: {texto_local(momento['inicio_s'], zona)} (hace {(ahora_s - momento['inicio_s']) // 60} min)\n"
        f"{clip.get('url', '')}"
    )


def ejecutar_una_vez(config, almacen, bd, twitch, telegram, ahora=None, reloj=time.time, servicios=None):
    inicio = reloj()
    ahora = ahora or datetime.now(timezone.utc)
    ahora_s = int(ahora.timestamp())
    modo = modo_actual(config.franjas, ahora.astimezone(ZoneInfo(config.zona_horaria)))
    resumen = {
        "hora_espana": hora_local(ahora, config.zona_horaria), "inicio_utc": iso_utc(ahora), "resultado": "ok",
        "modo": modo, "repaso": "ninguno", "streamers_en_directo": 0, "espectadores": 0, "tramos_consultados": 0,
        "tramos_truncados": 0, "clips_recibidos": 0, "clips_nuevos": 0, "clips_con_cambios": 0,
        "avisos_enviados": 0, "clips_borrados": 0, "historiales_resumidos": 0, "error": "",
    }
    estado = almacen.estado
    estado.pop("clips_vistos", None)
    try:
        base_vacia = bd.totales()["clips_en_base"] == 0
        ids = estado.setdefault("ids_streamers", {})
        pendientes = [s for s in config.streamers if s not in ids]
        if pendientes:
            ids.update(twitch.ids_de_usuarios(pendientes))
        desconocidos = [s for s in config.streamers if s not in ids]

        directos = twitch.directos(config.streamers)
        for streamer, d in directos.items():
            bd.guardar_directo(streamer, ahora_s, int(d.get("viewer_count", 0)), d.get("game_name", ""))
        resumen["streamers_en_directo"] = len(directos)
        resumen["espectadores"] = sum(int(d.get("viewer_count", 0)) for d in directos.values())

        observaciones = _repasar(config, estado, bd, twitch, ids, ahora, ahora_s, resumen)
        momentos = actualizar_momentos(config, bd, ahora_s) if observaciones else []
        resumen["momentos_puntuados"] = len(momentos)
        bd.guardar()

        con_videos = servicios is not None and config.preparar_videos
        if con_videos:
            videos.procesar_telegram(config, estado, bd, telegram, servicios, ahora_s)
        if modo == "alertas":
            if estado.get("ultimo_modo") == "silencio" and not base_vacia:
                resumen["avisos_enviados"] += _resumen_al_despertar(config, bd, telegram, ahora_s, con_videos)
            resumen["avisos_enviados"] += _avisar(config, bd, telegram, observaciones, momentos, ahora_s, base_vacia,
                                                  desconocidos, con_videos)
        estado["ultimo_modo"] = modo
        bd.guardar()
        if con_videos and modo == "alertas":
            resumen["avisos_enviados"] += videos.procesar_cola(config, bd, telegram, servicios, ahora_s)
            resumen["avisos_enviados"] += videos.resumen_semanal(config, estado, bd, telegram, servicios, ahora)
        if con_videos:
            videos.medir(bd, servicios, ahora_s)

        if _toca(estado, "ultima_limpieza_s", config.limpieza_cada_minutos, ahora_s):
            resumen.update(bd.limpiar(config, ahora_s))
            if servicios is not None:
                videos.limpiar_videos(config, bd, servicios.preparador.carpeta_videos, ahora_s)
            estado["ultima_limpieza_s"] = ahora_s
        if config.compactar_base_datos_cada_horas > 0 and _toca(
                estado, "ultima_compactacion_s", config.compactar_base_datos_cada_horas * 60, ahora_s):
            bd.compactar()
            estado["ultima_compactacion_s"] = ahora_s
        estado["ultimo_resultado"] = "ok"
    except Exception as e:
        resumen["resultado"] = "error"
        resumen["error"] = f"{type(e).__name__}: {e}"
        if modo == "alertas" and estado.get("ultimo_resultado") != "error":
            try:
                telegram.enviar(f"El detector ha fallado (se reintentara en la siguiente ejecucion):\n{resumen['error']}")
            except Exception:
                pass
        estado["ultimo_resultado"] = "error"
    finally:
        bd.guardar()
        resumen.update(bd.totales())
        resumen["llamadas_twitch"] = twitch.llamadas
        resumen["ratelimit_restante"] = twitch.ratelimit.get("ratelimit-remaining", "")
        resumen["duracion_s"] = round(reloj() - inicio, 2)
        almacen.registrar_ejecucion(resumen)
        _resumen_si_toca(config, almacen, bd, telegram, ahora, ahora_s)
        almacen.guardar_estado()
    return resumen


def _resumen_si_toca(config, almacen, bd, telegram, ahora, ahora_s):
    horas = config.resumen_de_funcionamiento_cada_horas
    if horas == 0 or not _toca(almacen.estado, "ultimo_resumen_s", horas * 60, ahora_s):
        return
    desde_s = almacen.estado.get("ultimo_resumen_s") or ahora_s - horas * 3600
    filas = [f for f in almacen.leer_ejecuciones()
             if datetime.fromisoformat(f["inicio_utc"].replace("Z", "+00:00")).timestamp() >= desde_s]
    errores = sum(1 for f in filas if f["resultado"] != "ok")
    en_directo = "si" if filas and (filas[-1].get("streamers_en_directo") or "0") not in ("", "0") else "no"
    zona = config.zona_horaria
    lineas = [
        f"Detector funcionando. Desde {texto_local(desde_s, zona)}:",
        f"{len(filas)} ejecuciones ({errores} con error) | {bd.clips_nuevos_desde(desde_s)} clips nuevos | "
        f"en directo ahora: {en_directo}",
        f"Modo actual: {modo_actual(config.franjas, ahora.astimezone(ZoneInfo(zona)))}",
    ]
    if errores:
        ultimo_error = next(f["error"] for f in reversed(filas) if f["resultado"] != "ok")
        lineas.append(f"Ultimo error: {ultimo_error}")
    lineas.append(f"Base de datos: {bd.totales()['clips_en_base']} clips guardados")
    cuantos = config.clips_en_el_resumen
    vistos = bd.mas_vistos(desde_s, cuantos) if cuantos else []
    if vistos:
        lineas.append(f"\nTop {len(vistos)} clips por visualizaciones:")
        for i, c in enumerate(vistos, 1):
            lineas.append(f"{i}. {c['visualizaciones']} vis | viralidad {_texto_nota(c['nota_maxima'])} | "
                          f"{c['streamer']} | {c['titulo']} | {c['url']}")
    mejores = bd.mejores_momentos(desde_s, cuantos) if cuantos else []
    if mejores:
        lineas.append(f"\nTop {len(mejores)} momentos por viralidad:")
        for i, m in enumerate(mejores, 1):
            lineas.append(f"{i}. viralidad {_texto_nota(m['nota_maxima'])} | {m['vistas_representante']} vis "
                          f"({m['clips']} clips) | {m['streamer']} | {m['titulo']} | {m['url']}")
    try:
        telegram.enviar("\n".join(lineas))
        almacen.estado["ultimo_resumen_s"] = ahora_s
    except Exception:
        pass


def registrar_fallo_previo(almacen, mensaje, telegram=None, zona="Europe/Madrid", ahora=None):
    ahora = ahora or datetime.now(timezone.utc)
    almacen.registrar_ejecucion({
        "hora_espana": hora_local(ahora, zona), "inicio_utc": iso_utc(ahora), "duracion_s": 0,
        "resultado": "error", "repaso": "ninguno", "error": mensaje,
    })
    if telegram and almacen.estado.get("ultimo_resultado") != "error":
        try:
            telegram.enviar(f"El detector no ha podido arrancar (se reintentara en la siguiente ejecucion):\n{mensaje}")
        except Exception:
            pass
    almacen.estado["ultimo_resultado"] = "error"
    almacen.guardar_estado()


def _repasar(config, estado, bd, twitch, ids, ahora, ahora_s, resumen):
    if _toca(estado, "ultimo_repaso_completo_s", config.repaso_completo_cada_minutos, ahora_s):
        tipo, horas = "completo", config.horas_que_cubre_el_repaso_completo
    elif _toca(estado, "ultimo_repaso_reciente_s", config.repaso_reciente_cada_minutos, ahora_s):
        tipo, horas = "reciente", config.horas_que_cubre_el_repaso_reciente
    else:
        return []
    resumen["repaso"] = tipo
    observaciones, vistos = [], set()
    for streamer in config.streamers:
        if streamer not in ids:
            continue
        for desde, hasta in tramos(ahora, horas, config.minutos_por_trozo_de_consulta, config.minutos_de_solape_entre_trozos):
            clips, truncado = twitch.clips(ids[streamer], desde, hasta, config.max_paginas_de_100_clips_por_trozo,
                                           config.clips_por_pagina)
            resumen["tramos_consultados"] += 1
            resumen["tramos_truncados"] += int(truncado)
            for clip in clips:
                if clip["id"] in vistos:
                    continue
                vistos.add(clip["id"])
                obs = bd.observar_clip(streamer, clip, ahora_s, config)
                observaciones.append(obs)
                resumen["clips_nuevos"] += int(obs["nuevo"])
                resumen["clips_con_cambios"] += int(obs["cambio"])
    resumen["clips_recibidos"] = len(observaciones)
    estado["ultimo_repaso_reciente_s"] = ahora_s
    if tipo == "completo":
        estado["ultimo_repaso_completo_s"] = ahora_s
    return observaciones


def actualizar_momentos(config, bd, ahora_s):
    desde_ventana_s = ahora_s - config.horas_que_cubre_el_repaso_completo * 3600
    desde_referencias_s = ahora_s - config.viralidad_dias_de_clips_para_comparar * 86400
    clips = bd.clips_desde(desde_ventana_s)
    grupos = mom.agrupar(clips, config.momento_segundos_de_cercania_en_el_vod,
                         config.momento_minutos_de_cercania_al_crearse * 60)
    externas = bd.momentos_de_referencia(desde_referencias_s, desde_ventana_s,
                                         config.viralidad_visualizaciones_minimas_para_comparar)
    momentos = mom.calcular(grupos, bd.historiales_desde(desde_ventana_s), bd.directos_desde(desde_ventana_s - 3600),
                            ahora_s, config, externas)
    bd.guardar_momentos(momentos, desde_ventana_s, ahora_s)
    return momentos


def _resumen_al_despertar(config, bd, telegram, ahora_s, con_videos=False):
    horas = config.resumen_al_despertar_momentos_de_las_ultimas_horas
    if horas == 0:
        return 0
    pendientes = bd.momentos_pendientes(ahora_s - horas * 3600, config.avisar_si_la_nota_es_al_menos,
                                        config.avisar_si_tiene_al_menos_visualizaciones,
                                        config.max_momentos_en_el_resumen_al_despertar)
    if not pendientes:
        return 0
    zona = config.zona_horaria
    lineas = [f"Mientras estabas en silencio ({horas} h): {len(pendientes)} momentos con potencial"]
    for i, m in enumerate(pendientes, 1):
        lineas.append(f"\n{i}. Viralidad {_texto_nota(m['nota_maxima'])} | {m['vistas_representante']} vis "
                      f"({m['clips']} clips) | {m['streamer']} | inicio {texto_local(m['inicio_s'], zona)} "
                      f"(hace {(ahora_s - m['inicio_s']) // 3600} h)\n{m['titulo']}\n{m['url']}")
    telegram.enviar("\n".join(lineas))
    bd.marcar_momentos_avisados_por_id([m["momento_id"] for m in pendientes])
    if con_videos:
        videos.encolar_momentos(bd, pendientes, ahora_s)
    bd.guardar()
    return 1


def _avisar(config, bd, telegram, observaciones, momentos, ahora_s, base_vacia, desconocidos, con_videos=False):
    enviados = 0
    if desconocidos:
        telegram.enviar(f"Aviso: no encuentro en Twitch estos streamers: {', '.join(desconocidos)}. Revisa config.json")
        enviados += 1
    if base_vacia and observaciones:
        telegram.enviar(f"Base de datos iniciada: {len(observaciones)} clips registrados. "
                        f"A partir de ahora solo te avisare de clips con potencial.")
        enviados += 1
        return enviados
    edad_maxima_s = config.avisar_si_se_creo_hace_menos_de_minutos * 60
    candidatos = [m for m in momentos
                  if m["nota"] is not None and m["nota"] >= config.avisar_si_la_nota_es_al_menos
                  and m["vistas"] >= config.avisar_si_tiene_al_menos_visualizaciones
                  and ahora_s - m["inicio_s"] <= edad_maxima_s
                  and not m["avisado"]]
    candidatos.sort(key=lambda m: (m["nota"], m["puntuacion"]), reverse=True)
    for m in candidatos[:config.max_avisos_por_ejecucion]:
        telegram.enviar(_texto_aviso(m, bd.clip(m["representante"]), ahora_s, config.zona_horaria)
                        + ("\n(Preparando el video vertical...)" if con_videos else ""))
        bd.marcar_momento_avisado(m)
        if con_videos:
            videos.encolar_momentos(bd, [m], ahora_s)
        bd.guardar()
        enviados += 1
    restantes = len(candidatos) - config.max_avisos_por_ejecucion
    if restantes > 0:
        telegram.enviar(f"...y {restantes} clips con potencial mas en esta ejecucion.")
        enviados += 1
    return enviados
