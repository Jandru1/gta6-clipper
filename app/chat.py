import re
import socket
import sqlite3
import ssl
import statistics
import time
from collections import Counter, deque
from pathlib import Path

from app.http import peticion
from app.youtube import guardar_tokens, leer_tokens

URL_DISPOSITIVO = "https://id.twitch.tv/oauth2/device"
URL_TOKEN = "https://id.twitch.tv/oauth2/token"
URL_VALIDAR = "https://id.twitch.tv/oauth2/validate"
PERMISOS = "chat:read clips:edit"
SERVIDOR = ("irc.chat.twitch.tv", 6697)
MARGEN_RENOVACION_S = 300
SEGUNDOS_SIN_DATOS_PARA_RECONECTAR = 360
REACCIONES = {"LUL", "LULW", "KEKW", "KEKL", "OMEGALUL", "ICANT", "LMAO", "LMFAO", "LOL", "LOLL", "LOLLL", "XD",
              "POG", "POGGERS", "POGU", "HUH", "WTF", "NAHH", "NAH", "BRO", "W", "L", "OMG", "WHAT", "INSANE"}
EMOJIS_REACCION = ("😭", "💀", "😂", "🤣", "😱", "🔥")
PATRON_PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #(\w+) :(.*)$")


class ErrorChat(Exception):
    pass


class ErrorAutenticacionChat(ErrorChat):
    pass


def autorizar_dispositivo(client_id, client_secret, mostrar=print, http=peticion, esperar=time.sleep, reloj=time.time):
    r = http("POST", URL_DISPOSITIVO, formulario={"client_id": client_id, "scopes": PERMISOS})
    if r.estado != 200 or not isinstance(r.cuerpo, dict) or "device_code" not in r.cuerpo:
        raise ErrorChat(f"Twitch no ha dado el codigo de autorizacion (HTTP {r.estado}): {r.cuerpo}")
    intervalo = int(r.cuerpo.get("interval", 5))
    limite = reloj() + int(r.cuerpo.get("expires_in", 1800))
    mostrar(f"1. Abre en el navegador: {r.cuerpo['verification_uri']}\n"
            f"2. Entra con TU cuenta de Twitch y confirma el codigo: {r.cuerpo['user_code']}\n"
            f"3. Pulsa 'Autorizar'. Este comando esperara hasta que lo hagas.")
    while reloj() < limite:
        esperar(intervalo)
        t = http("POST", URL_TOKEN, formulario={"client_id": client_id, "client_secret": client_secret,
                                                "device_code": r.cuerpo["device_code"], "scopes": PERMISOS,
                                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        mensaje = str(t.cuerpo.get("message", "")) if isinstance(t.cuerpo, dict) else str(t.cuerpo)
        if t.estado == 200 and isinstance(t.cuerpo, dict) and "access_token" in t.cuerpo:
            return _con_login(t.cuerpo, http, reloj)
        if "authorization_pending" in mensaje:
            continue
        if "slow_down" in mensaje:
            intervalo += 5
            continue
        raise ErrorChat(f"Twitch ha rechazado la autorizacion (HTTP {t.estado}): {mensaje}")
    raise ErrorChat("El codigo ha caducado antes de autorizarlo. Repite el comando.")


def _con_login(cuerpo, http, reloj):
    v = http("GET", URL_VALIDAR, cabeceras={"Authorization": f"OAuth {cuerpo['access_token']}"})
    if v.estado != 200 or not isinstance(v.cuerpo, dict) or "login" not in v.cuerpo:
        raise ErrorChat(f"Twitch no ha validado el token (HTTP {v.estado}): {v.cuerpo}")
    return {"access_token": cuerpo["access_token"], "refresh_token": cuerpo["refresh_token"],
            "expira_en": reloj() + int(cuerpo.get("expires_in", 0)), "login": v.cuerpo["login"],
            "permisos": v.cuerpo.get("scopes", [])}


class TokenUsuario:
    def __init__(self, ruta: Path, client_id, client_secret, http=peticion, reloj=time.time):
        self.ruta = ruta
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http
        self.reloj = reloj
        self.datos = leer_tokens(ruta)
        if not self.datos.get("refresh_token"):
            raise ErrorAutenticacionChat(f"Falta {ruta.name}: ejecuta antes 'python3 clipper.py autorizar-twitch'")

    @property
    def login(self):
        return self.datos["login"]

    def acceso(self, forzar=False) -> str:
        if forzar or self.datos.get("expira_en", 0) - self.reloj() < MARGEN_RENOVACION_S:
            r = self.http("POST", URL_TOKEN, formulario={"client_id": self.client_id, "client_secret": self.client_secret,
                                                         "grant_type": "refresh_token",
                                                         "refresh_token": self.datos["refresh_token"]})
            if r.estado != 200 or not isinstance(r.cuerpo, dict) or "access_token" not in r.cuerpo:
                raise ErrorAutenticacionChat(f"No se pudo renovar el permiso de Twitch (HTTP {r.estado}): {r.cuerpo}. "
                                             f"Repite 'python3 clipper.py autorizar-twitch'")
            self.datos.update(access_token=r.cuerpo["access_token"],
                              refresh_token=r.cuerpo.get("refresh_token", self.datos["refresh_token"]),
                              expira_en=self.reloj() + int(r.cuerpo.get("expires_in", 0)))
            guardar_tokens(self.ruta, self.datos)
        return self.datos["access_token"]


URL_API = "https://api.twitch.tv/helix"


class ClienteClips:
    def __init__(self, token: TokenUsuario, client_id, http=peticion):
        self.token = token
        self.client_id = client_id
        self.http = http
        self.ids = {}

    def _cabeceras(self):
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token.acceso()}"}

    def id_de(self, login):
        if login not in self.ids:
            r = self.http("GET", f"{URL_API}/users", cabeceras=self._cabeceras(), params={"login": login})
            datos = (r.cuerpo or {}).get("data") if isinstance(r.cuerpo, dict) else None
            if r.estado != 200 or not datos:
                raise ErrorChat(f"No encuentro el canal de Twitch '{login}' (HTTP {r.estado})")
            self.ids[login] = datos[0]["id"]
        return self.ids[login]

    def crear(self, login, segundos) -> str:
        r = self.http("POST", f"{URL_API}/clips", cabeceras=self._cabeceras(),
                      params={"broadcaster_id": self.id_de(login), "duration": segundos})
        datos = (r.cuerpo or {}).get("data") if isinstance(r.cuerpo, dict) else None
        if r.estado not in (200, 202) or not datos:
            raise ErrorChat(f"Twitch no ha creado el clip de {login} (HTTP {r.estado}): {r.cuerpo}")
        return datos[0]["id"]


class CreadorDeClips:
    def __init__(self, ajustes, clips: ClienteClips, bd, registro=print):
        self.ajustes = ajustes
        self.clips = clips
        self.bd = bd
        self.registro = registro
        self.atendidos = set()

    def merece_clip(self, pico) -> bool:
        return (pico["factor_maximo"] >= self.ajustes["factor_para_crear_clip"]
                and pico["usuarios_maximo"] >= self.ajustes["usuarios_para_crear_clip"])

    def revisar(self, picos, ahora):
        for p in picos:
            clave = (p["streamer"], p["inicio_s"])
            if clave in self.atendidos or not self.merece_clip(p):
                continue
            self.atendidos.add(clave)
            recientes = self.bd.clips_de_chat_desde(p["streamer"], ahora - 3600)
            if len(recientes) >= self.ajustes["max_clips_por_hora"]:
                self.registro(f"Pico fuerte en {p['streamer']} sin clip: ya hay {len(recientes)} en la ultima hora")
                continue
            if recientes and ahora - max(c["creado_s"] for c in recientes) < self.ajustes["minutos_entre_clips"] * 60:
                self.registro(f"Pico fuerte en {p['streamer']} sin clip: el anterior es muy reciente")
                continue
            try:
                clip_id = self.clips.crear(p["streamer"], self.ajustes["segundos_de_clip"])
            except Exception as e:
                self.registro(f"No he podido crear el clip de {p['streamer']}: {e}")
                if "403" in str(e):
                    self.bd.guardar_clip_de_chat({**p, "clip_id": f"sin:{p['streamer']}:{p['inicio_s']}",
                                                  "creado_s": int(ahora)})
                    self.bd.guardar()
                continue
            self.bd.guardar_clip_de_chat({**p, "clip_id": clip_id, "creado_s": int(ahora)})
            self.bd.guardar()
            self.registro(f"Clip creado en {p['streamer']}: {clip_id} (x{p['factor_maximo']}, "
                          f"{p['usuarios_maximo']} personas)")


def leer_privmsg(linea: str):
    m = PATRON_PRIVMSG.match(linea)
    return (m.group(2).lower(), m.group(1).lower(), m.group(3)) if m else None


def es_reaccion(texto: str) -> bool:
    if any(e in texto for e in EMOJIS_REACCION):
        return True
    palabras = re.findall(r"[A-Za-z]+", texto)
    return bool(palabras) and any(p.upper() in REACCIONES for p in palabras[:3])


def _normalizar(texto: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1\1", " ".join(texto.lower().split()))[:60]


class _Canal:
    def __init__(self, ventanas_de_base):
        self.inicio = None
        self.mensajes = 0
        self.usuarios = set()
        self.reacciones = 0
        self.textos = Counter()
        self.historial = deque(maxlen=ventanas_de_base)
        self.pico = None


class DetectorPicos:
    def __init__(self, ajustes: dict):
        self.ventana_s = ajustes["segundos_por_ventana"]
        self.ventanas_de_base = max(1, ajustes["minutos_de_base"] * 60 // self.ventana_s)
        self.factor = ajustes["factor_de_pico"]
        self.usuarios_minimos = ajustes["usuarios_minimos_en_la_ventana"]
        self.union_s = ajustes["segundos_para_unir_picos"]
        self.canales = {}

    def _canal(self, nombre):
        if nombre not in self.canales:
            self.canales[nombre] = _Canal(self.ventanas_de_base)
        return self.canales[nombre]

    def mensaje(self, canal, usuario, texto, instante) -> tuple:
        c = self._canal(canal)
        ventanas, picos = self.avanzar(canal, instante)
        if c.inicio is None:
            c.inicio = int(instante // self.ventana_s * self.ventana_s)
        c.mensajes += 1
        c.usuarios.add(usuario)
        c.reacciones += es_reaccion(texto)
        c.textos[_normalizar(texto)] += 1
        return ventanas, picos

    def avanzar(self, canal, instante) -> tuple:
        c = self._canal(canal)
        ventanas, picos = [], []
        while c.inicio is not None and instante >= c.inicio + self.ventana_s:
            ventana, pico = self._cerrar(canal, c)
            if ventana:
                ventanas.append(ventana)
            if pico:
                picos.append(pico)
            c.inicio += self.ventana_s
            if c.mensajes == 0 and instante >= c.inicio + self.ventana_s:
                c.inicio = int(instante // self.ventana_s * self.ventana_s)
        return ventanas, picos

    def _cerrar(self, nombre, c):
        if c.mensajes == 0:
            terminado = None
            if c.pico and c.inicio - c.pico["fin_s"] > self.union_s:
                terminado, c.pico = c.pico, None
            return None, terminado
        texto_top, veces = c.textos.most_common(1)[0] if c.textos else ("", 0)
        ventana = {"streamer": nombre, "instante_s": c.inicio, "mensajes": c.mensajes, "usuarios": len(c.usuarios),
                   "reacciones": c.reacciones, "repeticion": round(veces / c.mensajes, 3) if c.mensajes else 0.0}
        base = statistics.median(c.historial) if len(c.historial) >= max(1, self.ventanas_de_base // 3) else None
        factor = c.mensajes / max(base, 1.0) if base is not None else 0.0
        es_pico = base is not None and factor >= self.factor and len(c.usuarios) >= self.usuarios_minimos
        terminado = None
        if es_pico:
            if c.pico and c.inicio - c.pico["fin_s"] <= self.union_s:
                p = c.pico
                p["fin_s"] = c.inicio + self.ventana_s
                if factor > p["factor_maximo"]:
                    p.update(factor_maximo=round(factor, 2), texto_top=texto_top)
                p["mensajes_maximo"] = max(p["mensajes_maximo"], c.mensajes)
                p["usuarios_maximo"] = max(p["usuarios_maximo"], len(c.usuarios))
                p["mensajes"] += c.mensajes
                p["reacciones"] += c.reacciones
            else:
                terminado = c.pico
                c.pico = {"streamer": nombre, "inicio_s": c.inicio, "fin_s": c.inicio + self.ventana_s,
                          "factor_maximo": round(factor, 2), "mensajes_maximo": c.mensajes,
                          "usuarios_maximo": len(c.usuarios), "mensajes": c.mensajes, "reacciones": c.reacciones,
                          "texto_top": texto_top, "base": round(base, 2)}
        elif c.pico and c.inicio - c.pico["fin_s"] > self.union_s:
            terminado, c.pico = c.pico, None
        if not es_pico:
            c.historial.append(c.mensajes)
        c.mensajes, c.usuarios, c.reacciones, c.textos = 0, set(), 0, Counter()
        return ventana, terminado

    def picos_abiertos(self) -> list:
        return [c.pico for c in self.canales.values() if c.pico]


class ConexionChat:
    def __init__(self, token: TokenUsuario, canales, crear_socket=None, reloj=time.time):
        self.token = token
        self.canales = [c.lower() for c in canales]
        self.crear_socket = crear_socket or self._socket_tls
        self.reloj = reloj
        self.sock = None
        self.resto = b""

    @staticmethod
    def _socket_tls():
        crudo = socket.create_connection(SERVIDOR, timeout=30)
        return ssl.create_default_context().wrap_socket(crudo, server_hostname=SERVIDOR[0])

    def _enviar(self, linea):
        self.sock.sendall((linea + "\r\n").encode("utf-8"))

    def conectar(self):
        self.sock = self.crear_socket()
        self.sock.settimeout(5)
        self.resto = b""
        self._enviar(f"PASS oauth:{self.token.acceso()}")
        self._enviar(f"NICK {self.token.login}")
        self._enviar("JOIN " + ",".join(f"#{c}" for c in self.canales))

    def cerrar(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def leer(self):
        ultimo_dato = self.reloj()
        while True:
            try:
                bloque = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                if self.reloj() - ultimo_dato > SEGUNDOS_SIN_DATOS_PARA_RECONECTAR:
                    raise ErrorChat("sin datos del chat en 6 minutos")
                yield None
                continue
            if not bloque:
                raise ErrorChat("Twitch ha cerrado la conexion del chat")
            ultimo_dato = self.reloj()
            lineas = (self.resto + bloque).split(b"\r\n")
            self.resto = lineas.pop()
            for crudo in lineas:
                linea = crudo.decode("utf-8", errors="replace")
                if linea.startswith("PING"):
                    self._enviar("PONG" + linea[4:])
                elif "Login authentication failed" in linea or "Improperly formatted auth" in linea:
                    raise ErrorAutenticacionChat("Twitch ha rechazado el permiso del chat")
                elif " RECONNECT" in linea:
                    raise ErrorChat("Twitch ha pedido reconectar")
                else:
                    leido = leer_privmsg(linea)
                    if leido:
                        yield leido


def escuchar(ajustes, bd, token, registro=print, conexion=None, reloj=time.time, dormir=time.sleep, vueltas=None,
             creador=None):
    detector = DetectorPicos(ajustes)
    conexion = conexion or ConexionChat(token, ajustes["streamers"], reloj=reloj)
    espera = 5
    ultima_limpieza = 0
    while vueltas is None or vueltas > 0:
        if vueltas is not None:
            vueltas -= 1
        try:
            conexion.conectar()
            registro(f"Conectado al chat de: {', '.join(conexion.canales)}")
            espera = 5
            for leido in conexion.leer():
                ahora = reloj()
                resultados = [detector.mensaje(*leido, ahora)] if leido else \
                    [detector.avanzar(canal, ahora) for canal in list(detector.canales)]
                for ventanas, picos in resultados:
                    for v in ventanas:
                        bd.guardar_ventana_chat(v)
                    for p in picos:
                        bd.guardar_pico_chat(p)
                        registro(f"Pico en {p['streamer']}: x{p['factor_maximo']} con {p['usuarios_maximo']} personas "
                                 f"| \"{p['texto_top']}\"")
                    if ventanas or picos:
                        bd.guardar()
                    if creador and ventanas:
                        creador.revisar(detector.picos_abiertos() + picos, ahora)
                if ahora - ultima_limpieza > 3600:
                    bd.limpiar_chat(ahora - ajustes["dias_de_registro"] * 86400)
                    bd.guardar()
                    ultima_limpieza = ahora
        except ErrorAutenticacionChat as e:
            registro(f"Error de permiso: {e}. Reintento en 10 min.")
            conexion.cerrar()
            dormir(600)
        except (ErrorChat, OSError, sqlite3.OperationalError) as e:
            registro(f"Chat desconectado ({e}). Reconecto en {espera} s.")
            conexion.cerrar()
            dormir(espera)
            espera = min(espera * 2, 300)
        finally:
            for p in detector.picos_abiertos():
                bd.guardar_pico_chat(p)
            bd.guardar()


def cruzar_con_clips(picos, clips, segundos_antes=60, segundos_despues=300) -> list:
    resultado = []
    for p in picos:
        cercanos = [c for c in clips if c["streamer"] == p["streamer"]
                    and p["inicio_s"] - segundos_antes <= c["creado_s"] <= p["fin_s"] + segundos_despues]
        mejor = max(cercanos, key=lambda c: c["visualizaciones"], default=None)
        resultado.append({**p, "clips_humanos": len(cercanos),
                          "personas_que_clipean": len({c.get("creador") for c in cercanos}),
                          "vis_mejor_clip": mejor["visualizaciones"] if mejor else 0,
                          "url_mejor_clip": mejor["url"] if mejor else ""})
    return resultado
