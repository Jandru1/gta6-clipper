from app.http import peticion

URL_BOT = "https://api.telegram.org/bot{token}/{metodo}"
LONGITUD_MAXIMA = 4096


class ErrorTelegram(Exception):
    pass


class ClienteTelegram:
    def __init__(self, token, chat_id, http=peticion):
        self.token = token
        self.chat_id = chat_id
        self.http = http

    def _llamar(self, metodo, cuerpo=None):
        r = self.http("POST", URL_BOT.format(token=self.token, metodo=metodo), json_cuerpo=cuerpo or {})
        if r.estado != 200 or not isinstance(r.cuerpo, dict) or not r.cuerpo.get("ok"):
            descripcion = r.cuerpo.get("description") if isinstance(r.cuerpo, dict) else r.cuerpo
            raise ErrorTelegram(f"Telegram respondio HTTP {r.estado}: {descripcion}")
        return r.cuerpo["result"]

    def enviar(self, texto: str):
        self._llamar("sendMessage", {
            "chat_id": self.chat_id,
            "text": texto[:LONGITUD_MAXIMA],
            "disable_web_page_preview": True,
        })

    def chats_recientes(self) -> list:
        chats = {}
        for update in self._llamar("getUpdates"):
            chat = (update.get("message") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
        return list(chats.items())
