import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from app.http import peticion

URL_AUTORIZACION = "https://accounts.google.com/o/oauth2/v2/auth"
URL_TOKEN = "https://oauth2.googleapis.com/token"
URL_API = "https://www.googleapis.com/youtube/v3"
URL_SUBIDA = "https://www.googleapis.com/upload/youtube/v3/videos"
URL_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
PERMISOS = "https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/yt-analytics.readonly"
PAISES_TIER_1 = {"US", "GB", "CA", "AU", "NZ"}
CATEGORIA_VIDEOJUEGOS = "20"


class ErrorYouTube(Exception):
    pass


def leer_cliente(ruta: Path) -> dict:
    if not ruta.exists():
        raise ErrorYouTube(f"Falta {ruta.name}: descargalo de Google Cloud (ver GUIA_YOUTUBE.txt)")
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    cliente = datos.get("installed") or {}
    if not cliente.get("client_id") or not cliente.get("client_secret"):
        raise ErrorYouTube(f"{ruta.name} no es un cliente OAuth de tipo 'App de escritorio'")
    return cliente


def leer_tokens(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def guardar_tokens(ruta: Path, tokens: dict):
    temporal = ruta.with_suffix(".tmp")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(temporal, ruta)
    os.chmod(ruta, 0o600)


def autorizar(cliente: dict, abrir_navegador=webbrowser.open, peticion_http=peticion, espera_s=300) -> str:
    verificador = secrets.token_urlsafe(64)
    reto = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()
    estado = secrets.token_urlsafe(16)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    redireccion = f"http://127.0.0.1:{puerto}"
    recibido = {}

    class Manejador(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            recibido.update(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Autorizacion recibida. Ya puedes cerrar esta pestana.".encode("utf-8"))

        def log_message(self, *args):
            pass

    servidor = http.server.HTTPServer(("127.0.0.1", puerto), Manejador)
    hilo = threading.Thread(target=servidor.handle_request, daemon=True)
    hilo.start()
    url = URL_AUTORIZACION + "?" + urllib.parse.urlencode({
        "client_id": cliente["client_id"], "redirect_uri": redireccion, "response_type": "code", "scope": PERMISOS,
        "access_type": "offline", "prompt": "consent select_account", "state": estado,
        "code_challenge": reto, "code_challenge_method": "S256"})
    print(f"Se abrira el navegador. Si no se abre, copia esta direccion en Safari:\n{url}\n")
    abrir_navegador(url)
    hilo.join(espera_s)
    servidor.server_close()
    if recibido.get("state") != estado or "code" not in recibido:
        raise ErrorYouTube(f"No se completo la autorizacion: {recibido.get('error', 'sin respuesta')}")
    r = peticion_http("POST", URL_TOKEN, formulario={
        "code": recibido["code"], "client_id": cliente["client_id"], "client_secret": cliente["client_secret"],
        "redirect_uri": redireccion, "grant_type": "authorization_code", "code_verifier": verificador})
    if r.estado != 200 or "refresh_token" not in (r.cuerpo or {}):
        raise ErrorYouTube(f"Google no devolvio el permiso (HTTP {r.estado}): {r.cuerpo}")
    return r.cuerpo["refresh_token"]


class ClienteYouTube:
    def __init__(self, cliente: dict, refresh_token: str, http=peticion, reloj=time.time):
        self.cliente = cliente
        self.refresh_token = refresh_token
        self.http = http
        self.reloj = reloj
        self._token = None
        self._caduca = 0

    def _acceso(self) -> str:
        if self._token and self._caduca - self.reloj() > 60:
            return self._token
        r = self.http("POST", URL_TOKEN, formulario={
            "client_id": self.cliente["client_id"], "client_secret": self.cliente["client_secret"],
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.estado != 200 or "access_token" not in (r.cuerpo or {}):
            raise ErrorYouTube(f"No se pudo renovar el permiso de YouTube (HTTP {r.estado}): {r.cuerpo}. "
                               f"Repite 'python3 clipper.py autorizar-youtube'")
        self._token, self._caduca = r.cuerpo["access_token"], self.reloj() + int(r.cuerpo.get("expires_in", 3600))
        return self._token

    def _cabeceras(self, extra=None):
        return {"Authorization": f"Bearer {self._acceso()}", **(extra or {})}

    def _comprobar(self, r, que):
        if r.estado not in (200, 201, 204):
            raise ErrorYouTube(f"YouTube rechazo {que} (HTTP {r.estado}): {r.cuerpo}")
        return r.cuerpo

    def canal(self) -> dict:
        datos = self._comprobar(self.http("GET", f"{URL_API}/channels", cabeceras=self._cabeceras(),
                                          params={"part": "snippet", "mine": "true"}), "consultar el canal")
        items = datos.get("items") or []
        if not items:
            raise ErrorYouTube("Esta cuenta de Google no tiene canal de YouTube")
        return {"id": items[0]["id"], "titulo": items[0]["snippet"]["title"]}

    def subir(self, ruta: Path, titulo, descripcion, promocion_pagada: bool) -> tuple:
        cuerpo = {"snippet": {"title": titulo, "description": descripcion, "categoryId": CATEGORIA_VIDEOJUEGOS},
                  "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        partes = "snippet,status"
        if promocion_pagada:
            cuerpo["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
            partes += ",paidProductPlacementDetails"
        contenido = ruta.read_bytes()
        cabeceras = self._cabeceras({"X-Upload-Content-Length": str(len(contenido)), "X-Upload-Content-Type": "video/mp4"})
        r = self.http("POST", URL_SUBIDA, cabeceras=cabeceras,
                      params={"uploadType": "resumable", "part": partes}, json_cuerpo=cuerpo)
        aviso = ""
        if r.estado == 400 and promocion_pagada:
            del cuerpo["paidProductPlacementDetails"]
            r = self.http("POST", URL_SUBIDA, cabeceras=cabeceras,
                          params={"uploadType": "resumable", "part": "snippet,status"}, json_cuerpo=cuerpo)
            aviso = "YouTube no acepto la casilla de promocion pagada por API: marcala a mano en YouTube Studio"
        self._comprobar(r, "iniciar la subida")
        destino = r.cabeceras.get("location")
        if not destino:
            raise ErrorYouTube("YouTube no devolvio la direccion de subida")
        r = self.http("PUT", destino, cabeceras=self._cabeceras(), datos=contenido, tipo_datos="video/mp4", timeout=600)
        video = self._comprobar(r, "la subida del video")
        return video["id"], aviso

    def hacer_publico(self, video_id):
        self._comprobar(self.http("PUT", f"{URL_API}/videos", cabeceras=self._cabeceras(), params={"part": "status"},
                                  json_cuerpo={"id": video_id, "status": {"privacyStatus": "public",
                                                                          "selfDeclaredMadeForKids": False}}),
                        "hacer publico el video")

    def borrar(self, video_id):
        self._comprobar(self.http("DELETE", f"{URL_API}/videos", cabeceras=self._cabeceras(), params={"id": video_id}),
                        "borrar el video")

    def visualizaciones(self, video_ids) -> dict:
        resultado = {}
        for i in range(0, len(video_ids), 50):
            datos = self._comprobar(self.http("GET", f"{URL_API}/videos", cabeceras=self._cabeceras(),
                                              params={"part": "statistics", "id": ",".join(video_ids[i:i + 50])}),
                                    "consultar estadisticas")
            for item in datos.get("items") or []:
                resultado[item["id"]] = int(item.get("statistics", {}).get("viewCount", 0))
        return resultado

    def paises_ultimos_dias(self, dias=7, hoy=None) -> dict:
        hoy = hoy or date.today()
        datos = self._comprobar(self.http("GET", URL_ANALYTICS, cabeceras=self._cabeceras(), params={
            "ids": "channel==MINE", "startDate": (hoy - timedelta(days=dias)).isoformat(), "endDate": hoy.isoformat(),
            "metrics": "views", "dimensions": "country", "sort": "-views"}), "consultar los paises")
        filas = datos.get("rows") or []
        total = sum(int(f[1]) for f in filas)
        tier1 = sum(int(f[1]) for f in filas if f[0] in PAISES_TIER_1)
        return {"visualizaciones": total, "porcentaje_tier1": round(100 * tier1 / total) if total else None}
