import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import launchd
from app.almacen import Almacen
from app.config import ErrorConfiguracion, cargar_config, cargar_credenciales, guardar_credenciales_desde_entorno
from app.ejecucion import ejecutar_una_vez, hora_local, iso_utc
from app.http import ErrorRed, Respuesta
from app.telegram import ClienteTelegram
from app.twitch import ClienteTwitch, ErrorLimiteTwitch

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

    print(f"T3 - Clips de las ultimas {config.ventana_clips_horas} h")
    ahora = datetime.now(timezone.utc)
    clips = twitch.clips(ids[streamer], iso_utc(ahora - timedelta(hours=config.ventana_clips_horas)), iso_utc(ahora), 1)
    _ok(f"{len(clips)} clips recibidos (primera pagina)")
    for clip in sorted(clips, key=lambda c: c.get("view_count", 0), reverse=True)[:3]:
        print(f"          - {clip.get('view_count')} vis. | {clip.get('duration')} s | {clip.get('title')} | "
              f"vod_offset={clip.get('vod_offset')} | {clip.get('url')}")
    if clips:
        print(f"          Campos recibidos: {', '.join(sorted(clips[0].keys()))}")

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


def cmd_ejecutar(_args):
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    twitch, telegram = _clientes(almacen)
    r = ejecutar_una_vez(config, almacen, twitch, telegram)
    print(f"{r['hora_espana']} {r['resultado']} modo={r['modo']} clips={r['clips_vistos']} nuevos={r['clips_nuevos']} "
          f"avisos={r['avisos_enviados']} llamadas={r['llamadas_twitch']} {r['error']}")
    return 0 if r["resultado"] == "ok" else 1


def cmd_revisar(args):
    config = cargar_config(RUTA_CONFIG)
    filas = Almacen(CARPETA_DATOS).leer_ejecuciones()
    limite = datetime.now(timezone.utc) - timedelta(hours=args.horas)
    filas = [f for f in filas if datetime.fromisoformat(f["inicio_utc"].replace("Z", "+00:00")) >= limite]
    if not filas:
        print(f"No hay ejecuciones en las ultimas {args.horas} h.")
        return 1
    tiempos = [datetime.fromisoformat(f["inicio_utc"].replace("Z", "+00:00")) for f in filas]
    maximo = timedelta(minutes=config.intervalo_minutos * 1.5)
    huecos = [(a, b) for a, b in zip(tiempos, tiempos[1:]) if b - a > maximo]
    errores = [f for f in filas if f["resultado"] != "ok"]
    esperadas = int(args.horas * 60 / config.intervalo_minutos)
    zona = config.zona_horaria
    print(f"Ejecuciones en las ultimas {args.horas} h: {len(filas)} (esperadas aprox. {esperadas})")
    print(f"Primera: {hora_local(tiempos[0], zona)} | Ultima: {hora_local(tiempos[-1], zona)} (hora de Espana)")
    print(f"Huecos de mas de {maximo.seconds // 60} min: {len(huecos)}")
    for a, b in huecos:
        print(f"  - de {hora_local(a, zona)} a {hora_local(b, zona)} ({(b - a).seconds // 60} min) "
              f"-> el portatil pudo dormirse o apagarse")
    print(f"Ejecuciones con error: {len(errores)}")
    for f in errores[-5:]:
        print(f"  - {hora_local(f['inicio_utc'], zona)}: {f['error']}")
    return 0 if not huecos and not errores else 1


class _HttpSimulado:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)

    def __call__(self, metodo, url, **kwargs):
        siguiente = self.respuestas.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


class _TelegramMudo:
    def __init__(self):
        self.mensajes = []

    def enviar(self, texto):
        self.mensajes.append(texto)


def cmd_prueba_fallos(_args):
    config = cargar_config(RUTA_CONFIG)
    config.streamers = config.streamers[:1]
    exito = True
    token_ok = Respuesta(200, {}, {"access_token": "simulado", "expires_in": 5000000})

    casos = [
        ("Sin internet", [ErrorRed("simulado")], "ErrorRed"),
        ("Limite de la API superado (429)",
         [token_ok, Respuesta(200, {}, {"data": [{"login": config.streamers[0], "id": "1"}]}),
          Respuesta(429, {"ratelimit-reset": "0"}, {"message": "Too Many Requests"})], "ErrorLimiteTwitch"),
        ("Token caducado (401) con renovacion automatica",
         [token_ok, Respuesta(401, {}, {"message": "Invalid OAuth token"}), token_ok,
          Respuesta(200, {}, {"data": [{"login": config.streamers[0], "id": "1"}]}),
          Respuesta(200, {}, {"data": []}), Respuesta(200, {}, {"data": [], "pagination": {}})], None),
    ]
    print("T7 - Resistencia a fallos (simulados, sin usar tus credenciales ni tus datos reales)")
    for nombre, respuestas, error_esperado in casos:
        with tempfile.TemporaryDirectory() as carpeta:
            almacen = Almacen(Path(carpeta))
            twitch = ClienteTwitch("id", "secreto", almacen.estado, http=_HttpSimulado(respuestas))
            telegram = _TelegramMudo()
            r = ejecutar_una_vez(config, almacen, twitch, telegram)
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
    destino = launchd.instalar(PROYECTO, config.intervalo_minutos)
    print(f"Instalado: {destino}")
    print(f"El detector se ejecutara cada {config.intervalo_minutos} min mientras el portatil este despierto.")
    print("Comprueba el estado con: python3 clipper.py estado")
    return 0


def cmd_desinstalar(_args):
    print("Desinstalado." if launchd.desinstalar() else "No estaba instalado.")
    return 0


def cmd_estado(_args):
    print(launchd.estado())
    return 0


def main():
    parser = argparse.ArgumentParser(prog="clipper.py", description="Detector de clips GTA6 (prueba tecnica)")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("prueba", help="T1-T5: Twitch, clips, directo y aviso a Telegram").set_defaults(func=cmd_prueba)
    sub.add_parser("chat-id", help="Muestra tu chat_id de Telegram").set_defaults(func=cmd_chat_id)
    sub.add_parser("credenciales-desde-entorno",
                   help="Crea credenciales.json con las variables de ~/.zshrc").set_defaults(func=cmd_credenciales_desde_entorno)
    sub.add_parser("ejecutar", help="Una ejecucion (la que lanza launchd)").set_defaults(func=cmd_ejecutar)
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
