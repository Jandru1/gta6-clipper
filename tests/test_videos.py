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
from app.preparacion import (Preparador, agrupar_palabras, censurar, cuentas_destino, documento_ass, gancho,
                             leer_palabras, leer_transcripcion, textos, titulo_valido)
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
        largo = textos(CUENTA_KAI, ficha_clip(), None, "x" * 150, False, ["#short", "shortvideo"])["youtube_titulo"]
        self.assertTrue(largo.endswith(" #short #shortvideo"))
        self.assertLessEqual(len(largo), 100)
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

    def test_subtitulos_palabra_a_palabra(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "t.json"
            tokens = [("[_BEG_]", 0, 0), (" bro", 2000, 2200), (" what", 2250, 2400), (" is", 2400, 2500),
                      (" th", 2500, 2600), ("at", 2600, 2700), (" [Music]", 2800, 2900), (" no", 4000, 4200)]
            ruta.write_text(json.dumps({"transcription": [{"offsets": {"from": 0, "to": 5000}, "text": "x", "tokens": [
                {"text": t, "offsets": {"from": a, "to": b}} for t, a, b in tokens]}]}))
            palabras = leer_palabras(ruta)
        self.assertEqual([p["texto"] for p in palabras], ["bro", "what", "is", "that", "no"])
        self.assertEqual(agrupar_palabras(palabras, 3), [
            {"texto": "bro what is", "desde_ms": 2000, "hasta_ms": 2500},
            {"texto": "that", "desde_ms": 2500, "hasta_ms": 2800},
            {"texto": "no", "desde_ms": 4000, "hasta_ms": 4300}])

    def test_la_primera_palabra_no_arrastra_el_silencio_anterior(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "t.json"
            ruta.write_text(json.dumps({"transcription": [{"offsets": {"from": 0, "to": 11000}, "text": "x", "tokens": [
                {"text": " hello", "offsets": {"from": 0, "to": 10300}},
                {"text": " guys", "offsets": {"from": 10300, "to": 10600}}]}]}))
            palabras = leer_palabras(ruta)
        self.assertEqual(palabras[0], {"texto": "hello", "desde_ms": 9900, "hasta_ms": 10300})
        self.assertEqual(agrupar_palabras(palabras, 3)[0]["desde_ms"], 9900)

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
                {"offsets": {"from": 0, "to": 2000}, "text": " no way bro look at this",
                 "tokens": [{"text": t, "offsets": {"from": 300 * i, "to": 300 * i + 250}}
                            for i, t in enumerate([" no", " way", " bro", " look", " at", " this"])]}]}))
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

    def actualizaciones(self, desde, espera_s=0):
        self.pendientes = [u for u in self.pendientes if u["update_id"] >= desde]
        return list(self.pendientes)

    def responder_boton(self, callback_id, texto):
        self.respuestas.append(texto)

    def quitar_botones(self, message_id):
        self.botones_quitados.append(message_id)

    def cambiar_botones(self, message_id, botones):
        self.botones_cambiados = getattr(self, "botones_cambiados", []) + [(message_id, botones)]


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
        self.assertEqual(len(self.tg.videos), 1)
        nombre, pie, botones = self.tg.videos[0]
        self.assertEqual(nombre, "vertical_sin_aviso.mp4")
        self.assertIn("Cuentas: kai, gta", pie)
        self.assertIn("YouTube Kai Cenat: subido en PRIVADO", pie)
        self.assertIn("YouTube GTA: subelo tu a mano", pie)
        self.assertEqual([b["callback_data"] for b in botones[0]], ["p:1", "d:1"])
        self.assertEqual(len(botones), 1)
        self.assertEqual(self.youtube.subidos, [("Kai Cenat: \"no way bro look at this\" #shorts #shortvideo", False)])
        self.assertTrue(any("INSTAGRAM (copia todo esto)" in m for m in self.tg.mensajes))
        self.assertEqual(self.bd.con.execute("SELECT estado FROM cola_preparacion").fetchone()[0], "preparado")

    def _pulsar(self, data, chat="9", veces=1, ahora_s=None):
        for _ in range(veces):
            self.siguiente_update = getattr(self, "siguiente_update", 6) + 1
            self.tg.pendientes.append({"update_id": self.siguiente_update, "callback_query": {
                "id": f"b{self.siguiente_update}", "data": data, "message": {"chat": {"id": chat}, "message_id": 101}}})
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios,
                                 ahora_s or int(AHORA.timestamp()) + 60)

    def test_boton_publicar_y_seguridad_del_chat(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("p:1", chat="666")
        self.assertEqual(self.youtube.publicos, [])
        self._pulsar("p:1")
        self.assertEqual(self.youtube.publicos, ["yt1"])
        self.assertEqual(self.bd.version(1)["estado"], "publicado")
        self.assertIn("https://youtube.com/shorts/yt1", self.tg.mensajes[-1])
        self._pulsar("p:1")
        self.assertEqual(self.tg.respuestas[-1], "Ya estaba publicado. No hago nada.")

    def test_si_telegram_ya_no_acepta_la_respuesta_igual_confirma(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))

        def caducada(*a):
            raise RuntimeError("query is too old and response timeout expired")
        self.tg.responder_boton = caducada
        self._pulsar("p:1")
        self.assertEqual(self.bd.version(1)["estado"], "publicado")
        self.assertIn("https://youtube.com/shorts/yt1", self.tg.mensajes[-1])
        self.assertEqual(self.tg.botones_quitados, [101])

    def test_boton_descartar_borra_de_youtube(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("d:1")
        self.assertEqual(self.youtube.borrados, ["yt1"])
        self.assertEqual(self.bd.version(1)["estado"], "descartado")

    def test_descartar_confirma_quita_botones_y_queda_registrado(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("d:1")
        self.assertIn("Descartado (kai)", self.tg.mensajes[-1])
        self.assertEqual(self.tg.botones_quitados, [101])
        registro = (self.carpeta / "registro_botones.csv").read_text()
        self.assertIn(",7,d:1,descartado", registro)

    def test_pulsar_siete_veces_publica_una_sola_vez_y_sin_mensajes_de_mas(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        antes = len(self.tg.mensajes)
        self._pulsar("p:1", veces=7)
        self.assertEqual(self.youtube.publicos, ["yt1"])
        self.assertEqual(len(self.tg.mensajes), antes + 1)
        self.assertEqual(self.tg.respuestas.count("Ya estaba publicado. No hago nada."), 6)
        self._pulsar("d:1")
        self.assertEqual(self.youtube.borrados, [])
        self.assertEqual(self.bd.version(1)["estado"], "publicado")

    def test_la_posicion_de_telegram_se_guarda_y_no_se_repite_nada(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("p:1")
        self.tg.pendientes.append({"update_id": 1, "callback_query": {"id": "viejo", "data": "d:1",
                                                                     "message": {"chat": {"id": "9"}}}})
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertEqual(self.youtube.borrados, [])
        self.assertEqual(self.bd.valor(videos.CLAVE_OFFSET), "8")

    def test_boton_de_video_aun_preparandose_se_aplaza_y_luego_se_hace(self):
        ahora = int(AHORA.timestamp())
        vid = self.bd.nueva_version({"clip_id": "c1", "cuenta": "kai", "con_aviso": 0, "estado": "preparado",
                                     "creado_s": ahora, "youtube_id": "ytX"})
        self._pulsar(f"p:{vid}", ahora_s=ahora + 30)
        self.assertEqual(self.youtube.publicos, [])
        self.assertIn("terminando de preparar", self.tg.respuestas[-1])
        self.bd.actualizar_version(vid, estado="enviado")
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, ahora + 90)
        self.assertEqual(self.youtube.publicos, ["ytX"])
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, ahora + 150)
        self.assertEqual(self.youtube.publicos, ["ytX"])

    def test_si_falla_se_reintenta_y_luego_avisa_sin_perder_las_siguientes(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        original = self.tg.enviar
        self.tg.pendientes.append({"update_id": 20, "message": {"chat": {"id": "9"}, "text": "/mejores"}})
        self.tg.pendientes.append({"update_id": 21, "callback_query": {"id": "z", "data": "p:1",
                                                                      "message": {"chat": {"id": "9"}}}})

        def roto(texto, botones=None):
            if "mejores" in texto or "No quedan" in texto:
                raise RuntimeError("Telegram caido")
            return original(texto)
        self.tg.enviar = roto
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
            self.assertEqual(self.youtube.publicos, [])
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertEqual(self.youtube.publicos, ["yt1"])
        self.assertTrue(any("No he podido atender" in m for m in self.tg.mensajes))

    def test_descartar_dos_veces_aunque_youtube_ya_no_lo_tenga(self):
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))

        def ya_borrado(video_id):
            raise RuntimeError("Error de YouTube (HTTP 404): videoNotFound")
        self.youtube.borrar = ya_borrado
        self._pulsar("d:1")
        self.assertEqual(self.bd.version(1)["estado"], "descartado")

    def test_mejores_dos_veces_seguidas_no_duplica(self):
        ahora = int(AHORA.timestamp())
        self.bd.con.execute("INSERT INTO momentos (momento_id, streamer, inicio_s, clips, representante, vistas, "
                            "vistas_mejor_clip, nota_maxima, actualizado_s) VALUES ('c1', 'kaicenat', ?, 1, 'c1', 5, 5, "
                            "9, ?)", (ahora, ahora))
        self.bd.con.execute("DELETE FROM cola_preparacion")
        for i in (30, 31):
            self.tg.pendientes.append({"update_id": i, "message": {"chat": {"id": "9"}, "text": "/mejores"}})
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, ahora)
        self.assertIn("Ya estoy preparando", self.tg.mensajes[-1])
        self.assertEqual(self.bd.cola_por_preparar(), 1)

    def test_ejecutar_no_lee_telegram_si_la_escucha_continua_esta_viva(self):
        ahora = int(AHORA.timestamp())
        self.bd.fijar_valor(videos.CLAVE_LATIDO, str(ahora - 30))
        self.assertTrue(videos.escucha_telegram_activa(self.bd, ahora))
        self.assertFalse(videos.escucha_telegram_activa(self.bd, ahora + 600))

    def test_escucha_continua_sobrevive_a_errores(self):
        llamadas = {"n": 0}

        class TelegramIntermitente(TelegramVideos):
            def actualizaciones(self_, desde, espera_s=0):
                llamadas["n"] += 1
                if llamadas["n"] == 1:
                    raise RuntimeError("sin red")
                return super().actualizaciones(desde, espera_s)
        tg = TelegramIntermitente()
        registro = []
        videos.escuchar_telegram(self.cfg, self.bd, tg, lambda: self.servicios, lambda: {}, registro=registro.append,
                                 reloj=lambda: AHORA.timestamp(), dormir=lambda s: None, vueltas=2, espera_s=0)
        self.assertEqual(llamadas["n"], 2)
        self.assertIn("sin red", registro[0])

    def test_preparar_un_clip_concreto_no_coge_otro_de_la_cola(self):
        c = clip("c2", 900, AHORA)
        self.bd.observar_clip("kaicenat", c, int(AHORA.timestamp()), self.cfg)
        self.bd.encolar("c2", "c2", 9, int(AHORA.timestamp()) + 5)
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()), solo_clip="c2")
        estados = dict(self.bd.con.execute("SELECT clip_id, estado FROM cola_preparacion").fetchall())
        self.assertEqual(estados, {"c1": "pendiente", "c2": "preparado"})

    def test_permiso_de_youtube_caducado_no_rompe_y_avisa_una_vez_al_dia(self):
        from app.ejecucion import _avisar_permiso_youtube
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self._pulsar("p:1")

        def caducado(ids):
            raise RuntimeError("Token has been expired or revoked")
        self.youtube.visualizaciones = caducado
        errores, estado = {}, {}
        videos.medir(self.bd, self.servicios, int(AHORA.timestamp()) + 7200, errores)
        self.assertIn("kai", errores)
        _avisar_permiso_youtube(estado, self.tg, errores, AHORA)
        _avisar_permiso_youtube(estado, self.tg, errores, AHORA)
        avisos = [m for m in self.tg.mensajes if "autorizar-youtube --cuenta kai" in m]
        self.assertEqual(len(avisos), 1)

    def test_mensaje_mejores_encola_los_3_mejores_no_usados(self):
        ahora = int(AHORA.timestamp())
        for i, (nota, vistas) in enumerate([(9, 100), (10, 50), (7, 900), (8, 300), (10, 20)]):
            cid = f"m{i}"
            self.bd.observar_clip("kaicenat", clip(cid, vistas, AHORA), ahora, self.cfg)
            self.bd.con.execute("INSERT INTO momentos (momento_id, streamer, inicio_s, clips, representante, vistas, "
                                "vistas_mejor_clip, nota_maxima, actualizado_s) VALUES (?, 'kaicenat', ?, 1, ?, ?, ?, ?, ?)",
                                (cid, ahora - 3600, cid, vistas, vistas, nota, ahora))
        self.bd.encolar("m4", "m4", 10, ahora)
        estado = {}
        self.tg.pendientes.append({"update_id": 9, "message": {"chat": {"id": "9"}, "text": "/mejores"}})
        videos.procesar_telegram(self.cfg, estado, self.bd, self.tg, self.servicios, ahora)
        encolados = {r[0] for r in self.bd.con.execute("SELECT clip_id FROM cola_preparacion WHERE estado = 'pendiente'")}
        self.assertEqual(encolados, {"c1", "m4", "m1", "m0", "m3"})
        self.assertIn("Los 3 mejores momentos de kaicenat", self.tg.mensajes[-1])
        self.tg.pendientes.append({"update_id": 10, "message": {"chat": {"id": "9"}, "text": "mejores"}})
        videos.procesar_telegram(self.cfg, estado, self.bd, self.tg, self.servicios, ahora + 200)
        self.assertIn("Los 1 mejores", self.tg.mensajes[-1])
        self.tg.pendientes.append({"update_id": 11, "message": {"chat": {"id": "9"}, "text": "/mejores"}})
        videos.procesar_telegram(self.cfg, estado, self.bd, self.tg, self.servicios, ahora + 400)
        self.assertIn("No quedan momentos", self.tg.mensajes[-1])

    def test_clip_de_chat_prefiere_el_de_un_espectador_y_evita_avisos_dobles(self):
        ahora = int(AHORA.timestamp())
        self.bd.con.execute("DELETE FROM cola_preparacion")
        self.bd.con.execute("DELETE FROM clips")
        propio = clip("nuestro", 3, AHORA)
        propio["creator_name"] = "jandru1"
        humano = clip("humano", 80, AHORA)
        for c in (propio, humano):
            self.bd.observar_clip("kaicenat", c, ahora, self.cfg)
        self.bd.con.execute("UPDATE clips SET creador = 'jandru1' WHERE clip_id = 'nuestro'")
        self.bd.con.execute("UPDATE clips SET creador = 'otro' WHERE clip_id = 'humano'")
        inicio = self.bd.clip("nuestro")["creado_s"] - 20
        self.bd.guardar_clip_de_chat({"clip_id": "nuestro", "streamer": "kaicenat", "inicio_s": inicio,
                                      "creado_s": ahora, "factor_maximo": 6, "usuarios_maximo": 90, "texto_top": "kekw"})
        avisos = videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora)
        self.assertEqual(avisos, 1)
        self.assertIn("clip de un espectador", self.tg.mensajes[-1])
        cola = [r[0] for r in self.bd.con.execute("SELECT clip_id FROM cola_preparacion")]
        self.assertEqual(cola, ["humano"])
        self.assertTrue(self.bd.pico_de_chat_encolado_cerca("kaicenat", inicio + 100, 300))
        self.assertEqual(videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora), 0)

    def test_clip_de_chat_que_twitch_no_publica_se_da_por_fallido(self):
        ahora = int(AHORA.timestamp())
        self.bd.guardar_clip_de_chat({"clip_id": "nunca", "streamer": "kaicenat", "inicio_s": ahora, "creado_s": ahora,
                                      "factor_maximo": 6, "usuarios_maximo": 90})
        videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora + 60)
        self.assertEqual(self.bd.clips_de_chat_pendientes()[0]["estado"], "creado")
        videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora + 3600)
        self.assertEqual(self.bd.clips_de_chat_pendientes(), [])

    def test_un_solo_video_con_botones_por_cuenta_y_se_quitan_por_separado(self):
        yt_gta = YouTubeFalso()
        self.servicios.youtube["gta"] = yt_gta
        videos.procesar_cola(self.cfg, self.bd, self.tg, self.servicios, int(AHORA.timestamp()))
        self.assertEqual(len(self.tg.videos), 1)
        _, _, botones = self.tg.videos[0]
        self.assertEqual([[b["text"] for b in fila] for fila in botones],
                         [["Publicar en Kai Cenat", "Descartar Kai Cenat"], ["Publicar en GTA", "Descartar GTA"]])
        self._pulsar("p:1")
        self.assertEqual(self.youtube.publicos, ["yt1"])
        self.assertEqual(yt_gta.publicos, [])
        self.assertEqual(self.tg.botones_cambiados[-1][1], [botones[1]])
        self._pulsar("p:2")
        self.assertEqual(yt_gta.publicos, ["yt1"])
        self.assertEqual(self.tg.botones_quitados, [101])

    def test_canal_que_no_deja_crear_clips_usa_el_de_un_espectador(self):
        ahora = int(AHORA.timestamp())
        self.bd.con.execute("DELETE FROM cola_preparacion")
        inicio = self.bd.clip("c1")["creado_s"] - 20
        self.bd.guardar_clip_de_chat({"clip_id": f"sin:kaicenat:{inicio}", "streamer": "kaicenat", "inicio_s": inicio,
                                      "creado_s": ahora, "factor_maximo": 20, "usuarios_maximo": 300})
        videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora)
        self.assertEqual([r[0] for r in self.bd.con.execute("SELECT clip_id FROM cola_preparacion")], ["c1"])
        self.assertIn("clip de un espectador", self.tg.mensajes[-1])

    def test_clip_propio_que_no_llega_en_el_repaso_se_busca_por_su_id(self):
        ahora = int(AHORA.timestamp())
        self.bd.con.execute("DELETE FROM clips")
        self.bd.con.execute("DELETE FROM cola_preparacion")

        class TwitchFalso:
            def clips_por_id(self_, ids):
                return [clip(i, 0, AHORA) for i in ids]
        self.bd.guardar_clip_de_chat({"clip_id": "propio", "streamer": "kaicenat", "inicio_s": ahora - 30,
                                      "creado_s": ahora, "factor_maximo": 20, "usuarios_maximo": 300})
        videos.encolar_clips_de_chat(self.cfg, self.bd, self.tg, ahora, twitch=TwitchFalso())
        self.assertEqual([r[0] for r in self.bd.con.execute("SELECT clip_id FROM cola_preparacion")], ["propio"])

    def test_limite_diario_de_publicaciones_por_cuenta(self):
        from dataclasses import replace
        self.cfg = replace(self.cfg, max_publicaciones_al_dia_por_cuenta=1)
        ahora = int(AHORA.timestamp())
        for vid in (1, 2):
            self.bd.nueva_version({"clip_id": "c1", "cuenta": "kai", "con_aviso": 0, "estado": "enviado",
                                   "creado_s": ahora, "youtube_id": f"y{vid}"})
        self._pulsar("p:1")
        self._pulsar("p:2")
        self.assertEqual(self.youtube.publicos, ["y1"])
        self.assertIn("limite: 1", self.tg.mensajes[-1])
        self.assertEqual(self.bd.version(2)["estado"], "enviado")

    def test_publicados_por_telegram(self):
        ahora = int(AHORA.timestamp())
        self.bd.nueva_version({"clip_id": "c1", "cuenta": "kai", "con_aviso": 0, "estado": "publicado",
                               "creado_s": ahora, "publicado_s": ahora, "youtube_id": "y1"})
        self.bd.nueva_version({"clip_id": "c1", "cuenta": "kai", "con_aviso": 0, "estado": "enviado",
                               "creado_s": ahora - 86400, "youtube_id": "y2"})
        self.tg.pendientes.append({"update_id": 40, "message": {"chat": {"id": "9"}, "text": "/publicados 3"}})
        videos.procesar_telegram(self.cfg, {}, self.bd, self.tg, self.servicios, ahora)
        self.assertIn("ultimos 3 dias", self.tg.mensajes[-1])
        self.assertIn("kai: 1 publicos | 1 privados", self.tg.mensajes[-1])

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
