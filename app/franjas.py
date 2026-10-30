from datetime import datetime


def _minutos(hora: str) -> int:
    h, m = hora.split(":")
    return int(h) * 60 + int(m)


def modo_actual(franjas: list, ahora_local: datetime, modo_por_defecto: str = "silencio") -> str:
    minuto = ahora_local.hour * 60 + ahora_local.minute
    for franja in franjas:
        desde, hasta = _minutos(franja.desde), _minutos(franja.hasta)
        if desde <= hasta:
            dentro = desde <= minuto < hasta
        else:
            dentro = minuto >= desde or minuto < hasta
        if dentro:
            return franja.modo
    return modo_por_defecto
