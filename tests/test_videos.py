import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import videos
from app.almacen import Almacen
from app.basedatos import BaseDatos
from app.config import Franja, cargar_config
from app.ejecucion import ejecutar_una_vez
from app.http import Respuesta, multipart
from app.preparacion import (Preparador, censurar, cuentas_destino, documento_ass, gancho, leer_transcripcion,
                             textos, titulo_valido)
from app.youtube import ClienteYouTube
from tests.test_detector import AHORA, TwitchFalso, clip

RUTA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
LANZAMIENTO = date(2026, 11, 19)


def config_videos(**cambios):
    valores = {"franjas": [Franja("00:00", "24:00", "alertas")], "streamers": ["kaicenat"]}
    valores.update(cambios)
    return replace(cargar_config(RUTA_CONFIG), **valores)


CUENTA_KAI = {"id": "kai", "tipo": "streamer", "streamer": "kaicenat", "nombre_streamer": "Kai Cenat",
              "hashtags": ["#KaiCenat", "#Twitch"], "youtube_automatico": True,
              "campana": {"activa": True, "hashtag_obligatorio": "#KaiCenatCampaign", "desde": "2026-11-19",
                          "hasta": "2026-12-31"}}
CUENTA_GTA = {"id": "gta", "tipo": "gta", "hashtags": ["#GTA", "#Twitch"], "youtube_automatico": True,
              "campana": {"activa": False}}
JUEGOS = {"1867279146": {"nombre": "GTA VI", "hashtag": "#GTA6", "es_gta": True, "es_gta_vi": True},
          "32982": {"nombre": "GTA V", "hashtag": "#GTA5", "es_gta": True, "es_gta_vi": False}}


def ficha_clip(juego="32982", creado=AHORA, titulo="Kai was shook from AI video"):
    return {"clip_id": "c1", "streamer": "kaicenat", "titulo": titulo, "url": "https://clips.twitch.tv/c1",
            "juego_id": juego, "creado_s": int(creado.timestamp()), "duracion_s": 30.0}


class TestTextos(unittest.TestCase):
    def test_filtro_de_titulos(self):
        self.assertTrue(titulo_valido("Kai was Shook from ai video"))
        for malo in (".", "lol", "kai", "⚠️LIVE⚠️CLICK⚠️HERE⚠️DRAMA⚠️GTA V", "[663/730] 🔴 MY FIRST TIME PLAYING",
                     "esto no es ingles mañana ñññ ñññ", "a" * 30 + " " + "b" * 30 + " c"):
            self.assertFalse(titulo_valido(malo), malo)

    def test_gancho_titulo_luego_subtitulos_luego_streamer(self):
        segmentos = [{"texto": "bro", "desde_ms": 0, "hasta_ms": 500},
                     {"texto": "bro what is that thing over there right now man", "desde_ms": 500, "hasta_ms": 3000}]
        self.assertEqual(gancho("Kai was shook from AI video", segmentos, "Kai Cenat", []), ("Kai was shook from AI video", "titulo"))
        self.assertEqual(gancho("lol", segmentos, "Kai Cenat", []), ('"bro what is that thing over there right"', "subtitulos"))
        self.assertEqual(gancho("lol", [], "Kai Cenat", []), ("Kai Cenat", "streamer"))

    def test_censura(self):
        self.assertEqual(censurar("what the FUCK bro shitty", ["fuck", "shit"]), "what the F*** bro s*****")

    def test_textos_con_aviso_y_limites_de_hashtags(self):
        t = textos(CUENTA_KAI, ficha_clip(), JUEGOS["32982"], "KAI WAS SHOOK", True)
        self.assertEqual(t["youtube_titulo"], "Kai Cenat: Kai was shook")
        self.assertTrue(t["instagram_texto"].startswith("#Ad\n"))
        self.assertIn("Credit: twitch.tv/kaicenat", t["instagram_texto"])
        self.assertEqual(t["youtube_descripcion"].splitlines()[-1], "#KaiCenatCampaign #KaiCenat #Twitch")
        self.assertEqual(t["instagram_texto"].splitlines()[-1].split(), ["#KaiCenatCampaign", "#KaiCenat", "#Twitch", "#GTA5"])
        sin = textos(CUENTA_GTA, ficha_clip(), JUEGOS["32982"], "Kai was shook", False)
        self.assertNotIn("#Ad", sin["instagram_texto"])

    def test_destino_bloquea_gta_vi_antes_del_lanzamiento(self):
        destino, motivo = cuentas_destino(ficha_clip("1867279146"), [CUENTA_KAI, CUENTA_GTA], JUEGOS, LANZAMIENTO)
        self.assertEqual(destino, [])
        self.assertIn("lanzamiento", motivo)

    def test_destino_cuenta_gta_solo_con_juegos_gta_y_campana_por_fechas(self):
        destino, _ = cuentas_destino(ficha_clip("32982"), [CUENTA_KAI, CUENTA_GTA], JUEGOS, LANZAMIENTO)
        self.assertEqual([(c["id"], aviso) for c, aviso in destino], [("kai", False), ("gta", False)])
        destino, _ = cuentas_destino(ficha_clip("999"), [CUENTA_KAI, CUENTA_GTA], JUEGOS, LANZAMIENTO)
        self.assertEqual([c["id"] for c, _ in destino], ["kai"])
        despues = datetime(2026, 11, 20, tzinfo=timezone.utc)
        destino, _ = cuentas_destino(ficha_clip("1867279146", despues), [CUENTA_KAI, CUENTA_GTA], JUEGOS, LANZAMIENTO)
        self.assertEqual([(c["id"], aviso) for c, aviso in destino], [("kai", True), ("gta", False)])

    def test_documento_ass(self):
        tema = cargar_config(RUTA_CONFIG).tema
        doc = documento_ass([{"texto": "hello {there}", "desde_ms": 1000, "hasta_ms": 2500},
                             {"texto": "[Music]", "desde_ms": 3000, "hasta_ms": 4000}], "Kai was shook", True, tema, 30000, [])
        self.assertIn("PlayResX: 1080", doc)
        self.assertIn("Gancho,,0,0,0,,KAI WAS SHOOK", doc)
        self.assertIn("Aviso,,0,0,0,,#Ad", doc)
        self.assertIn("0:00:01.00,0:00:02.50,Subtitulo,,0,0,0,,hello (there)", doc)
        self.assertNotIn("Music", doc)
        self.assertIn("&H0000D4FF", doc)

    def test_leer_transcripcion(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "t.json"
            ruta.write_text(json.dumps({"result": {"language": "en"}, "transcription": [
                {"offsets": {"from": 0, "to": 1200}, "text": " hi there"}, {"offsets": {"from": 1200, "to": 1300}, "text": " "}]}))
            self.assertEqual(leer_transcripcion(ruta), ([{"desde_ms": 0, "hasta_ms": 1200, "texto": "hi there"}], "en"))


class EjecutorFalso:
    def __init__(self, tamano=1000):
        self.comandos, self.tamano = [], tamano

    def __call__(self, comando, carpeta=None, timeout=0):
        comando = [str(c) for c in comando]
        self.comandos.append(comando)
        if "yt-dlp" in comando[0]:
            Path(comando[comando.index("-o") + 1]).write_bytes(b"video")
        elif "whisper" in comando[0]:
            (Path(carpeta) / "transcripcion.json").write_text(json.dumps({"result": {"language": "en"}, "transcription": [
                {"offsets": {"from": 0, "to": 2000}, "text": " no way bro look at this"}]}))
        elif comando[-1].startswith("vertical_"):
            (Path(carpeta) / comando[-1]).write_bytes(b"x" * self.tamano)
        return ""


class TelegramVideos:
    def __init__(self, chat_id="9"):
        self.chat_id, self.mensajes, self.videos, self.respuestas, self.botones_quitados = chat_id, [], [], [], []
        self.pendientes = []

    def enviar(self, texto, botones=None):
        self.mensajes.append(texto)
        return {"message_id": len(self.mensajes)}

    def enviar_video(self, ruta, pie, botones=None):
        self.videos.append((Path(ruta).name, pie, botones))
        return {"message_id": 100 + len(self.videos)}

    def actualizaciones(self, desde):
        lista = [u for u in self.pendientes if u["update_id"] >= desde]
        self.pendientes = []
        return lista

    def responder_boton(self, callback_id, texto):
        self.respuestas.append(texto)

    def quitar_botones(self, message_id):
        self.botones_quitados.append(message_id)


class YouTubeFalso:
    def __init__(self):
        self.subidos, self.publicos, self.borrados = [], [], []

    def subir(self, ruta, titulo, descripcion, promocion):
        self.subidos.append((titulo, promocion))
        return f"yt{len(self.subidos)}", ""

    def hacer_publico(self, video_id):
        self.publicos.append(video_id)

    def borrar(self, video_id):
        self.borrados.append(video_id)

    def visualizaciones(self, ids):
        return {i: 1234 for i in ids}

    def paises_ultimos_dias(self):
        return {"visualizaciones": 500, "porcentaje_tier1": 61}


class TestFlujoDeVideos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.carpeta = Path(self.tmp.name)
        self.bd = BaseDatos(self.carpeta / "clips.db")
        self.cfg = config_videos(cuentas=[CUENTA_KAI, CUENTA_GTA])
        self.youtube = YouTubeFalso()
        self.servicios = videos.Servicios(Preparador(self.cfg, self.carpeta / "videos", self.carpeta / "modelo.bin",
                                                     EjecutorFalso()), {"kai": self.youtube})
        self.tg = TelegramVideos()
        c = clip("c1", 900, AHORA)
        c["game_id"] = "32982"
        self.bd.observar_clip("kaicenat", c, int(AHORA.timestamp()), self.cfg)
        self.bd.encolar("c1", "c1", 9, int(AHORA.timestamp()))

    def tearDown(self):
        self.bd.cerrar()
        self.tmp.cleanup()

    def test_prepara_envia_y_sube_en_privado_solo_a_cuentas_autorizadas(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertEqual(len(self.tg.videos), 2)
        (nombre_kai, pie_kai, botones_kai), (_, pie_gta, botones_gta) = self.tg.videos
        self.assertEqual(nombre_kai, "vertical_sin_aviso.mp4")
        self.assertIn("subido en PRIVADO", pie_kai)
        self.assertEqual(botones_kai[0][0]["callback_data"], "p:1")
        self.assertIsNone(botones_gta)
        self.assertEqual(self.youtube.subidos, [("Kai Cenat: \"no way bro look at this\"", False)])
        self.assertTrue(any("INSTAGRAM (copia todo esto)" in m for m in self.tg.mensajes))
        self.assertEqual(self.bd.con.execute("SELECT estado FROM cola_preparacion").fetchone()[0], "preparado")

    def _pulsar(self, data, chat="9"):
        self.tg.pendientes.append({"update_id": 7, "callback_query": {"id": "b1", "data": data,
                                                                     "message": {"chat": {"id": chat}}}})
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, int(AHORA.timestamp()) + 60)

    def test_boton_publicar_y_seguridad_del_chat(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("p:1", chat="666")
        self.assertEqual(self.youtube.publicos, [])
        self._pulsar("p:1")
        self.assertEqual(self.youtube.publicos, ["yt1"])
        self.assertEqual(self.bd.version(1)["estado"], "publicado")
        self.assertIn("https://youtube.com/shorts/yt1", self.tg.mensajes[-1])
        self._pulsar("p:1")
        self.assertEqual(self.tg.respuestas[-1], "Ya estaba resuelto")

    def test_boton_descartar_borra_de_youtube(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("d:1")
        self.assertEqual(self.youtube.borrados, ["yt1"])
        self.assertEqual(self.bd.version(1)["estado"], "descartado")

    def test_gta_vi_antes_del_lanzamiento_no_se_prepara(self):
        self.bd.con.execute("UPDATE clips SET juego_id = '1867279146'")
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertEqual(self.tg.videos, [])
        self.assertIn("posible filtracion", self.tg.mensajes[0])

    def test_error_de_herramienta_avisa_y_no_reintenta_solo(self):
        def falla(*a, **k):
            raise RuntimeError("yt-dlp roto")
        self.servicios.preparador.ejecutar = falla
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertIn("preparar-clip c1", self.tg.mensajes[0])
        self.assertEqual(self.bd.pendientes_de_preparar(5), [])

    def test_medicion_a_1h_24h_7d_y_respuesta_semanal(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("p:1")
        videos.medir(self.bd, self.servicios, int(AHORA.timestamp()) + 2 * 3600)
        v = self.bd.version(1)
        self.assertEqual((v["vistas_1h"], v["vistas_24h"]), (1234, None))
        estado = {}
        domingo = datetime(2026, 11, 8, 11, 30, tzinfo=timezone.utc)
        self.assertEqual(videos.resumen_semanal(self.cfg, estado, self.bd, self.tg, self.servicios, domingo), 1)
        self.assertIn("publico Tier 1: 61%", self.tg.mensajes[-1])
        self.assertEqual(videos.resumen_semanal(self.cfg, estado, self.bd, self.tg, self.servicios, domingo), 0)
        self.tg.pendientes.append({"update_id": 9, "message": {"chat": {"id": "9"}, "text": "IG kai: 12.500 / 45 / no"}})
        videos.procesar_telegram(self.cfg, estado, self.bd, self.tg, self.servicios, int(domingo.timestamp()))
        fila = self.bd.con.execute("SELECT * FROM medicion_manual").fetchone()
        self.assertEqual((fila["cuenta"], fila["visualizaciones"], fila["porcentaje_eeuu"]), ("kai", 12500, 45))


class TestIntegracion(unittest.TestCase):
    def test_aviso_encola_y_envia_el_video(self):
        with tempfile.TemporaryDirectory() as d:
            almacen = Almacen(Path(d))
            bd = BaseDatos(almacen.ruta_base_datos)
            cfg = config_videos(cuentas=[CUENTA_KAI])
            servicios = videos.Servicios(Preparador(cfg, Path(d) / "videos", Path(d) / "m.bin", EjecutorFalso()), {})
            fondo = [clip(f"f{i}", 20, AHORA - timedelta(hours=3, minutes=10 * i)) for i in range(21)]
            tw = TwitchFalso(fondo)
            tg = TelegramVideos()
            ejecutar_una_vez(cfg, almacen, bd, tw, tg, ahora=AHORA, servicios=servicios)
            tw.datos["viral"] = clip("viral", 900, AHORA + timedelta(minutes=5))
            ejecutar_una_vez(cfg, almacen, bd, tw, tg, ahora=AHORA + timedelta(minutes=15), servicios=servicios)
            self.assertEqual(len(tg.videos), 1)
            self.assertIn("publicacion automatica desactivada o sin autorizar", tg.videos[0][1])
            bd.cerrar()


class HttpGuion:
    def __init__(self, respuestas):
        self.respuestas, self.llamadas = list(respuestas), []

    def __call__(self, metodo, url, **kwargs):
        self.llamadas.append((metodo, url, kwargs))
        return self.respuestas.pop(0)


TOKEN = Respuesta(200, {}, {"access_token": "a", "expires_in": 3600})


class TestYouTube(unittest.TestCase):
    def test_subida_reanudable_en_privado_con_promocion_pagada(self):
        http = HttpGuion([TOKEN, Respuesta(200, {"location": "https://subida/1"}, {}), Respuesta(200, {}, {"id": "v9"})])
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b"datos")
            f.flush()
            video_id, aviso = ClienteYouTube({"client_id": "i", "client_secret": "s"}, "r", http=http).subir(
                Path(f.name), "t", "d", True)
        self.assertEqual((video_id, aviso), ("v9", ""))
        inicio = http.llamadas[1][2]
        self.assertEqual(inicio["params"]["part"], "snippet,status,paidProductPlacementDetails")
        self.assertEqual(inicio["json_cuerpo"]["status"]["privacyStatus"], "private")
        self.assertEqual(inicio["cabeceras"]["X-Upload-Content-Length"], "5")
        self.assertEqual(http.llamadas[2][:2], ("PUT", "https://subida/1"))

    def test_si_rechaza_la_promocion_pagada_sube_igual_y_avisa(self):
        http = HttpGuion([TOKEN, Respuesta(400, {}, {"error": "x"}), Respuesta(200, {"location": "https://s/2"}, {}),
                          Respuesta(200, {}, {"id": "v2"})])
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            video_id, aviso = ClienteYouTube({"client_id": "i", "client_secret": "s"}, "r", http=http).subir(
                Path(f.name), "t", "d", True)
        self.assertEqual(video_id, "v2")
        self.assertIn("a mano", aviso)

    def test_paises_tier_1(self):
        http = HttpGuion([TOKEN, Respuesta(200, {}, {"rows": [["US", 60], ["ES", 30], ["GB", 10]]})])
        datos = ClienteYouTube({"client_id": "i", "client_secret": "s"}, "r", http=http).paises_ultimos_dias(hoy=date(2026, 11, 8))
        self.assertEqual(datos, {"visualizaciones": 100, "porcentaje_tier1": 70})


class TestMultipart(unittest.TestCase):
    def test_formato(self):
        cuerpo, tipo = multipart({"chat_id": "9"}, {"video": ("v.mp4", b"abc", "video/mp4")})
        self.assertTrue(tipo.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="chat_id"\r\n\r\n9\r\n', cuerpo)
        self.assertIn(b'filename="v.mp4"\r\nContent-Type: video/mp4\r\n\r\nabc\r\n', cuerpo)


if __name__ == "__main__":
    unittest.main()
