def velocidad_por_hora(historial, ahora_s, creado_s, ventana_minutos, edad_minima_minutos):
    actual_s, actuales = historial[-1]
    limite = ahora_s - ventana_minutos * 60
    referencia = None
    for instante, vistas in historial[:-1]:
        if instante <= limite:
            referencia = (instante, vistas)
        else:
            if referencia is None:
                referencia = (instante, vistas)
            break
    if referencia is None:
        horas = max(actual_s - creado_s, edad_minima_minutos * 60) / 3600
        return actuales / horas
    horas = max(actual_s - referencia[0], edad_minima_minutos * 60) / 3600
    return max(0.0, (actuales - referencia[1]) / horas)
