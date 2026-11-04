import json
from pathlib import Path

from app.http import multipart, peticion

URL_BOT = "https://api.telegram.org/bot{token}/{metodo}"
LONGITUD_MAXIMA = 4096
LONGITUD_MAXIMA_PIE = 1024
TAMANO_MAXIMO_VIDEO = 50 * 1024 * 1024


class ErrorTelegram(Exception):
    pass


class ClienteTelegram:
    def __init__(self, token, chat_id, http=peticion):
        self.token = token
        self.chat_id = chat_id
        self.http = http

    def _respuesta(self, r):
        if r.estado != 200 or not isinstance(r.cuerpo, dict) or not r.cuerpo.get("ok"):
            descripcion = r.cuerpo.get("description") if isinstance(r.cuerpo, dict) else r.cuerpo
            raise ErrorTelegram(f"Telegram respondio HTTP {r.estado}: {descripcion}")
        return r.cuerpo["result"]

    def _llamar(self, metodo, cuerpo=None):
        return self._respuesta(self.http("POST", URL_BOT.format(token=self.token, metodo=metodo), json_cuerpo=cuerpo or {}))

    def enviar(self, texto: str, botones=None):
        cuerpo = {"chat_id": self.chat_id, "text": texto[:LONGITUD_MAXIMA], "disable_web_page_preview": True}
        if botones:
            cuerpo["reply_markup"] = {"inline_keyboard": botones}
        return self._llamar("sendMessage", cuerpo)

    def enviar_video(self, ruta: Path, pie: str, botones=None):
        if ruta.stat().st_size > TAMANO_MAXIMO_VIDEO:
            raise ErrorTelegram(f"El video ocupa mas de 50 MB ({ruta.stat().st_size // 1048576} MB)")
        campos = {"chat_id": self.chat_id, "caption": pie[:LONGITUD_MAXIMA_PIE], "supports_streaming": "true"}
        if botones:
            campos["reply_markup"] = json.dumps({"inline_keyboard": botones})
        datos, tipo = multipart(campos, {"video": (ruta.name, ruta.read_bytes(), "video/mp4")})
        r = self.http("POST", URL_BOT.format(token=self.token, metodo="sendVideo"), datos=datos, tipo_datos=tipo,
                      timeout=300)
        return self._respuesta(r)

    def quitar_botones(self, message_id):
        self._llamar("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id,
                                                "reply_markup": {"inline_keyboard": []}})

    def responder_boton(self, callback_id, texto):
        self._llamar("answerCallbackQuery", {"callback_query_id": callback_id, "text": texto[:200]})

    def actualizaciones(self, desde_id):
        return self._llamar("getUpdates", {"offset": desde_id, "timeout": 0,
                                           "allowed_updates": ["message", "callback_query"]})

    def chats_recientes(self) -> list:
        chats = {}
        for update in self._llamar("getUpdates"):
            chat = (update.get("message") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
        return list(chats.items())
