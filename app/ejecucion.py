import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.basedatos import texto_local
from app.franjas import modo_actual
from app.viralidad import nota_1_a_10

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


def _texto_aviso(obs, ahora_s, zona):
    clip = obs["clip"]
    return (
        f"Clip con potencial de {obs['streamer']}\n"
        f"{clip.get('title', '(sin titulo)')}\n"
        f"Viralidad: {_texto_nota(obs.get('nota'))} | {obs['visualizaciones']} visualizaciones "
        f"| {round(obs['velocidad'] or 0)} vis/hora\n"
        f"Creado: {texto_local(obs['creado_s'], zona)} (hace {(ahora_s - obs['creado_s']) // 60} min)\n"
        f"{clip.get('url', '')}"
    )


def ejecutar_una_vez(config, almacen, bd, twitch, telegram, ahora=None, reloj=time.time):
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
        _poner_notas(config, bd, observaciones, ahora_s)
        bd.guardar()

        if modo == "alertas":
            resumen["avisos_enviados"] = _avisar(config, bd, telegram, observaciones, ahora_s, base_vacia, desconocidos)
        bd.guardar()

        if _toca(estado, "ultima_limpieza_s", config.limpieza_cada_minutos, ahora_s):
            resumen.update(bd.limpiar(config, ahora_s))
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
        almacen.guardar_estado()
    return resumen


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


def _poner_notas(config, bd, observaciones, ahora_s):
    referencias = {}
    desde_s = ahora_s - config.viralidad_dias_de_clips_para_comparar * 86400
    for obs in observaciones:
        if obs["velocidad"] is None:
            continue
        if obs["streamer"] not in referencias:
            referencias[obs["streamer"]] = bd.velocidades_de_referencia(obs["streamer"], desde_s)
        obs["nota"] = nota_1_a_10(obs["velocidad"], referencias[obs["streamer"]], config.viralidad_clips_minimos_para_dar_nota)
        bd.poner_nota(obs["clip_id"], obs["nota"])


def _avisar(config, bd, telegram, observaciones, ahora_s, base_vacia, desconocidos):
    enviados = 0
    if desconocidos:
        telegram.enviar(f"Aviso: no encuentro en Twitch estos streamers: {', '.join(desconocidos)}. Revisa config.json")
        enviados += 1
    if base_vacia and observaciones:
        telegram.enviar(f"Base de datos iniciada: {len(observaciones)} clips registrados. "
                        f"A partir de ahora solo te avisare de clips con potencial.")
        enviados += 1
    edad_maxima_s = config.avisar_si_se_creo_hace_menos_de_minutos * 60
    candidatos = [o for o in observaciones
                  if o["visualizaciones"] >= config.avisar_si_tiene_al_menos_visualizaciones
                  and ahora_s - o["creado_s"] <= edad_maxima_s
                  and not bd.esta_avisado(o["clip_id"])]
    candidatos.sort(key=lambda o: o["velocidad"] or 0, reverse=True)
    for obs in candidatos[:config.max_avisos_por_ejecucion]:
        telegram.enviar(_texto_aviso(obs, ahora_s, config.zona_horaria))
        bd.marcar_avisado([obs["clip_id"]])
        enviados += 1
    restantes = len(candidatos) - config.max_avisos_por_ejecucion
    if restantes > 0:
        telegram.enviar(f"...y {restantes} clips con potencial mas en esta ejecucion.")
        enviados += 1
    return enviados
