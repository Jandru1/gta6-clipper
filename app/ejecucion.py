import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.franjas import modo_actual

DIAS_MEMORIA_CLIPS = 7


def iso_utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hora_local(momento, zona: str) -> str:
    if isinstance(momento, str):
        if not momento:
            return ""
        momento = datetime.fromisoformat(momento.replace("Z", "+00:00"))
    return momento.astimezone(ZoneInfo(zona)).strftime("%d/%m/%Y %H:%M:%S")


def _fila_clip(ahora, zona, streamer, clip):
    return {
        "hora_consulta_espana": hora_local(ahora, zona),
        "consulta_utc": iso_utc(ahora),
        "clip_id": clip.get("id", ""),
        "streamer": streamer,
        "creado_espana": hora_local(clip.get("created_at", ""), zona),
        "creado_utc": clip.get("created_at", ""),
        "visualizaciones": clip.get("view_count", 0),
        "duracion_s": clip.get("duration", ""),
        "vod_id": clip.get("video_id", ""),
        "vod_offset_s": "" if clip.get("vod_offset") is None else clip.get("vod_offset"),
        "titulo": clip.get("title", ""),
        "url": clip.get("url", ""),
    }


def _texto_aviso(streamer, clip, zona):
    return (
        f"Nuevo clip de {streamer}\n"
        f"{clip.get('title', '(sin titulo)')}\n"
        f"Visualizaciones: {clip.get('view_count', 0)} | Duracion: {clip.get('duration', '?')} s\n"
        f"Creado: {hora_local(clip.get('created_at', ''), zona) or '?'} (hora de Espana)\n"
        f"{clip.get('url', '')}"
    )


def ejecutar_una_vez(config, almacen, twitch, telegram, ahora=None, reloj=time.time):
    inicio = reloj()
    ahora = ahora or datetime.now(timezone.utc)
    modo = modo_actual(config.franjas, ahora.astimezone(ZoneInfo(config.zona_horaria)))
    resumen = {
        "hora_espana": hora_local(ahora, config.zona_horaria), "inicio_utc": iso_utc(ahora), "resultado": "ok", "modo": modo, "llamadas_twitch": 0,
        "ratelimit_restante": "", "streamers_en_directo": 0, "clips_vistos": 0, "clips_nuevos": 0,
        "avisos_enviados": 0, "error": "",
    }
    estado = almacen.estado
    try:
        ids = estado.setdefault("ids_streamers", {})
        pendientes = [s for s in config.streamers if s not in ids]
        if pendientes:
            ids.update(twitch.ids_de_usuarios(pendientes))
        desconocidos = [s for s in config.streamers if s not in ids]

        directos = twitch.directos(config.streamers)
        resumen["streamers_en_directo"] = len(directos)

        vistos = estado.setdefault("clips_vistos", {})
        primera_vez = not vistos
        desde = iso_utc(ahora - timedelta(hours=config.ventana_clips_horas))
        hasta = iso_utc(ahora)
        nuevos, filas, ids_nuevos = [], [], set()
        for streamer in config.streamers:
            if streamer not in ids:
                continue
            for clip in twitch.clips(ids[streamer], desde, hasta, config.max_paginas_clips):
                filas.append(_fila_clip(ahora, config.zona_horaria, streamer, clip))
                if clip["id"] not in vistos and clip["id"] not in ids_nuevos:
                    ids_nuevos.add(clip["id"])
                    nuevos.append((streamer, clip))
        almacen.registrar_clips(filas)
        resumen["clips_vistos"] = len(filas)
        resumen["clips_nuevos"] = len(nuevos)

        if modo == "alertas":
            resumen["avisos_enviados"] = _avisar(config, telegram, nuevos, primera_vez, desconocidos)
        for _, clip in nuevos:
            vistos[clip["id"]] = clip.get("created_at", resumen["inicio_utc"])
        _olvidar_clips_antiguos(vistos, ahora)
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
        resumen["llamadas_twitch"] = twitch.llamadas
        resumen["ratelimit_restante"] = twitch.ratelimit.get("ratelimit-remaining", "")
        resumen["duracion_s"] = round(reloj() - inicio, 2)
        almacen.registrar_ejecucion(resumen)
        almacen.guardar_estado()
    return resumen


def _avisar(config, telegram, nuevos, primera_vez, desconocidos):
    enviados = 0
    if desconocidos:
        telegram.enviar(f"Aviso: no encuentro en Twitch estos streamers: {', '.join(desconocidos)}. Revisa config.json")
        enviados += 1
    if not nuevos:
        return enviados
    if primera_vez:
        telegram.enviar(f"Primera ejecucion: {len(nuevos)} clips registrados. A partir de ahora solo te avisare de los nuevos.")
        return enviados + 1
    nuevos.sort(key=lambda par: par[1].get("view_count", 0), reverse=True)
    for streamer, clip in nuevos[:config.max_avisos_por_ejecucion]:
        telegram.enviar(_texto_aviso(streamer, clip, config.zona_horaria))
        enviados += 1
    restantes = len(nuevos) - config.max_avisos_por_ejecucion
    if restantes > 0:
        telegram.enviar(f"...y {restantes} clips nuevos mas en esta ejecucion.")
        enviados += 1
    return enviados


def _olvidar_clips_antiguos(vistos, ahora):
    limite = iso_utc(ahora - timedelta(days=DIAS_MEMORIA_CLIPS))
    for clip_id in [c for c, creado in vistos.items() if creado < limite]:
        del vistos[clip_id]
