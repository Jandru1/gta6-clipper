import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.almacen import Almacen
from app.config import Config, ErrorConfiguracion, Franja, cargar_credenciales, guardar_credenciales_desde_entorno
from app.ejecucion import ejecutar_una_vez
from app.franjas import modo_actual
from app.http import ErrorRed, Respuesta
from app.twitch import ClienteTwitch, ErrorLimiteTwitch

TOKEN = Respuesta(200, {}, {"access_token": "t1", "expires_in": 5_000_000})


class HttpFalso:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, metodo, url, **kwargs):
        self.llamadas.append((metodo, url, kwargs))
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TelegramFalso:
    def __init__(self, fallar=False):
        self.mensajes = []
        self.fallar = fallar

    def enviar(self, texto):
        if self.fallar:
            raise RuntimeError("telegram caido")
        self.mensajes.append(texto)


def clip(id_, vistas=10):
    return {"id": id_, "title": f"clip {id_}", "view_count": vistas, "duration": 30.0,
            "created_at": "2026-10-29T01:00:00Z", "url": f"https://clips.twitch.tv/{id_}",
            "video_id": "v1", "vod_offset": 120}


def config(modo="alertas", max_avisos=3):
    return Config("Europe/Madrid", 5, ["kaicenat"], 24, 3, max_avisos, [Franja("00:00", "24:00", modo)])


USUARIOS = Respuesta(200, {"ratelimit-remaining": "799"}, {"data": [{"login": "kaicenat", "id": "123"}]})
SIN_DIRECTO = Respuesta(200, {}, {"data": []})


def pagina(clips, cursor=None):
    return Respuesta(200, {"ratelimit-remaining": "797"}, {"data": clips, "pagination": {"cursor": cursor} if cursor else {}})


class TestCredencialesDesdeEntorno(unittest.TestCase):
    def test_crea_fichero_privado_con_las_variables(self):
        entorno = {"TWITCH_ID_CLIENTE": "id", "TWITCH_SECRETO": "s", "TELEGRAM_GTA6_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "9"}
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "credenciales.json"
            guardar_credenciales_desde_entorno(ruta, entorno)
            creds = cargar_credenciales(ruta)
            self.assertEqual((creds.twitch_client_id, creds.telegram_chat_id), ("id", "9"))
            self.assertEqual(ruta.stat().st_mode & 0o777, 0o600)

    def test_falla_si_falta_alguna_variable(self):
        with tempfile.TemporaryDirectory() as carpeta:
            with self.assertRaises(ErrorConfiguracion) as ctx:
                guardar_credenciales_desde_entorno(Path(carpeta) / "c.json", {"TWITCH_ID_CLIENTE": "id"})
            self.assertIn("TWITCH_SECRETO", str(ctx.exception))


class TestFranjas(unittest.TestCase):
    def test_franja_normal_y_nocturna(self):
        franjas = [Franja("00:00", "07:30", "silencio"), Franja("18:00", "02:00", "alertas")]
        self.assertEqual(modo_actual(franjas, datetime(2026, 1, 1, 3, 0)), "silencio")
        self.assertEqual(modo_actual(franjas[1:], datetime(2026, 1, 1, 1, 0)), "alertas")
        self.assertEqual(modo_actual(franjas[1:], datetime(2026, 1, 1, 12, 0)), "silencio")

    def test_dia_completo(self):
        self.assertEqual(modo_actual([Franja("00:00", "24:00", "alertas")], datetime(2026, 1, 1, 23, 59)), "alertas")


class TestTwitch(unittest.TestCase):
    def test_reutiliza_token_vigente(self):
        http = HttpFalso([TOKEN, USUARIOS, USUARIOS])
        tw = ClienteTwitch("id", "s", {}, http=http)
        tw.ids_de_usuarios(["kaicenat"])
        tw.ids_de_usuarios(["kaicenat"])
        self.assertEqual(sum(1 for m, *_ in http.llamadas if m == "POST"), 1)

    def test_renueva_token_si_401(self):
        http = HttpFalso([TOKEN, Respuesta(401, {}, {}), TOKEN, USUARIOS])
        tw = ClienteTwitch("id", "s", {}, http=http)
        self.assertEqual(tw.ids_de_usuarios(["kaicenat"]), {"kaicenat": "123"})

    def test_429_lanza_error_de_limite(self):
        http = HttpFalso([TOKEN, Respuesta(429, {"ratelimit-reset": "100"}, {})])
        tw = ClienteTwitch("id", "s", {}, http=http, reloj=lambda: 40.0)
        with self.assertRaises(ErrorLimiteTwitch) as ctx:
            tw.ids_de_usuarios(["kaicenat"])
        self.assertEqual(ctx.exception.segundos_espera, 60)

    def test_paginacion_de_clips(self):
        http = HttpFalso([TOKEN, pagina([clip("a")], "c1"), pagina([clip("b")])])
        tw = ClienteTwitch("id", "s", {}, http=http)
        self.assertEqual([c["id"] for c in tw.clips("123", "x", "y", 3)], ["a", "b"])
        self.assertEqual(http.llamadas[2][2]["params"]["after"], "c1")


class TestEjecucion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.almacen = Almacen(Path(self.tmp.name))
        self.ahora = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def _ejecutar(self, respuestas, telegram=None, cfg=None):
        tw = ClienteTwitch("id", "s", self.almacen.estado, http=HttpFalso(respuestas))
        telegram = telegram or TelegramFalso()
        return ejecutar_una_vez(cfg or config(), self.almacen, tw, telegram, ahora=self.ahora), telegram

    def test_primera_ejecucion_no_inunda_y_luego_avisa_solo_nuevos(self):
        r, tg = self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip("a"), clip("b")])])
        self.assertEqual((r["resultado"], r["clips_nuevos"], len(tg.mensajes)), ("ok", 2, 1))
        self.assertIn("Primera ejecucion", tg.mensajes[0])
        r, tg = self._ejecutar([SIN_DIRECTO, pagina([clip("a"), clip("b"), clip("c", 99)])])
        self.assertEqual((r["clips_nuevos"], len(tg.mensajes)), (1, 1))
        self.assertIn("clip c", tg.mensajes[0])

    def test_modo_silencio_no_envia(self):
        r, tg = self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip("a")])], cfg=config("silencio"))
        self.assertEqual((r["clips_nuevos"], tg.mensajes), (1, []))

    def test_limita_avisos_por_ejecucion(self):
        self.almacen.estado["clips_vistos"] = {"viejo": "2026-10-29T00:00:00Z"}
        r, tg = self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip(str(i), i) for i in range(5)])],
                               cfg=config(max_avisos=2))
        self.assertEqual(len(tg.mensajes), 3)
        self.assertIn("clip 4", tg.mensajes[0])
        self.assertIn("y 3 clips nuevos mas", tg.mensajes[2])

    def test_sin_internet_registra_error_y_avisa_una_sola_vez(self):
        r, tg = self._ejecutar([ErrorRed("x")])
        self.assertEqual(r["resultado"], "error")
        self.assertEqual(len(tg.mensajes), 1)
        r, tg = self._ejecutar([ErrorRed("x")])
        self.assertEqual(tg.mensajes, [])
        self.assertEqual(len(self.almacen.leer_ejecuciones()), 2)

    def test_si_telegram_falla_los_clips_se_vuelven_a_avisar(self):
        self.almacen.estado["clips_vistos"] = {"viejo": "2026-10-29T00:00:00Z"}
        r, _ = self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip("a")])], telegram=TelegramFalso(fallar=True))
        self.assertEqual(r["resultado"], "error")
        r, tg = self._ejecutar([SIN_DIRECTO, pagina([clip("a")])])
        self.assertEqual(r["clips_nuevos"], 1)
        self.assertEqual(len(tg.mensajes), 1)

    def test_registra_observaciones_de_clips_con_hora_espanola(self):
        self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip("a", 50)])])
        with self.almacen.ruta_clips.open(newline="", encoding="utf-8") as f:
            fila = next(csv.DictReader(f))
        self.assertEqual(fila["hora_consulta_espana"], "29/10/2026 03:00:00")
        self.assertEqual(fila["creado_espana"], "29/10/2026 02:00:00")
        self.assertEqual((fila["clip_id"], fila["visualizaciones"]), ("a", "50"))
        self.assertEqual(self.almacen.leer_ejecuciones()[0]["hora_espana"], "29/10/2026 03:00:00")

    def test_csv_con_formato_antiguo_se_aparta(self):
        self.almacen.ruta_clips.write_text("consulta_utc,clip_id\nx,y\n", encoding="utf-8")
        self._ejecutar([TOKEN, USUARIOS, SIN_DIRECTO, pagina([clip("a")])])
        self.assertTrue((self.almacen.carpeta / "clips_observaciones_formato_antiguo.csv").exists())
        self.assertTrue(self.almacen.ruta_clips.read_text(encoding="utf-8").startswith("hora_consulta_espana"))


if __name__ == "__main__":
    unittest.main()
