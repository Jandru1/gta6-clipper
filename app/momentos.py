from bisect import bisect_left, bisect_right
from collections import defaultdict

from app.viralidad import velocidad_por_hora

SENALES = ("consenso", "velocidad_temprana", "velocidad_actual", "tamano_relativo")


def agrupar(clips, segundos_vod, segundos_creacion):
    grupos = []
    por_clave = defaultdict(list)
    for c in clips:
        con_offset = c.get("vod_id") and c.get("vod_offset_s") is not None
        por_clave[(c["streamer"], c["vod_id"] if con_offset else None)].append(c)
    for (_, vod), lista in por_clave.items():
        posicion = (lambda c: c["vod_offset_s"]) if vod else (lambda c: c["creado_s"])
        ventana = segundos_vod if vod else segundos_creacion
        lista.sort(key=lambda c: (posicion(c), c["clip_id"]))
        actual = None
        for c in lista:
            if actual and posicion(c) - posicion(actual[0]) <= ventana:
                actual.append(c)
            else:
                actual = [c]
                grupos.append(actual)
    return grupos


def _historial_hasta(historial, instante_s):
    return [h for h in historial if h[0] <= instante_s]


def vistas_en(clip, historial, instante_s, ahora_s):
    if instante_s >= ahora_s:
        return clip["visualizaciones"]
    previos = _historial_hasta(historial, instante_s)
    return previos[-1][1] if previos else None


def espectadores_cerca(directos, instante_s, margen_s=1800):
    candidatos = [(abs(t - instante_s), e) for t, e in directos if abs(t - instante_s) <= margen_s]
    return min(candidatos)[1] if candidatos else None


def representante(grupo):
    return max(grupo, key=lambda c: (c["visualizaciones"], c.get("duracion_s") or 0, c["clip_id"]))


def senales_del_momento(grupo, historiales, directos, ahora_s, config):
    inicio_s = min(c["creado_s"] for c in grupo)
    fin_temprano_s = inicio_s + config.viralidad_minutos_de_vida_tempranos * 60
    instante_temprano_s = min(ahora_s, fin_temprano_s)
    tempranos = [c for c in grupo if c["creado_s"] <= instante_temprano_s]
    vistas_tempranas = 0
    for c in tempranos:
        v = vistas_en(c, historiales.get(c["clip_id"], []), instante_temprano_s, ahora_s)
        if v is None:
            if c["primera_vez_s"] > instante_temprano_s + config.ejecucion_automatica_cada_minutos * 60:
                vistas_tempranas = None
                break
            v = 0
        vistas_tempranas += v
    minutos = max(instante_temprano_s - inicio_s, config.viralidad_minutos_minimos_de_calculo * 60) / 60
    velocidad_actual = 0.0
    for c in grupo:
        historial = list(historiales.get(c["clip_id"], []))
        if not historial or historial[-1][0] < ahora_s:
            historial.append((ahora_s, c["visualizaciones"]))
        velocidad_actual += velocidad_por_hora(historial, ahora_s, c["creado_s"],
                                               config.viralidad_minutos_para_medir_crecimiento,
                                               config.viralidad_minutos_minimos_de_calculo)
    vistas = sum(c["visualizaciones"] for c in grupo)
    espectadores = espectadores_cerca(directos.get(grupo[0]["streamer"], []), inicio_s)
    rep = representante(grupo)
    return {
        "momento_id": min(grupo, key=lambda c: (c["creado_s"], c["clip_id"]))["clip_id"],
        "streamer": grupo[0]["streamer"],
        "inicio_s": inicio_s,
        "clips": len(grupo),
        "clip_ids": [c["clip_id"] for c in grupo],
        "representante": rep["clip_id"],
        "vistas": vistas,
        "vistas_mejor_clip": rep["visualizaciones"],
        "consenso": len({c.get("creador_id") or c.get("creador") or c["clip_id"] for c in tempranos}),
        "velocidad_temprana": None if vistas_tempranas is None else vistas_tempranas / minutos,
        "velocidad_actual": velocidad_actual,
        "tamano_relativo": None if not espectadores else vistas / (espectadores / 1000),
    }


def percentil(valor, referencias_ordenadas):
    if not referencias_ordenadas:
        return None
    return 100 * bisect_left(referencias_ordenadas, valor) / len(referencias_ordenadas)


def pesos(config):
    return {"consenso": config.peso_consenso, "velocidad_temprana": config.peso_velocidad_temprana,
            "velocidad_actual": config.peso_velocidad_actual, "tamano_relativo": config.peso_tamano_relativo_al_directo}


def puntuar(momento, referencias, pesos_senales):
    total = peso_usado = 0
    desglose = {}
    for senal in SENALES:
        valor, peso = momento.get(senal), pesos_senales[senal]
        p = None if valor is None else percentil(valor, referencias.get(senal, []))
        desglose[senal] = None if p is None else round(p)
        if p is not None and peso:
            total += peso * p
            peso_usado += peso
    return (total / peso_usado if peso_usado else 0.0), desglose


def nota_por_cortes(puntuacion, puntuaciones_referencia, cortes_porcentaje, minimo_referencias):
    if len(puntuaciones_referencia) < minimo_referencias:
        return None
    por_encima = 100 * (len(puntuaciones_referencia) - bisect_right(puntuaciones_referencia, puntuacion)) \
        / len(puntuaciones_referencia)
    acumulado = 0
    for i, corte in enumerate(cortes_porcentaje):
        acumulado += corte
        if por_encima <= acumulado:
            return 10 - i
    return 1


def referencias_de(momentos_relevantes):
    return {s: sorted(m[s] for m in momentos_relevantes if m.get(s) is not None) for s in SENALES}


def prueba_hacia_atras(clips, historiales, directos, ahora_s, config):
    grupos = agrupar(clips, config.momento_segundos_de_cercania_en_el_vod, config.momento_minutos_de_cercania_al_crearse * 60)
    tempranos_s = config.viralidad_minutos_de_vida_tempranos * 60
    evaluados = defaultdict(list)
    for grupo in grupos:
        inicio_s = min(c["creado_s"] for c in grupo)
        instante_s = inicio_s + tempranos_s
        if instante_s > ahora_s - 86400:
            continue
        pasado, historial_pasado, completo = [], {}, True
        for c in grupo:
            if c["creado_s"] > instante_s:
                continue
            historial = _historial_hasta(historiales.get(c["clip_id"], []), instante_s)
            if not historial and c["primera_vez_s"] > instante_s + config.ejecucion_automatica_cada_minutos * 60:
                completo = False
                break
            pasado.append({**c, "visualizaciones": historial[-1][1] if historial else 0})
            historial_pasado[c["clip_id"]] = historial
        if not completo or not pasado:
            continue
        m = senales_del_momento(pasado, historial_pasado, directos, instante_s, config)
        m["instante_s"] = instante_s
        m["final_mejor_clip"] = max(c["visualizaciones"] for c in grupo)
        evaluados[m["streamer"]].append(m)
    pesos_senales = pesos(config)
    informe = {}
    for streamer, lista in evaluados.items():
        lista.sort(key=lambda m: m["instante_s"])
        for m in lista:
            refs = [r for r in lista if r is not m and r["instante_s"] <= m["instante_s"]
                    and r["inicio_s"] >= m["instante_s"] - config.viralidad_dias_de_clips_para_comparar * 86400
                    and r["final_mejor_clip"] >= config.viralidad_visualizaciones_minimas_para_comparar]
            tablas = referencias_de(refs)
            m["puntuacion"], _ = puntuar(m, tablas, pesos_senales)
            puntuaciones = sorted(puntuar(r, tablas, pesos_senales)[0] for r in refs)
            m["nota"] = nota_por_cortes(m["puntuacion"], puntuaciones, config.cortes_de_nota_en_porcentaje,
                                        config.viralidad_clips_minimos_para_dar_nota)
        con_nota = [m for m in lista if m["nota"] is not None]
        cuantos_top = max(1, len(con_nota) // 10)
        top = {id(m) for m in sorted(con_nota, key=lambda m: -m["final_mejor_clip"])[:cuantos_top]}
        avisados = [m for m in con_nota if m["nota"] >= config.avisar_si_la_nota_es_al_menos]
        informe[streamer] = {
            "momentos_evaluados": len(lista), "con_nota": len(con_nota), "top_10_por_ciento": len(top),
            "avisados": len(avisados), "aciertos": sum(id(m) in top for m in avisados),
        }
    return informe


def calcular(grupos, historiales, directos, ahora_s, config, referencias_externas):
    momentos = [senales_del_momento(g, historiales, directos, ahora_s, config) for g in grupos]
    minimo = config.viralidad_visualizaciones_minimas_para_comparar
    desde_s = ahora_s - config.viralidad_dias_de_clips_para_comparar * 86400
    por_streamer = defaultdict(list)
    for m in momentos:
        por_streamer[m["streamer"]].append(m)
    pesos_senales = pesos(config)
    for streamer, lista in por_streamer.items():
        externas = [r for r in referencias_externas.get(streamer, []) if r["inicio_s"] >= desde_s]
        relevantes = externas + [m for m in lista if m["vistas_mejor_clip"] >= minimo and m["inicio_s"] >= desde_s]
        refs = referencias_de(relevantes)
        for m in lista + externas:
            m["puntuacion"], m["desglose"] = puntuar(m, refs, pesos_senales)
        puntuaciones = sorted(max(r["puntuacion"], r.get("puntuacion_maxima") or 0) for r in relevantes)
        for m in lista:
            m["nota"] = nota_por_cortes(m["puntuacion"], puntuaciones, config.cortes_de_nota_en_porcentaje,
                                        config.viralidad_clips_minimos_para_dar_nota)
    return momentos
