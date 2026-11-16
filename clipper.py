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

VERSION = "2026-11-16.2"
PROYECTO = Path(__file__).resolve().parent
RUTA_CONFIG = PROYECTO / "config.json"
RUTA_CREDENCIALES = PROYECTO / "credenciales.json"
RUTA_CLIENTE_GOOGLE = PROYECTO / "google_cliente.json"
RUTA_TOKENS_YOUTUBE = PROYECTO / "youtube_tokens.json"
RUTA_TOKENS_TWITCH = PROYECTO / "twitch_usuario.json"
CARPETA_DATOS = PROYECTO / "datos"
CARPETA_VIDEOS = CARPETA_DATOS / "videos"
CARPETA_MODELOS = CARPETA_DATOS / "modelos"


def _servicios(config):
    from app.herramientas import MODELO_WHISPER
    from app.preparacion import Preparador
    from app.videos import Servicios
    from app.youtube import ClienteYouTube, leer_cliente, leer_tokens
    youtube = {}
    tokens = leer_tokens(RUTA_TOKENS_YOUTUBE)
    if tokens and RUTA_CLIENTE_GOOGLE.exists():
        cliente = leer_cliente(RUTA_CLIENTE_GOOGLE)
        youtube = {cuenta: ClienteYouTube(cliente, datos["refresh_token"]) for cuenta, datos in tokens.items()}
    return Servicios(Preparador(config, CARPETA_VIDEOS, CARPETA_MODELOS / MODELO_WHISPER), youtube)


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
            r = ejecutar_una_vez(config, almacen, bd, twitch, telegram,
                                 servicios=_servicios(config) if config.preparar_videos else None)
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
        mejores = bd.mejores_momentos(desde_s, args.top)
        print(f"\nTop {len(mejores)} momentos por viralidad (actualizados en las ultimas {args.horas} h):")
        for i, m in enumerate(mejores, 1):
            nota = "sin nota" if m["nota_maxima"] is None else f"{m['nota_maxima']}/10"
            print(f"{i:>3}. {nota:>8} | {m['vistas_representante']:>7} vis ({m['clips']} clips, {m['vistas']} vis en total) | "
                  f"inicio {texto_local(m['inicio_s'], zona)} | {m['streamer']} | {m['titulo']}\n"
                  f"      {m['url']}")
        if not mejores:
            print("  (ninguno: todavia no hay momentos puntuados en ese periodo)")
    finally:
        bd.cerrar()
    return 0


def cmd_comprobar_herramientas(_args):
    from app.herramientas import comprobar
    config = cargar_config(RUTA_CONFIG)
    resultados = comprobar(config, CARPETA_MODELOS)
    for nombre, correcto, detalle in resultados:
        (_ok if correcto else _fallo)(f"{nombre}: {detalle}")
    print(f"  [{'OK' if RUTA_CLIENTE_GOOGLE.exists() else '--'}]    google_cliente.json "
          f"{'encontrado' if RUTA_CLIENTE_GOOGLE.exists() else 'no esta (necesario para YouTube, ver GUIA_YOUTUBE.txt)'}")
    from app.youtube import leer_tokens
    tokens = leer_tokens(RUTA_TOKENS_YOUTUBE)
    for cuenta in config.cuentas:
        autorizada = cuenta["id"] in tokens
        print(f"  [{'OK' if autorizada else '--'}]    YouTube cuenta '{cuenta['id']}': "
              f"{'autorizada (' + tokens[cuenta['id']].get('canal_titulo', '') + ')' if autorizada else 'sin autorizar'}")
    return 0 if all(r[1] for r in resultados[:4]) else 1


def cmd_preparar_herramientas(_args):
    from app.herramientas import asegurar_modelo
    print("Descargando el modelo de whisper desde la fuente oficial (547 MB) y comprobando su huella...")
    ruta = asegurar_modelo(CARPETA_MODELOS)
    _ok(f"Modelo listo: {ruta}")
    return 0


def cmd_preparar_clip(args):
    from app import videos
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    _, telegram = _clientes(almacen)
    with almacen.ruta_bloqueo.open("w") as bloqueo:
        try:
            fcntl.flock(bloqueo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Esperando a que termine la ejecucion automatica en curso...")
            fcntl.flock(bloqueo, fcntl.LOCK_EX)
        bd = BaseDatos(almacen.ruta_base_datos)
        try:
            clip = bd.clip(args.clip_id)
            if not clip:
                print(f"No encuentro el clip '{args.clip_id}' en la base de datos (usa el id del final de su enlace).")
                return 1
            bd.con.execute("DELETE FROM cola_preparacion WHERE clip_id = ?", (args.clip_id,))
            bd.encolar(args.clip_id, clip.get("momento_id"), clip.get("nota_maxima"),
                       int(datetime.now(timezone.utc).timestamp()))
            bd.guardar()
            servicios = _servicios(config)
            enviados = videos.procesar_cola(config, bd, telegram, servicios, int(datetime.now(timezone.utc).timestamp()),
                                            solo_clip=args.clip_id)
            fila = bd.con.execute("SELECT estado, detalle FROM cola_preparacion WHERE clip_id = ?",
                                  (args.clip_id,)).fetchone()
            print(f"Resultado: {fila['estado']} {fila['detalle'] or ''} ({enviados} mensajes enviados a Telegram)")
            return 0 if fila["estado"] == "preparado" else 1
        finally:
            bd.cerrar()


def cmd_autorizar_youtube(args):
    from app.youtube import ClienteYouTube, autorizar, guardar_tokens, leer_cliente, leer_tokens
    config = cargar_config(RUTA_CONFIG)
    if args.cuenta not in [c["id"] for c in config.cuentas]:
        print(f"La cuenta '{args.cuenta}' no esta en config.json ('cuentas'). Opciones: "
              f"{', '.join(c['id'] for c in config.cuentas)}")
        return 1
    cliente = leer_cliente(RUTA_CLIENTE_GOOGLE)
    print(f"Autorizando la cuenta '{args.cuenta}'. En el navegador ELIGE EL CANAL de esa cuenta.")
    refresh = autorizar(cliente)
    canal = ClienteYouTube(cliente, refresh).canal()
    tokens = leer_tokens(RUTA_TOKENS_YOUTUBE)
    tokens[args.cuenta] = {"refresh_token": refresh, "canal_id": canal["id"], "canal_titulo": canal["titulo"]}
    guardar_tokens(RUTA_TOKENS_YOUTUBE, tokens)
    _ok(f"Cuenta '{args.cuenta}' conectada al canal de YouTube '{canal['titulo']}'.")
    print("Si no es el canal correcto, repite el comando y elige otro canal.")
    return 0


def cmd_autorizar_twitch(_args):
    from app.chat import autorizar_dispositivo
    from app.youtube import guardar_tokens
    creds = cargar_credenciales(RUTA_CREDENCIALES, exigir_telegram=False)
    print("Vas a dar permiso al script para LEER el chat de Twitch y CREAR clips con tu cuenta de Twitch.")
    datos = autorizar_dispositivo(creds.twitch_client_id, creds.twitch_client_secret)
    guardar_tokens(RUTA_TOKENS_TWITCH, datos)
    _ok(f"Cuenta de Twitch '{datos['login']}' conectada (permisos: {', '.join(datos['permisos'])}).")
    print("Siguiente paso: python3 clipper.py instalar   (pone en marcha la escucha del chat)")
    return 0


def cmd_escuchar_chat(_args):
    from app.chat import TokenUsuario, escuchar
    config = cargar_config(RUTA_CONFIG)
    if not config.chat["activo"]:
        print("La escucha del chat esta desactivada en config.json (chat.activo = false).")
        return 0
    creds = cargar_credenciales(RUTA_CREDENCIALES, exigir_telegram=False)
    token = TokenUsuario(RUTA_TOKENS_TWITCH, creds.twitch_client_id, creds.twitch_client_secret)
    bd = BaseDatos(Almacen(CARPETA_DATOS).ruta_base_datos)
    try:
        escuchar(config.chat, bd, token,
                 registro=lambda t: print(f"{hora_local(datetime.now(timezone.utc), config.zona_horaria)} {t}", flush=True))
    finally:
        bd.cerrar()
    return 0


def cmd_informe_chat(args):
    import csv
    from app.chat import cruzar_con_clips
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    bd = BaseDatos(almacen.ruta_base_datos)
    zona = config.zona_horaria
    desde_s = int((datetime.now(timezone.utc) - timedelta(hours=args.horas)).timestamp())
    try:
        filas = []
        for streamer in config.chat["streamers"]:
            minutos = bd.minutos_de_chat_escuchados(streamer, desde_s)
            picos = [p for p in bd.picos_chat_desde(desde_s) if p["streamer"] == streamer]
            cruzados = cruzar_con_clips(picos, bd.clips_de_streamer_desde(streamer, desde_s - 3600))
            con_clips = sum(1 for p in cruzados if p["clips_humanos"])
            print(f"{streamer}: chat escuchado ~{minutos // 60} h {minutos % 60} min en las ultimas {args.horas} h | "
                  f"{len(picos)} picos | {con_clips} con algun clip de espectadores")
            for p in sorted(cruzados, key=lambda p: -p["factor_maximo"])[:args.top]:
                print(f"  {texto_local(p['inicio_s'], zona)} | x{p['factor_maximo']} | {p['usuarios_maximo']} personas | "
                      f"{p['fin_s'] - p['inicio_s']} s | clips: {p['clips_humanos']} de {p['personas_que_clipean']} "
                      f"personas (mejor: {p['vis_mejor_clip']} vis) | \"{p['texto_top']}\"")
                if p["url_mejor_clip"]:
                    print(f"      {p['url_mejor_clip']}")
            filas += cruzados
    finally:
        bd.cerrar()
    ruta = CARPETA_DATOS / "picos_chat.csv"
    with ruta.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(["streamer", "inicio_espana", "segundos", "veces_lo_normal", "personas", "mensajes",
                           "reacciones", "texto_mas_repetido", "clips_de_espectadores", "personas_que_clipean",
                           "vis_mejor_clip", "enlace_mejor_clip", "bueno_si_no"])
        for p in filas:
            escritor.writerow([p["streamer"], texto_local(p["inicio_s"], zona), p["fin_s"] - p["inicio_s"],
                               p["factor_maximo"], p["usuarios_maximo"], p["mensajes"], p["reacciones"], p["texto_top"],
                               p["clips_humanos"], p["personas_que_clipean"], p["vis_mejor_clip"], p["url_mejor_clip"], ""])
    print(f"\nLista completa para revisar: {ruta} (rellena la ultima columna con si/no)")
    return 0


def cmd_prueba_algoritmo(_args):
    from app.momentos import prueba_hacia_atras
    config = cargar_config(RUTA_CONFIG)
    almacen = Almacen(CARPETA_DATOS)
    if not almacen.ruta_base_datos.exists():
        print("Todavia no hay base de datos (datos/clips.db).")
        return 1
    bd = BaseDatos(almacen.ruta_base_datos)
    try:
        informe = prueba_hacia_atras(bd.clips_desde(0), bd.historiales_desde(0), bd.directos_desde(0),
                                     int(datetime.now(timezone.utc).timestamp()), config)
    finally:
        bd.cerrar()
    minutos = config.viralidad_minutos_de_vida_tempranos
    print(f"Prueba hacia atras: nota calculada con lo que se sabia a los {minutos} min de cada momento, comparada "
          f"con el 10% de momentos con mas visualizaciones al final (solo momentos de hace mas de 24 h).")
    if not informe:
        print("  No hay momentos evaluables todavia (hacen falta momentos de hace mas de 24 h con historial temprano).")
        return 1
    for streamer, r in informe.items():
        precision = f"{100 * r['aciertos'] / r['avisados']:.0f}%" if r["avisados"] else "-"
        cobertura = f"{100 * r['aciertos'] / r['top_10_por_ciento']:.0f}%" if r["top_10_por_ciento"] else "-"
        print(f"  {streamer}: {r['momentos_evaluados']} momentos evaluados ({r['con_nota']} con nota) | "
              f"avisados (nota >= {config.avisar_si_la_nota_es_al_menos}): {r['avisados']} | "
              f"de ellos en el top 10%: {r['aciertos']} ({precision}) | top 10% detectado: {cobertura}")
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
    if config.chat["activo"] and RUTA_TOKENS_TWITCH.exists():
        print(f"Instalado: {launchd.instalar_chat(PROYECTO)}")
        print(f"La escucha del chat de {', '.join(config.chat['streamers'])} queda en marcha de forma continua.")
    elif config.chat["activo"]:
        print("La escucha del chat NO se ha instalado: falta autorizar tu cuenta de Twitch "
              "(python3 clipper.py autorizar-twitch) y repetir 'instalar'.")
    else:
        launchd.desinstalar(launchd.ETIQUETA_CHAT)
    print("Comprueba el estado con: python3 clipper.py estado")
    return 0


def cmd_desinstalar(_args):
    print("Desinstalado." if launchd.desinstalar() else "No estaba instalado.")
    if launchd.desinstalar(launchd.ETIQUETA_CHAT):
        print("Escucha del chat desinstalada.")
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
    chat = launchd.estado(launchd.ETIQUETA_CHAT)
    if chat is None:
        print("ESCUCHA DEL CHAT: NO instalada (python3 clipper.py autorizar-twitch y luego instalar).")
    else:
        print("ESCUCHA DEL CHAT: " + ("SI, en marcha." if chat.get("state") == "running" else
                                      f"instalada pero parada (estado: {chat.get('state', '?')})."))
        registro = CARPETA_DATOS / "chat_salida.log"
        if registro.exists():
            ultimas = [l for l in registro.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()][-2:]
            for linea in ultimas:
                print(f"  {linea}")
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
    sub.add_parser("prueba-algoritmo", help="Mide con tus datos si la nota de viralidad acierta").set_defaults(
        func=cmd_prueba_algoritmo)
    sub.add_parser("comprobar-herramientas", help="Comprueba yt-dlp, ffmpeg-full, whisper y YouTube").set_defaults(
        func=cmd_comprobar_herramientas)
    sub.add_parser("preparar-herramientas", help="Descarga el modelo de whisper (una vez)").set_defaults(
        func=cmd_preparar_herramientas)
    preparar = sub.add_parser("preparar-clip", help="Prepara a mano el video de un clip guardado y lo envia a Telegram")
    preparar.add_argument("clip_id")
    preparar.set_defaults(func=cmd_preparar_clip)
    autorizar = sub.add_parser("autorizar-youtube", help="Conecta una cuenta de config.json con su canal de YouTube")
    autorizar.add_argument("--cuenta", required=True)
    autorizar.set_defaults(func=cmd_autorizar_youtube)
    sub.add_parser("autorizar-twitch", help="Conecta tu cuenta de Twitch (leer el chat y crear clips)").set_defaults(
        func=cmd_autorizar_twitch)
    sub.add_parser("escuchar-chat", help="Escucha el chat y apunta los picos (lo lanza launchd)").set_defaults(
        func=cmd_escuchar_chat)
    informe_chat = sub.add_parser("informe-chat", help="Picos de chat y si otros espectadores los clipearon")
    informe_chat.add_argument("--horas", type=int, default=24)
    informe_chat.add_argument("--top", type=int, default=15)
    informe_chat.set_defaults(func=cmd_informe_chat)
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
    except Exception as e:
        if type(e).__name__ in ("ErrorYouTube", "ErrorHerramienta", "ErrorTelegram"):
            print(f"Error: {e}")
            return 5
        raise


if __name__ == "__main__":
    sys.exit(main())
