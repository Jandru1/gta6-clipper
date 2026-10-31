import argparse
import fcntl
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import launchd
from app.almacen import Almacen
from app.basedatos import BaseDatos, texto_local
from app.config import ErrorConfiguracion, cargar_config, cargar_credenciales, guardar_credenciales_desde_entorno
from app.ejecucion import ejecutar_una_vez, hora_local, iso_utc, registrar_fallo_previo
from app.http import ErrorRed, Respuesta
from app.telegram import ClienteTelegram
from app.twitch import ClienteTwitch, ErrorLimiteTwitch

VERSION = "2026-10-31.2"
PROYECTO = Path(__file__).resolve().parent
RUTA_CONFIG = PROYECTO / "config.json"
RUTA_CREDENCIALES = PROYECTO / "credenciales.json"
CARPETA_DATOS = PROYECTO / "datos"


def _ok(texto):
    print(f"  [OK]    {texto}")


def _fallo(texto):
    print(f"  [FALLO] {texto}")


def _clientes(almacen, exigir_telegram=True):
    creds = cargar_credenciales(RUTA_CREDENCIALES, exigir_telegram=exigir_telegram)
    twitch = ClienteTwitch(creds.twitch_client_id, creds.twitch_client_secret, almacen.estado)
    telegram = ClienteTelegram(creds.telegram_bot_token, creds.telegram_chat_id) if creds.telegram_chat_id else None
    return twitch, telegram


def cmd_prueba(_args):
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    twitch, telegram = _clientes(almacen, exigir_telegram=False)
    streamer = config.streamers[0]
    exito = True

    print("T1 - Conexion con Twitch (token de app)")
    try:
        twitch.token(forzar_nuevo=True)
        _ok(f"Token obtenido. Valido durante {twitch.dias_validez_token():.0f} dias")
    except Exception as e:
        _fallo(e)
        return 1

    print(f"T2 - Buscar el streamer '{streamer}'")
    ids = twitch.ids_de_usuarios([streamer])
    if streamer in ids:
        _ok(f"ID de {streamer}: {ids[streamer]}")
    else:
        _fallo(f"Twitch no devuelve ningun usuario '{streamer}'. Revisa config.json")
        return 1

    print(f"T3 - Clips creados en los ultimos {config.minutos_por_trozo_de_consulta} min (un tramo)")
    ahora = datetime.now(timezone.utc)
    clips, truncado = twitch.clips(ids[streamer], iso_utc(ahora - timedelta(minutes=config.minutos_por_trozo_de_consulta)),
                                   iso_utc(ahora), config.max_paginas_de_100_clips_por_trozo, config.clips_por_pagina)
    _ok(f"{len(clips)} clips recibidos{' (TRUNCADO: hay mas de los que caben)' if truncado else ''}")
    for clip in sorted(clips, key=lambda c: c.get("view_count", 0), reverse=True)[:3]:
        print(f"          - {clip.get('view_count')} vis. | creado {hora_local(clip.get('created_at', ''), config.zona_horaria)}"
              f" | {clip.get('title')} | {clip.get('url')}")

    print("T4 - Comprobar si esta en directo")
    directo = twitch.directos([streamer]).get(streamer)
    if directo:
        _ok(f"En directo: {directo.get('viewer_count')} espectadores | {directo.get('game_name')} | {directo.get('title')}")
    else:
        _ok("No esta en directo ahora mismo (respuesta correcta, sin errores)")
    print(f"          Limite de la API: {twitch.ratelimit} | Llamadas hechas: {twitch.llamadas}")

    print("T5 - Aviso al movil por Telegram")
    if not telegram:
        _fallo("Falta telegram_chat_id en credenciales.json (usa: python3 clipper.py chat-id)")
        exito = False
    else:
        try:
            telegram.enviar(f"Prueba del detector GTA6: si lees esto, T5 funciona ({datetime.now():%d-%m %H:%M}).")
            _ok("Mensaje enviado. Comprueba que te ha llegado al movil")
        except Exception as e:
            _fallo(e)
            exito = False

    almacen.guardar_estado()
    print("\nResultado:", "TODO CORRECTO" if exito else "HAY FALLOS (ver arriba)")
    return 0 if exito else 1


def cmd_chat_id(_args):
    creds = cargar_credenciales(RUTA_CREDENCIALES, exigir_telegram=False)
    if not creds.telegram_bot_token:
        print("Falta telegram_bot_token en credenciales.json")
        return 1
    chats = ClienteTelegram(creds.telegram_bot_token, "").chats_recientes()
    if not chats:
        print("No hay mensajes. Escribe 'hola' a tu bot en Telegram y vuelve a ejecutar este comando.")
        return 1
    for chat_id, nombre in chats:
        print(f"chat_id: {chat_id}  ({nombre})")
    print("Copia tu chat_id en credenciales.json, campo telegram_chat_id")
    return 0


def cmd_credenciales_desde_entorno(_args):
    variables = guardar_credenciales_desde_entorno(RUTA_CREDENCIALES)
    print(f"credenciales.json creado (solo lectura para tu usuario) a partir de: {', '.join(variables)}")
    print("Comprueba que funciona con: python3 clipper.py prueba")
    return 0


def _telegram_si_es_posible():
    try:
        creds = cargar_credenciales(RUTA_CREDENCIALES)
        return ClienteTelegram(creds.telegram_bot_token, creds.telegram_chat_id)
    except ErrorConfiguracion:
        return None


def cmd_ejecutar(_args):
    almacen = Almacen(CARPETA_DATOS)
    try:
        config = cargar_config(RUTA_CONFIG)
        cargar_credenciales(RUTA_CREDENCIALES)
    except ErrorConfiguracion as e:
        registrar_fallo_previo(almacen, f"Configuracion: {e}", _telegram_si_es_posible())
        raise
    with almacen.ruta_bloqueo.open("w") as bloqueo:
        try:
            fcntl.flock(bloqueo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Hay otra ejecucion en curso; esta se omite.")
            return 0
        twitch, telegram = _clientes(almacen)
        bd = BaseDatos(almacen.ruta_base_datos)
        try:
            r = ejecutar_una_vez(config, almacen, bd, twitch, telegram)
        finally:
            bd.cerrar()
    print(f"{r['hora_espana']} {r['resultado']} modo={r['modo']} repaso={r['repaso']} recibidos={r['clips_recibidos']} "
          f"nuevos={r['clips_nuevos']} con_cambios={r['clips_con_cambios']} avisos={r['avisos_enviados']} "
          f"llamadas={r['llamadas_twitch']} truncados={r['tramos_truncados']} en_base={r['clips_en_base']} {r['error']}")
    return 0 if r["resultado"] == "ok" else 1


def cmd_exportar(_args):
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    if not almacen.ruta_base_datos.exists():
        print("Todavia no hay base de datos (datos/clips.db). Ejecuta antes el detector.")
        return 1
    bd = BaseDatos(almacen.ruta_base_datos)
    try:
        rutas = bd.exportar(CARPETA_DATOS, config.zona_horaria)
        totales = bd.totales()
    finally:
        bd.cerrar()
    print(f"Exportado ({totales['clips_en_base']} clips, {totales['filas_historial']} filas de historial):")
    for ruta in rutas:
        print(f"  {ruta}")
    return 0


def cmd_informe(args):
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    if not almacen.ruta_base_datos.exists():
        print("Todavia no hay base de datos (datos/clips.db): el detector aun no ha guardado nada.")
        return 1
    bd = BaseDatos(almacen.ruta_base_datos)
    try:
        zona = config.zona_horaria
        totales = bd.totales()
        print(f"Base de datos: {totales['clips_en_base']} clips | {totales['filas_historial']} filas de historial")
        for s in bd.resumen_por_streamer():
            print(f"  {s['streamer']}: {s['clips']} clips | maximo {s['max_vis']} vis | {s['con_nota']} con nota | "
                  f"ultimo clip nuevo guardado: {texto_local(s['ultimo_guardado_s'], zona)}")
        desde_s = int((datetime.now(timezone.utc) - timedelta(hours=args.horas)).timestamp())
        mejores = bd.mejores_clips(desde_s, args.top)
        print(f"\nTop {len(mejores)} por viralidad (clips consultados en las ultimas {args.horas} h):")
        for i, c in enumerate(mejores, 1):
            nota = "sin nota" if c["nota_maxima"] is None else f"{c['nota_maxima']}/10"
            velocidad = round(c["velocidad_maxima"] or 0)
            print(f"{i:>3}. {nota:>8} | {c['visualizaciones']:>7} vis | {velocidad:>7} vis/h max | "
                  f"creado {texto_local(c['creado_s'], zona)} | {c['streamer']} | {c['titulo']}\n"
                  f"      {c['url']}")
        if not mejores:
            print("  (ninguno: no se ha consultado ningun clip en ese periodo)")
    finally:
        bd.cerrar()
    return 0


def cmd_revisar(args):
    config = cargar_config(RUTA_CONFIG)
    filas = Almacen(CARPETA_DATOS).leer_ejecuciones()
    limite = datetime.now(timezone.utc) - timedelta(hours=args.horas)
    filas = [f for f in filas if datetime.fromisoformat(f["inicio_utc"].replace("Z", "+00:00")) >= limite]
    if not filas:
        print(f"No hay ejecuciones en las ultimas {args.horas} h.")
        return 1
    intervalo = config.ejecucion_automatica_cada_minutos
    tiempos = [datetime.fromisoformat(f["inicio_utc"].replace("Z", "+00:00")) for f in filas]
    maximo = timedelta(minutes=intervalo * 1.5)
    huecos = [(a, b) for a, b in zip(tiempos, tiempos[1:]) if b - a > maximo]
    errores = [f for f in filas if f["resultado"] != "ok"]
    zona = config.zona_horaria
    print(f"Ejecuciones en las ultimas {args.horas} h: {len(filas)} (esperadas aprox. {int(args.horas * 60 / intervalo)})")
    print(f"Primera: {hora_local(tiempos[0], zona)} | Ultima: {hora_local(tiempos[-1], zona)} (hora de Espana)")
    print(f"Huecos de mas de {int(maximo.total_seconds() // 60)} min: {len(huecos)}")
    for a, b in huecos:
        print(f"  - de {hora_local(a, zona)} a {hora_local(b, zona)} ({int((b - a).total_seconds() // 60)} min) "
              f"-> el portatil pudo dormirse o apagarse")
    print(f"Ejecuciones con error: {len(errores)}")
    for f in errores[-5:]:
        print(f"  - {hora_local(f['inicio_utc'], zona)}: {f['error']}")
    truncados = sum(int(f.get("tramos_truncados") or 0) for f in filas)
    if truncados:
        print(f"Tramos truncados: {truncados} -> en algun tramo habia mas clips de los que caben. "
              f"Sube 'max_paginas_de_100_clips_por_trozo' o baja 'minutos_por_trozo_de_consulta'.")
    return 0 if not huecos and not errores else 1


class _HttpSimulado:
    def __init__(self, caso, streamer):
        self.caso = caso
        self.streamer = streamer
        self.devuelto_401 = False

    def __call__(self, metodo, url, **kwargs):
        if self.caso == "sin_internet":
            raise ErrorRed("simulado")
        if url.endswith("/oauth2/token"):
            return Respuesta(200, {}, {"access_token": "simulado", "expires_in": 5000000})
        if self.caso == "token_caducado" and not self.devuelto_401:
            self.devuelto_401 = True
            return Respuesta(401, {}, {"message": "Invalid OAuth token"})
        if url.endswith("/users"):
            return Respuesta(200, {}, {"data": [{"login": self.streamer, "id": "1"}]})
        if url.endswith("/streams"):
            if self.caso == "limite":
                return Respuesta(429, {"ratelimit-reset": "0"}, {"message": "Too Many Requests"})
            return Respuesta(200, {}, {"data": []})
        return Respuesta(200, {}, {"data": [], "pagination": {}})


class _TelegramMudo:
    def __init__(self):
        self.mensajes = []

    def enviar(self, texto):
        self.mensajes.append(texto)


def cmd_prueba_fallos(_args):
    config = cargar_config(RUTA_CONFIG)
    config.streamers = config.streamers[:1]
    exito = True
    casos = [
        ("Sin internet", "sin_internet", "ErrorRed"),
        ("Limite de la API superado (429)", "limite", "ErrorLimiteTwitch"),
        ("Token caducado (401) con renovacion automatica", "token_caducado", None),
    ]
    print("T7 - Resistencia a fallos (simulados, sin usar tus credenciales ni tus datos reales)")
    for nombre, caso, error_esperado in casos:
        with tempfile.TemporaryDirectory() as carpeta:
            almacen = Almacen(Path(carpeta))
            twitch = ClienteTwitch("id", "secreto", almacen.estado, http=_HttpSimulado(caso, config.streamers[0]))
            bd = BaseDatos(almacen.ruta_base_datos)
            r = ejecutar_una_vez(config, almacen, bd, twitch, _TelegramMudo())
            bd.cerrar()
            registrado = len(almacen.leer_ejecuciones()) == 1
            if error_esperado is None:
                correcto = r["resultado"] == "ok" and registrado
                detalle = "renovo el token y termino bien" if correcto else f"resultado inesperado: {r['error']}"
            else:
                correcto = r["resultado"] == "error" and error_esperado in r["error"] and registrado
                detalle = (f"error registrado sin romperse ({r['error']})" if correcto
                           else f"resultado inesperado: {r['resultado']} {r['error']}")
            (_ok if correcto else _fallo)(f"{nombre}: {detalle}")
            exito = exito and correcto
    print("\nResultado:", "TODO CORRECTO" if exito else "HAY FALLOS (ver arriba)")
    return 0 if exito else 1


def cmd_instalar(_args):
    config = cargar_config(RUTA_CONFIG)
    cargar_credenciales(RUTA_CREDENCIALES)
    aviso = launchd.aviso_carpeta(PROYECTO)
    if aviso:
        print(aviso)
    destino = launchd.instalar(PROYECTO, config.ejecucion_automatica_cada_minutos)
    print(f"Instalado: {destino}")
    print(f"El detector se ejecutara cada {config.ejecucion_automatica_cada_minutos} min mientras el portatil este despierto.")
    print("Comprueba el estado con: python3 clipper.py estado")
    return 0


def cmd_desinstalar(_args):
    print("Desinstalado." if launchd.desinstalar() else "No estaba instalado.")
    return 0


def _ultimas_lineas_con_error(ruta, cuantas=3):
    if not ruta.exists():
        return []
    lineas = [l.strip() for l in ruta.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return [l for l in lineas if "error" in l.lower() or "falta" in l.lower()][-cuantas:]


def cmd_estado(_args):
    datos = launchd.estado()
    if datos is None:
        print("EN MARCHA: NO. La ejecucion automatica esta parada.")
        print("  Para activarla: python3 clipper.py instalar")
        return 0
    codigo = datos.get("last exit code", "")
    correcta = codigo.startswith("0")
    sin_ejecuciones = not codigo or codigo.startswith("(")
    print("EN MARCHA: SI" + ("." if correcta or sin_ejecuciones else ", PERO LA ULTIMA EJECUCION HA FALLADO (ver abajo)."))
    print(f"  Se ejecuta cada: {datos.get('run interval', '?').split()[0]} segundos")
    print(f"  Ejecuciones desde que se instalo: {datos.get('runs', '?')}")
    print("  Ahora mismo: " + ("ejecutandose" if datos.get("state") == "running"
                              else "en espera hasta la siguiente ejecucion (normal)"))
    if datos.get("script") and Path(datos["script"]).resolve() != (PROYECTO / "clipper.py").resolve():
        print(f"  ATENCION: launchd ejecuta {datos['script']}, que NO es esta carpeta. Repite 'instalar' desde aqui.")
    filas = Almacen(CARPETA_DATOS).leer_ejecuciones()
    if filas:
        f = filas[-1]
        print(f"  Ultima ejecucion registrada: {f['hora_espana']} | {f['resultado']} | repaso {f.get('repaso', '')} | "
              f"{f.get('clips_nuevos', '')} clips nuevos | {f.get('avisos_enviados', '')} avisos")
        if f["resultado"] != "ok" and f.get("error"):
            print(f"  Error: {f['error']}")
    if not correcta and not sin_ejecuciones:
        lineas = _ultimas_lineas_con_error(CARPETA_DATOS / "launchd_salida.log") + \
                 _ultimas_lineas_con_error(CARPETA_DATOS / "launchd_errores.log")
        if lineas:
            print("  Ultimos mensajes de error de la ejecucion automatica:")
            for linea in lineas[-3:]:
                print(f"    {linea}")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="clipper.py", description=f"Detector de clips GTA6 - version {VERSION}")
    parser.add_argument("--version", action="version", version=f"clipper.py {VERSION} en {PROYECTO}")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("prueba", help="T1-T5: Twitch, clips, directo y aviso a Telegram").set_defaults(func=cmd_prueba)
    sub.add_parser("chat-id", help="Muestra tu chat_id de Telegram").set_defaults(func=cmd_chat_id)
    sub.add_parser("credenciales-desde-entorno",
                   help="Crea credenciales.json con las variables de ~/.zshrc").set_defaults(func=cmd_credenciales_desde_entorno)
    sub.add_parser("ejecutar", help="Una ejecucion (la que lanza launchd)").set_defaults(func=cmd_ejecutar)
    sub.add_parser("exportar", help="Exporta fichas e historial a CSV (hora de Espana)").set_defaults(func=cmd_exportar)
    informe = sub.add_parser("informe", help="Muestra que hay guardado y los clips mas virales")
    informe.add_argument("--horas", type=int, default=24)
    informe.add_argument("--top", type=int, default=10)
    informe.set_defaults(func=cmd_informe)
    revisar = sub.add_parser("revisar-ejecuciones", help="T6: comprueba huecos y errores")
    revisar.add_argument("--horas", type=float, default=12)
    revisar.set_defaults(func=cmd_revisar)
    sub.add_parser("prueba-fallos", help="T7: simula fallos y comprueba que no se rompe").set_defaults(func=cmd_prueba_fallos)
    sub.add_parser("instalar", help="Programa la ejecucion periodica con launchd").set_defaults(func=cmd_instalar)
    sub.add_parser("desinstalar", help="Quita la ejecucion periodica").set_defaults(func=cmd_desinstalar)
    sub.add_parser("estado", help="Estado de la ejecucion periodica").set_defaults(func=cmd_estado)
    args = parser.parse_args()
    try:
        return args.func(args)
    except ErrorConfiguracion as e:
        print(f"Error de configuracion: {e}")
        return 2
    except ErrorRed as e:
        print(f"Error de red: {e}")
        return 3
    except ErrorLimiteTwitch as e:
        print(e)
        return 4


if __name__ == "__main__":
    sys.exit(main())
