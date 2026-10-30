import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.almacen import Almacen
from app.basedatos import BaseDatos
from app.config import (ErrorConfiguracion, Franja, cargar_config, cargar_credenciales,
                        guardar_credenciales_desde_entorno)
from app.ejecucion import ejecutar_una_vez, iso_utc, tramos
from app.franjas import modo_actual
from app.http import ErrorRed, Respuesta
from app.twitch import ClienteTwitch, ErrorLimiteTwitch
from app.viralidad import nota_1_a_10, velocidad_por_hora

RUTA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
AHORA = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TOKEN = Respuesta(200, {}, {"access_token": "t1", "expires_in": 5_000_000})


def config_base(**cambios):
    return replace(cargar_config(RUTA_CONFIG), **cambios)


def clip(id_, vistas, creado, streamer="kaicenat"):
    return {"id": id_, "title": f"clip {id_}", "view_count": vistas, "duration": 30.0, "created_at": iso_utc(creado),
            "url": f"https://clips.twitch.tv/{id_}", "video_id": "v1", "vod_offset": None, "creator_name": "fan"}


class TwitchFalso:
    def __init__(self, clips, en_directo=None):
        self.datos = {c["id"]: c for c in clips}
        self.en_directo = en_directo or {}
        self.llamadas = 0
        self.ratelimit = {}

    def ids_de_usuarios(self, logins):
        self.llamadas += 1
        return {l: "123" for l in logins}

    def directos(self, logins):
        self.llamadas += 1
        return self.en_directo

    def clips(self, broadcaster_id, desde, hasta, max_paginas, por_pagina):
        dentro = sorted((c for c in self.datos.values() if desde <= c["created_at"] < hasta),
                        key=lambda c: c["view_count"], reverse=True)
        paginas = max(1, -(-len(dentro) // por_pagina))
        self.llamadas += min(paginas, max_paginas)
        return [dict(c) for c in dentro[:max_paginas * por_pagina]], paginas > max_paginas


class TelegramFalso:
    def __init__(self, fallar=False):
        self.mensajes, self.fallar = [], fallar

    def enviar(self, texto):
        if self.fallar:
            raise RuntimeError("telegram caido")
        self.mensajes.append(texto)


class HttpFalso:
    def __init__(self, respuestas):
        self.respuestas, self.llamadas = list(respuestas), []

    def __call__(self, metodo, url, **kwargs):
        self.llamadas.append((metodo, url, kwargs))
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestConfig(unittest.TestCase):
    def test_config_del_proyecto_es_valida(self):
        c = cargar_config(RUTA_CONFIG)
        self.assertEqual(c.ejecucion_automatica_cada_minutos, 5)

    def _config_con(self, **cambios):
        datos = json.loads(RUTA_CONFIG.read_text(encoding="utf-8"))
        datos.update(cambios)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(datos, tmp)
        tmp.close()
        return Path(tmp.name)

    def test_rechaza_limpieza_rapida_menor_que_repaso_completo(self):
        with self.assertRaises(ErrorConfiguracion) as ctx:
            cargar_config(self._config_con(limpieza_rapida_tras_horas=24))
        self.assertIn("limpieza_rapida_tras_horas", str(ctx.exception))

    def test_rechaza_valores_no_enteros_y_parametros_que_faltan(self):
        with self.assertRaises(ErrorConfiguracion):
            cargar_config(self._config_con(horas_que_cubre_el_repaso_reciente="2"))
        datos = json.loads(RUTA_CONFIG.read_text(encoding="utf-8"))
        del datos["clips_por_pagina"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(datos, tmp)
        tmp.close()
        with self.assertRaises(ErrorConfiguracion) as ctx:
            cargar_config(Path(tmp.name))
        self.assertIn("clips_por_pagina", str(ctx.exception))

    def test_rechaza_mas_de_100_clips_por_pagina(self):
        with self.assertRaises(ErrorConfiguracion):
            cargar_config(self._config_con(clips_por_pagina=150))


class TestCredencialesDesdeEntorno(unittest.TestCase):
    def test_crea_fichero_privado_con_las_variables(self):
        entorno = {"TWITCH_ID_CLIENTE": "id", "TWITCH_SECRETO": "s", "TELEGRAM_GTA6_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "9"}
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "credenciales.json"
            guardar_credenciales_desde_entorno(ruta, entorno)
            self.assertEqual(cargar_credenciales(ruta).telegram_chat_id, "9")
            self.assertEqual(ruta.stat().st_mode & 0o777, 0o600)


class TestFranjasYTramos(unittest.TestCase):
    def test_franja_nocturna(self):
        franjas = [Franja("18:00", "02:00", "alertas")]
        self.assertEqual(modo_actual(franjas, datetime(2026, 1, 1, 1, 0)), "alertas")
        self.assertEqual(modo_actual(franjas, datetime(2026, 1, 1, 12, 0)), "silencio")

    def test_tramos_cubren_la_ventana_con_solape(self):
        t = tramos(AHORA, 24, 60, 2)
        self.assertEqual(len(t), 24)
        self.assertEqual(t[0], (iso_utc(AHORA - timedelta(minutes=62)), iso_utc(AHORA)))
        self.assertEqual(t[-1][0], iso_utc(AHORA - timedelta(hours=24)))


class TestViralidad(unittest.TestCase):
    def test_clip_nuevo_usa_su_edad(self):
        self.assertAlmostEqual(velocidad_por_hora([(1800, 300)], 1800, 0, 60, 5), 600)

    def test_usa_la_ventana_de_crecimiento(self):
        historial = [(0, 10), (3600, 100), (5400, 1000), (7200, 1900)]
        self.assertAlmostEqual(velocidad_por_hora(historial, 7200, 0, 60, 5), 1800)

    def test_nota_relativa_y_sin_datos_suficientes(self):
        referencias = sorted(range(100))
        self.assertEqual(nota_1_a_10(1000, referencias, 20), 10)
        self.assertEqual(nota_1_a_10(-1, referencias, 20), 1)
        self.assertEqual(nota_1_a_10(49, referencias, 20), 5)
        self.assertIsNone(nota_1_a_10(50, list(range(10)), 20))


class TestTwitch(unittest.TestCase):
    def test_renueva_token_si_401(self):
        http = HttpFalso([TOKEN, Respuesta(401, {}, {}), TOKEN,
                          Respuesta(200, {}, {"data": [{"login": "kaicenat", "id": "1"}]})])
        self.assertEqual(ClienteTwitch("id", "s", {}, http=http).ids_de_usuarios(["kaicenat"]), {"kaicenat": "1"})

    def test_429_lanza_error_de_limite(self):
        http = HttpFalso([TOKEN, Respuesta(429, {"ratelimit-reset": "100"}, {})])
        with self.assertRaises(ErrorLimiteTwitch):
            ClienteTwitch("id", "s", {}, http=http, reloj=lambda: 40.0).ids_de_usuarios(["kaicenat"])

    def test_paginacion_y_truncado(self):
        pagina = lambda cursor: Respuesta(200, {}, {"data": [{"id": "x"}], "pagination": {"cursor": cursor} if cursor else {}})
        tw = ClienteTwitch("id", "s", {}, http=HttpFalso([TOKEN, pagina("c1"), pagina(None)]))
        self.assertEqual(tw.clips("1", "a", "b", 3, 100)[1], False)
        tw = ClienteTwitch("id", "s", {}, http=HttpFalso([TOKEN, pagina("c1"), pagina("c2")]))
        clips, truncado = tw.clips("1", "a", "b", 2, 100)
        self.assertEqual((len(clips), truncado), (2, True))


class TestEjecucion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.almacen = Almacen(Path(self.tmp.name))
        self.bd = BaseDatos(self.almacen.ruta_base_datos)

    def tearDown(self):
        self.bd.cerrar()
        self.tmp.cleanup()

    def _ejecutar(self, twitch, ahora=AHORA, telegram=None, cfg=None):
        telegram = telegram or TelegramFalso()
        r = ejecutar_una_vez(cfg or config_base(), self.almacen, self.bd, twitch, telegram, ahora=ahora)
        return r, telegram

    def _alertas(self, **cambios):
        return config_base(franjas=[Franja("00:00", "24:00", "alertas")], **cambios)

    def test_primera_ejecucion_hace_repaso_completo_y_guarda_todo_en_silencio(self):
        tw = TwitchFalso([clip("viejo", 5000, AHORA - timedelta(hours=20)), clip("joven", 900, AHORA - timedelta(minutes=10))])
        r, tg = self._ejecutar(tw)
        self.assertEqual((r["repaso"], r["clips_nuevos"], r["clips_en_base"], tg.mensajes), ("completo", 2, 2, []))

    def test_programacion_de_repasos(self):
        tw = TwitchFalso([])
        self.assertEqual(self._ejecutar(tw)[0]["repaso"], "completo")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=2))[0]["repaso"], "ninguno")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=5))[0]["repaso"], "reciente")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=15))[0]["repaso"], "completo")

    def test_aviso_solo_para_clips_jovenes_que_superan_el_minimo_y_una_sola_vez(self):
        tw = TwitchFalso([clip("viejo", 5000, AHORA - timedelta(hours=20)),
                          clip("joven_viral", 900, AHORA - timedelta(minutes=10)),
                          clip("joven_flojo", 20, AHORA - timedelta(minutes=10))])
        r, tg = self._ejecutar(tw, cfg=self._alertas())
        avisos = [m for m in tg.mensajes if m.startswith("Clip con potencial")]
        self.assertEqual(len(avisos), 1)
        self.assertIn("joven_viral", avisos[0])
        r, tg = self._ejecutar(tw, AHORA + timedelta(minutes=15), cfg=self._alertas())
        self.assertEqual(tg.mensajes, [])

    def test_si_telegram_falla_el_aviso_se_reintenta(self):
        self._ejecutar(TwitchFalso([]))
        tw = TwitchFalso([clip("a", 900, AHORA)])
        r, _ = self._ejecutar(tw, AHORA + timedelta(minutes=5), TelegramFalso(fallar=True), self._alertas())
        self.assertEqual(r["resultado"], "error")
        r, tg = self._ejecutar(tw, AHORA + timedelta(minutes=10), cfg=self._alertas())
        self.assertEqual(len(tg.mensajes), 1)

    def test_historial_solo_cuando_cambian_las_visualizaciones(self):
        datos = [clip("a", 10, AHORA - timedelta(minutes=30))]
        tw = TwitchFalso(datos)
        self._ejecutar(tw)
        self._ejecutar(tw, AHORA + timedelta(minutes=5))
        tw.datos["a"]["view_count"] = 50
        self._ejecutar(tw, AHORA + timedelta(minutes=10))
        self.assertEqual(self.bd.totales()["filas_historial"], 2)

    def test_minimo_de_visualizaciones_para_historial(self):
        tw = TwitchFalso([clip("a", 10, AHORA - timedelta(minutes=30))])
        cfg = config_base(guardar_historial_desde_visualizaciones=100)
        self._ejecutar(tw, cfg=cfg)
        tw.datos["a"]["view_count"] = 50
        self._ejecutar(tw, AHORA + timedelta(minutes=15), cfg=cfg)
        tw.datos["a"]["view_count"] = 150
        self._ejecutar(tw, AHORA + timedelta(minutes=30), cfg=cfg)
        self.assertEqual(self.bd.totales()["filas_historial"], 2)

    def test_limpiezas_respetan_los_clips_virales(self):
        cfg = config_base(viralidad_clips_minimos_para_dar_nota=1)
        tw = TwitchFalso([clip("flojo", 3, AHORA - timedelta(hours=1)), clip("medio", 500, AHORA - timedelta(hours=1)),
                          clip("viral", 9000, AHORA - timedelta(hours=1))])
        self._ejecutar(tw, cfg=cfg)
        self.bd.con.execute("UPDATE clips SET nota_maxima = 10 WHERE clip_id = 'viral'")
        self.bd.con.execute("UPDATE clips SET nota_maxima = 3 WHERE clip_id != 'viral'")
        r = self.bd.limpiar(cfg, int((AHORA + timedelta(days=4)).timestamp()))
        self.assertEqual(r, {"clips_borrados": 1, "historiales_resumidos": 1})
        restantes = {f[0]: f[1] for f in self.bd.con.execute("SELECT clip_id, historial_resumido FROM clips")}
        self.assertEqual(restantes, {"medio": 1, "viral": 0})
        con_historial = {f[0] for f in self.bd.con.execute("SELECT DISTINCT clip_id FROM historial")}
        self.assertEqual(con_historial, {"viral"})

    def test_sin_internet_registra_error(self):
        class TwitchSinRed(TwitchFalso):
            def ids_de_usuarios(self, logins):
                raise ErrorRed("x")
        r, _ = self._ejecutar(TwitchSinRed([]))
        self.assertEqual(r["resultado"], "error")
        self.assertEqual(self.almacen.leer_ejecuciones()[0]["resultado"], "error")

    def test_fallo_de_configuracion_queda_registrado_y_avisa_una_vez(self):
        from app.ejecucion import registrar_fallo_previo
        tg = TelegramFalso()
        registrar_fallo_previo(self.almacen, "Configuracion: falta X", tg, ahora=AHORA)
        registrar_fallo_previo(self.almacen, "Configuracion: falta X", tg, ahora=AHORA + timedelta(minutes=5))
        filas = self.almacen.leer_ejecuciones()
        self.assertEqual([f["resultado"] for f in filas], ["error", "error"])
        self.assertIn("falta X", filas[0]["error"])
        self.assertEqual(len(tg.mensajes), 1)

    def test_exportar_con_hora_espanola(self):
        self._ejecutar(TwitchFalso([clip("a", 10, AHORA - timedelta(minutes=30))]))
        fichas, _ = self.bd.exportar(Path(self.tmp.name), "Europe/Madrid")
        with fichas.open(newline="", encoding="utf-8") as f:
            fila = next(csv.DictReader(f))
        self.assertEqual(fila["creado_espana"], "30/10/2026 12:30:00")

    def test_volumen_grande_marca_tramos_truncados(self):
        muchos = [clip(f"c{i}", i % 50, AHORA - timedelta(minutes=5 + i % 50)) for i in range(2500)]
        r, _ = self._ejecutar(TwitchFalso(muchos), cfg=config_base(max_paginas_de_100_clips_por_trozo=2))
        self.assertGreater(r["tramos_truncados"], 0)


if __name__ == "__main__":
    unittest.main()
