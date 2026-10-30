import time

from app.http import peticion

URL_TOKEN = "https://id.twitch.tv/oauth2/token"
URL_API = "https://api.twitch.tv/helix"
MARGEN_RENOVACION_S = 24 * 3600


class ErrorTwitch(Exception):
    pass


class ErrorLimiteTwitch(ErrorTwitch):
    def __init__(self, segundos_espera):
        super().__init__(f"Limite de la API de Twitch superado; recarga en {segundos_espera:.0f} s")
        self.segundos_espera = segundos_espera


class ClienteTwitch:
    def __init__(self, client_id, client_secret, estado: dict, http=peticion, reloj=time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.estado = estado
        self.http = http
        self.reloj = reloj
        self.llamadas = 0
        self.ratelimit = {}

    def token(self, forzar_nuevo=False) -> str:
        guardado = self.estado.get("twitch_token") or {}
        if not forzar_nuevo and guardado.get("access_token") and guardado.get("expira_en", 0) - self.reloj() > MARGEN_RENOVACION_S:
            return guardado["access_token"]
        r = self.http("POST", URL_TOKEN, params={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        })
        if r.estado != 200 or not isinstance(r.cuerpo, dict) or "access_token" not in r.cuerpo:
            raise ErrorTwitch(f"No se pudo obtener el token de Twitch (HTTP {r.estado}): {r.cuerpo}")
        self.estado["twitch_token"] = {
            "access_token": r.cuerpo["access_token"],
            "expira_en": self.reloj() + int(r.cuerpo.get("expires_in", 0)),
        }
        return r.cuerpo["access_token"]

    def dias_validez_token(self) -> float:
        guardado = self.estado.get("twitch_token") or {}
        return max(0.0, (guardado.get("expira_en", 0) - self.reloj()) / 86400)

    def _get(self, ruta, params, reintentar=True):
        cabeceras = {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"}
        r = self.http("GET", f"{URL_API}/{ruta}", cabeceras=cabeceras, params=params)
        self.llamadas += 1
        self._leer_ratelimit(r.cabeceras)
        if r.estado == 401 and reintentar:
            self.token(forzar_nuevo=True)
            return self._get(ruta, params, reintentar=False)
        if r.estado == 429:
            reset = float(r.cabeceras.get("ratelimit-reset", self.reloj() + 60))
            raise ErrorLimiteTwitch(max(0.0, reset - self.reloj()))
        if r.estado != 200 or not isinstance(r.cuerpo, dict):
            raise ErrorTwitch(f"Error de Twitch en {ruta} (HTTP {r.estado}): {r.cuerpo}")
        return r.cuerpo

    def _leer_ratelimit(self, cabeceras):
        for clave in ("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset"):
            if clave in cabeceras:
                self.ratelimit[clave] = cabeceras[clave]

    def ids_de_usuarios(self, logins: list) -> dict:
        resultado = {}
        for i in range(0, len(logins), 100):
            cuerpo = self._get("users", [("login", l) for l in logins[i:i + 100]])
            resultado.update({u["login"]: u["id"] for u in cuerpo.get("data", [])})
        return resultado

    def directos(self, logins: list) -> dict:
        resultado = {}
        for i in range(0, len(logins), 100):
            cuerpo = self._get("streams", [("user_login", l) for l in logins[i:i + 100]] + [("first", 100)])
            resultado.update({s["user_login"]: s for s in cuerpo.get("data", []) if s.get("type") == "live"})
        return resultado

    def clips(self, broadcaster_id: str, desde_iso: str, hasta_iso: str, max_paginas: int, por_pagina: int = 100):
        params = {"broadcaster_id": broadcaster_id, "started_at": desde_iso, "ended_at": hasta_iso, "first": por_pagina}
        resultado = []
        for _ in range(max_paginas):
            cuerpo = self._get("clips", params)
            resultado.extend(cuerpo.get("data", []))
            cursor = (cuerpo.get("pagination") or {}).get("cursor")
            if not cursor:
                return resultado, False
            params = {**params, "after": cursor}
        return resultado, True
