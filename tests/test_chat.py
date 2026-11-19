import socket
import tempfile
import unittest
from pathlib import Path

from app.basedatos import BaseDatos
from app.chat import (ConexionChat, CreadorDeClips, DetectorPicos, ErrorAutenticacionChat, ErrorChat, TokenUsuario,
                      autorizar_dispositivo, cruzar_con_clips, es_reaccion, escuchar, leer_privmsg)
from app.http import Respuesta
from app.youtube import guardar_tokens

AJUSTES = {"activo": True, "streamers": ["kaicenat"], "segundos_por_ventana": 10, "minutos_de_base": 2,
           "factor_de_pico": 3, "usuarios_minimos_en_la_ventana": 5, "segundos_para_unir_picos": 20,
           "dias_de_registro": 14}


def rellenar(detector, desde, hasta, por_ventana, usuarios=10, texto="hola"):
    salida = []
    for inicio in range(desde, hasta, 10):
        for i in range(por_ventana):
            salida.append(detector.mensaje("kaicenat", f"u{i % usuarios}", texto, inicio + i * 9 / max(por_ventana, 1)))
    return salida


class TestDetector(unittest.TestCase):
    def test_detecta_un_pico_y_lo_cierra_cuando_vuelve_la_calma(self):
        d = DetectorPicos(AJUSTES)
        rellenar(d, 0, 120, 10)
        rellenar(d, 120, 140, 50, usuarios=40, texto="KEKW KEKW")
        resultados = rellenar(d, 140, 200, 10)
        picos = [p for _, ps in resultados for p in ps]
        self.assertEqual(len(picos), 1)
        p = picos[0]
        self.assertEqual((p["inicio_s"], p["fin_s"]), (120, 140))
        self.assertGreaterEqual(p["factor_maximo"], 5)
        self.assertEqual(p["usuarios_maximo"], 40)
        self.assertEqual(p["reacciones"], 100)
        self.assertEqual(p["texto_top"], "kekw kekw")

    def test_no_es_pico_si_lo_escriben_pocas_personas(self):
        d = DetectorPicos(AJUSTES)
        rellenar(d, 0, 120, 10)
        resultados = rellenar(d, 120, 130, 60, usuarios=2) + rellenar(d, 130, 200, 10)
        self.assertEqual([p for _, ps in resultados for p in ps], [])

    def test_sin_ritmo_normal_todavia_no_hay_picos(self):
        d = DetectorPicos(AJUSTES)
        resultados = rellenar(d, 0, 20, 100, usuarios=50) + rellenar(d, 20, 40, 10)
        self.assertEqual([p for _, ps in resultados for p in ps], [])

    def test_el_silencio_no_genera_ventanas_ni_picos_falsos_al_volver(self):
        d = DetectorPicos(AJUSTES)
        rellenar(d, 0, 120, 10)
        ventanas, _ = d.avanzar("kaicenat", 5000)
        self.assertLessEqual(len(ventanas), 1)
        resultados = rellenar(d, 5000, 5100, 10)
        self.assertEqual([p for _, ps in resultados for p in ps], [])


class TestMensajes(unittest.TestCase):
    def test_leer_privmsg(self):
        linea = "@badge-info=;color=#FF0000 :Pepe!pepe@pepe.tmi.twitch.tv PRIVMSG #KaiCenat :NO WAY 😭"
        self.assertEqual(leer_privmsg(linea), ("kaicenat", "pepe", "NO WAY 😭"))
        self.assertIsNone(leer_privmsg(":tmi.twitch.tv 001 yo :Welcome"))

    def test_reacciones(self):
        self.assertTrue(es_reaccion("KEKW"))
        self.assertTrue(es_reaccion("bro 💀"))
        self.assertFalse(es_reaccion("what game is this"[5:]))


class HttpGuion:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, metodo, url, **kwargs):
        self.llamadas.append((metodo, url, kwargs))
        return self.respuestas.pop(0)


class TestAutorizacion(unittest.TestCase):
    def test_codigo_de_dispositivo_espera_y_guarda_login(self):
        http = HttpGuion([
            Respuesta(200, {}, {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://twitch.tv/activate",
                                "interval": 1, "expires_in": 600}),
            Respuesta(400, {}, {"status": 400, "message": "authorization_pending"}),
            Respuesta(200, {}, {"access_token": "a1", "refresh_token": "r1", "expires_in": 14000}),
            Respuesta(200, {}, {"login": "jandru", "scopes": ["chat:read", "clips:edit"]}),
        ])
        mostrado = []
        datos = autorizar_dispositivo("id", "secreto", mostrar=mostrado.append, http=http, esperar=lambda s: None,
                                      reloj=lambda: 1000)
        self.assertIn("ABCD", mostrado[0])
        self.assertEqual((datos["login"], datos["refresh_token"], datos["expira_en"]), ("jandru", "r1", 15000))
        self.assertEqual(http.llamadas[0][2]["formulario"]["scopes"], "chat:read clips:edit")

    def test_renueva_el_token_y_lo_guarda(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "twitch_usuario.json"
            guardar_tokens(ruta, {"access_token": "viejo", "refresh_token": "r1", "expira_en": 100, "login": "jandru"})
            http = HttpGuion([Respuesta(200, {}, {"access_token": "nuevo", "refresh_token": "r2", "expires_in": 14000})])
            token = TokenUsuario(ruta, "id", "secreto", http=http, reloj=lambda: 1000)
            self.assertEqual(token.acceso(), "nuevo")
            self.assertEqual(TokenUsuario(ruta, "id", "s", reloj=lambda: 1000).datos["refresh_token"], "r2")

    def test_sin_autorizar_avisa_del_comando(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ErrorAutenticacionChat) as ctx:
            TokenUsuario(Path(d) / "no.json", "id", "s")
        self.assertIn("autorizar-twitch", str(ctx.exception))


class SocketFalso:
    def __init__(self, bloques):
        self.bloques = list(bloques)
        self.enviado = []

    def settimeout(self, _):
        pass

    def sendall(self, datos):
        self.enviado.append(datos.decode())

    def recv(self, _):
        if not self.bloques:
            return b""
        b = self.bloques.pop(0)
        if b is None:
            raise socket.timeout()
        return b

    def close(self):
        pass


class TokenFalso:
    login = "jandru"

    def acceso(self):
        return "tok"


class TestConexion(unittest.TestCase):
    def test_se_identifica_responde_al_ping_y_entrega_mensajes(self):
        sock = SocketFalso([b"PING :tmi.twitch.tv\r\n:a!a@a.tmi.twitch.tv PRIVMSG #kaicenat :LUL\r\n:b!b@b", None,
                            b".tmi.twitch.tv PRIVMSG #kaicenat :hi\r\n"])
        c = ConexionChat(TokenFalso(), ["KaiCenat"], crear_socket=lambda: sock)
        c.conectar()
        leidos = []
        with self.assertRaises(ErrorChat):
            for m in c.leer():
                leidos.append(m)
        self.assertEqual(sock.enviado[:3], ["PASS oauth:tok\r\n", "NICK jandru\r\n", "JOIN #kaicenat\r\n"])
        self.assertIn("PONG :tmi.twitch.tv\r\n", sock.enviado)
        self.assertEqual([m for m in leidos if m], [("kaicenat", "a", "LUL"), ("kaicenat", "b", "hi")])

    def test_permiso_rechazado(self):
        sock = SocketFalso([b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n"])
        c = ConexionChat(TokenFalso(), ["kaicenat"], crear_socket=lambda: sock)
        c.conectar()
        with self.assertRaises(ErrorAutenticacionChat):
            list(c.leer())


class ConexionGuion:
    canales = ["kaicenat"]

    def __init__(self, mensajes):
        self.mensajes = mensajes

    def conectar(self):
        pass

    def cerrar(self):
        pass

    def leer(self):
        for m in self.mensajes:
            yield m
        raise ErrorChat("fin de la prueba")


class TestEscucharYCruzar(unittest.TestCase):
    def test_guarda_ventanas_y_picos_en_la_base_de_datos(self):
        reloj = {"t": 0.0}
        mensajes = []
        for inicio, n, usuarios in [(s, 10, 10) for s in range(0, 120, 10)] + [(120, 60, 40), (130, 10, 10),
                                                                              (140, 10, 10), (150, 10, 10),
                                                                              (160, 10, 10), (170, 10, 10)]:
            for i in range(n):
                mensajes.append((inicio + i * 9 / n, ("kaicenat", f"u{i % usuarios}", "OMEGALUL")))

        class Conexion(ConexionGuion):
            def leer(self_):
                for t, m in mensajes:
                    reloj["t"] = t
                    yield m
                raise ErrorChat("fin")

        with tempfile.TemporaryDirectory() as d:
            bd = BaseDatos(Path(d) / "clips.db")
            escuchar(AJUSTES, bd, TokenFalso(), registro=lambda t: None, conexion=Conexion([]),
                     reloj=lambda: reloj["t"], dormir=lambda s: None, vueltas=1)
            picos = bd.picos_chat_desde(0)
            ventanas = bd.con.execute("SELECT COUNT(*) FROM chat_ventanas").fetchone()[0]
            bd.cerrar()
        self.assertEqual(len(picos), 1)
        self.assertEqual(picos[0]["inicio_s"], 120)
        self.assertGreater(ventanas, 10)

    def test_cruza_picos_con_clips_de_espectadores(self):
        pico = {"streamer": "kaicenat", "inicio_s": 1000, "fin_s": 1020, "factor_maximo": 5}
        clips = [{"streamer": "kaicenat", "creado_s": 1100, "visualizaciones": 50, "creador": "a", "url": "u1"},
                 {"streamer": "kaicenat", "creado_s": 1200, "visualizaciones": 900, "creador": "b", "url": "u2"},
                 {"streamer": "kaicenat", "creado_s": 5000, "visualizaciones": 9999, "creador": "c", "url": "u3"},
                 {"streamer": "xqc", "creado_s": 1100, "visualizaciones": 9999, "creador": "d", "url": "u4"}]
        r = cruzar_con_clips([pico], clips)[0]
        self.assertEqual((r["clips_humanos"], r["personas_que_clipean"], r["vis_mejor_clip"], r["url_mejor_clip"]),
                         (2, 2, 900, "u2"))


AJUSTES_CLIPS = {**AJUSTES, "crear_clips": True, "factor_para_crear_clip": 4, "usuarios_para_crear_clip": 20,
                 "segundos_de_clip": 45, "max_clips_por_hora": 2, "minutos_entre_clips": 10}


class ClipsFalso:
    def __init__(self, falla=False):
        self.creados = []
        self.falla = falla

    def crear(self, login, segundos):
        if self.falla:
            raise ErrorChat("clips desactivados por el streamer")
        self.creados.append((login, segundos))
        return f"clip{len(self.creados)}"


def pico(inicio, factor=6, usuarios=50, streamer="kaicenat"):
    return {"streamer": streamer, "inicio_s": inicio, "fin_s": inicio + 10, "factor_maximo": factor,
            "usuarios_maximo": usuarios, "mensajes": 300, "reacciones": 200, "texto_top": "kekw", "base": 10}


class TestFase2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bd = BaseDatos(Path(self.tmp.name) / "clips.db")

    def tearDown(self):
        self.bd.cerrar()
        self.tmp.cleanup()

    def test_crea_un_clip_por_pico_fuerte_y_respeta_los_limites(self):
        clips = ClipsFalso()
        creador = CreadorDeClips(AJUSTES_CLIPS, clips, self.bd, registro=lambda t: None)
        creador.revisar([pico(1000), pico(1000)], 1010)
        creador.revisar([pico(1100, factor=2)], 1110)
        creador.revisar([pico(1200)], 1210)
        creador.revisar([pico(2000)], 2010)
        creador.revisar([pico(3000)], 3010)
        self.assertEqual(clips.creados, [("kaicenat", 45), ("kaicenat", 45)])
        self.assertEqual(len(self.bd.clips_de_chat_desde("kaicenat", 0)), 2)

    def test_si_twitch_no_deja_crear_el_clip_no_se_rompe(self):
        creador = CreadorDeClips(AJUSTES_CLIPS, ClipsFalso(falla=True), self.bd, registro=lambda t: None)
        creador.revisar([pico(1000)], 1010)
        self.assertEqual(self.bd.clips_de_chat_desde("kaicenat", 0), [])

    def test_cliente_de_clips_llama_a_la_api_oficial(self):
        http = HttpGuion([Respuesta(200, {}, {"data": [{"id": "123"}]}),
                          Respuesta(202, {}, {"data": [{"id": "ClipNuevo", "edit_url": "x"}]})])
        from app.chat import ClienteClips
        cliente = ClienteClips(TokenFalso(), "id", http=http)
        self.assertEqual(cliente.crear("kaicenat", 45), "ClipNuevo")
        metodo, url, kwargs = http.llamadas[1]
        self.assertEqual((metodo, url.endswith("/helix/clips"), kwargs["params"]),
                         ("POST", True, {"broadcaster_id": "123", "duration": 45}))


if __name__ == "__main__":
    unittest.main()
