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
from app.momentos import agrupar, nota_por_cortes, percentil, prueba_hacia_atras
from app.twitch import ClienteTwitch, ErrorLimiteTwitch
from app.viralidad import velocidad_por_hora

RUTA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
AHORA = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TOKEN = Respuesta(200, {}, {"access_token": "t1", "expires_in": 5_000_000})


def config_base(**cambios):
    valores = {"franjas": [Franja("00:00", "24:00", "silencio")], "streamers": ["kaicenat"],
               "avisar_si_tiene_al_menos_visualizaciones": 500, "avisar_si_se_creo_hace_menos_de_minutos": 60}
    valores.update(cambios)
    return replace(cargar_config(RUTA_CONFIG), **valores)


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

    def test_nota_estricta_por_cortes(self):
        cortes = [1, 2, 4, 8, 10, 15, 15, 15, 15]
        referencias = [float(x) for x in range(100)]
        self.assertEqual(nota_por_cortes(1000, referencias, cortes, 20), 10)
        self.assertEqual(nota_por_cortes(98.5, referencias, cortes, 20), 10)
        self.assertEqual(nota_por_cortes(97.5, referencias, cortes, 20), 9)
        self.assertEqual(nota_por_cortes(93.5, referencias, cortes, 20), 8)
        self.assertEqual(nota_por_cortes(92.5, referencias, cortes, 20), 8)
        self.assertEqual(nota_por_cortes(91.5, referencias, cortes, 20), 7)
        self.assertEqual(nota_por_cortes(50, referencias, cortes, 20), 4)
        self.assertEqual(nota_por_cortes(-1, referencias, cortes, 20), 1)
        self.assertIsNone(nota_por_cortes(50, referencias[:10], cortes, 20))

    def test_percentil(self):
        self.assertEqual(percentil(5, [1, 2, 3, 4]), 100)
        self.assertEqual(percentil(2, [1, 2, 3, 4]), 25)
        self.assertIsNone(percentil(2, []))


def ficha(id_, creado_s, vistas=10, offset=None, creador="fan", streamer="kaicenat", vod="v1"):
    return {"clip_id": id_, "streamer": streamer, "creado_s": creado_s, "vod_id": vod, "vod_offset_s": offset,
            "visualizaciones": vistas, "duracion_s": 30, "creador": creador, "creador_id": "", "primera_vez_s": creado_s}


class TestMomentos(unittest.TestCase):
    def test_agrupa_por_vod_y_por_hora_de_creacion_sin_encadenar(self):
        clips = [ficha("a", 0, offset=1000), ficha("b", 10, offset=1050), ficha("c", 20, offset=1095),
                 ficha("d", 30, offset=1150), ficha("e", 5000), ficha("f", 5100), ficha("g", 5300)]
        grupos = sorted(sorted(c["clip_id"] for c in g) for g in agrupar(clips, 90, 120))
        self.assertEqual(grupos, [["a", "b"], ["c", "d"], ["e", "f"], ["g"]])

    def test_streamers_distintos_no_se_mezclan(self):
        clips = [ficha("a", 0, offset=10), ficha("b", 0, offset=10, streamer="xqc")]
        self.assertEqual(len(agrupar(clips, 90, 120)), 2)


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
        self.assertEqual((r["repaso"], r["clips_nuevos"], r["clips_en_base"]), ("completo", 2, 2))
        self.assertEqual([m for m in tg.mensajes if not m.startswith("Detector funcionando")], [])

    def test_programacion_de_repasos(self):
        tw = TwitchFalso([])
        self.assertEqual(self._ejecutar(tw)[0]["repaso"], "completo")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=2))[0]["repaso"], "ninguno")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=5))[0]["repaso"], "reciente")
        self.assertEqual(self._ejecutar(tw, AHORA + timedelta(minutes=15))[0]["repaso"], "completo")

    def _fondo(self):
        return [clip(f"fondo{i}", 20, AHORA - timedelta(hours=3, minutes=10 * i)) for i in range(21)]

    def _avisos(self, telegram):
        return [m for m in telegram.mensajes if m.startswith("Clip con potencial")]

    def test_aviso_por_momento_con_nota_alta_y_una_sola_vez(self):
        tw = TwitchFalso(self._fondo())
        _, tg = self._ejecutar(tw, cfg=self._alertas())
        self.assertTrue(any(m.startswith("Base de datos iniciada") for m in tg.mensajes))
        self.assertEqual(self._avisos(tg), [])
        momento = AHORA + timedelta(minutes=5)
        for c in (clip("viejo", 5000, AHORA - timedelta(hours=20)), clip("joven_viral", 900, momento),
                  clip("copia_viral", 300, momento + timedelta(seconds=30)),
                  clip("joven_flojo", 20, momento - timedelta(minutes=4))):
            tw.datos[c["id"]] = c
        _, tg = self._ejecutar(tw, AHORA + timedelta(minutes=15), cfg=self._alertas())
        avisos = self._avisos(tg)
        self.assertEqual(len(avisos), 1)
        self.assertIn("joven_viral", avisos[0])
        self.assertIn("2 clips", avisos[0])
        _, tg = self._ejecutar(tw, AHORA + timedelta(minutes=30), cfg=self._alertas())
        self.assertEqual(self._avisos(tg), [])
        notas = dict(self.bd.con.execute("SELECT clip_id, nota_maxima FROM clips WHERE clip_id LIKE '%viral'"))
        self.assertEqual(notas, {"joven_viral": 10, "copia_viral": 10})

    def test_lo_que_pasa_en_silencio_llega_en_un_resumen_al_despertar(self):
        tw = TwitchFalso(self._fondo())
        self._ejecutar(tw)
        momento = AHORA + timedelta(minutes=5)
        tw.datos["noche"] = clip("noche", 900, momento)
        _, tg = self._ejecutar(tw, AHORA + timedelta(minutes=15))
        self.assertEqual(self._avisos(tg), [])
        _, tg = self._ejecutar(tw, AHORA + timedelta(hours=6), cfg=self._alertas())
        resumenes = [m for m in tg.mensajes if m.startswith("Mientras estabas en silencio")]
        self.assertEqual(len(resumenes), 1)
        self.assertIn("noche", resumenes[0])
        self.assertEqual(self._avisos(tg), [])
        _, tg = self._ejecutar(tw, AHORA + timedelta(hours=6, minutes=15), cfg=self._alertas())
        self.assertEqual([m for m in tg.mensajes if not m.startswith("Detector funcionando")], [])

    def test_si_telegram_falla_el_aviso_se_reintenta(self):
        tw = TwitchFalso(self._fondo())
        self._ejecutar(tw)
        tw.datos["a"] = clip("a", 900, AHORA)
        r, _ = self._ejecutar(tw, AHORA + timedelta(minutes=5), TelegramFalso(fallar=True), self._alertas())
        self.assertEqual(r["resultado"], "error")
        _, tg = self._ejecutar(tw, AHORA + timedelta(minutes=10), cfg=self._alertas())
        self.assertEqual(len(self._avisos(tg)), 1)

    def test_migracion_borra_las_notas_antiguas_y_anade_columnas(self):
        ruta = Path(self.tmp.name) / "antigua.db"
        import sqlite3
        con = sqlite3.connect(ruta)
        con.executescript("CREATE TABLE clips (clip_id TEXT PRIMARY KEY, streamer TEXT NOT NULL, titulo TEXT, url TEXT,"
                          " creador TEXT, creado_s INTEGER NOT NULL, duracion_s REAL, vod_id TEXT, vod_offset_s INTEGER,"
                          " primera_vez_s INTEGER NOT NULL, ultima_vez_s INTEGER NOT NULL, visualizaciones INTEGER NOT NULL,"
                          " velocidad_actual REAL, velocidad_maxima REAL, nota_actual INTEGER, nota_maxima INTEGER,"
                          " hito_alcanzado_s INTEGER, avisado INTEGER NOT NULL DEFAULT 0,"
                          " historial_resumido INTEGER NOT NULL DEFAULT 0);"
                          "INSERT INTO clips (clip_id, streamer, creado_s, primera_vez_s, ultima_vez_s, visualizaciones,"
                          " nota_maxima) VALUES ('x', 'kai', 1, 1, 1, 1, 10);")
        con.commit()
        con.close()
        bd = BaseDatos(ruta)
        fila = bd.con.execute("SELECT nota_maxima, juego_id, momento_id FROM clips").fetchone()
        self.assertEqual(tuple(fila), (None, None, None))
        bd.cerrar()

    def test_guarda_juego_y_creador(self):
        c = clip("a", 10, AHORA - timedelta(minutes=30))
        c.update(game_id="1867279146", creator_id="99")
        self._ejecutar(TwitchFalso([c]))
        self.assertEqual(tuple(self.bd.con.execute("SELECT juego_id, creador_id FROM clips").fetchone()),
                         ("1867279146", "99"))

    def test_prueba_hacia_atras_detecta_el_momento_viral(self):
        cfg = config_base(viralidad_clips_minimos_para_dar_nota=5, viralidad_visualizaciones_minimas_para_comparar=1)
        base = 1_000_000
        clips, historiales = [], {}
        for i in range(30):
            inicio = base + i * 3600
            viral = i == 25
            clips.append(ficha(f"m{i}", inicio, vistas=5000 if viral else 20 + i))
            temprano = 900 if viral else 1 + (i * 37) % 11
            historiales[f"m{i}"] = [(inicio + 600, temprano), (inicio + 7200, 5000 if viral else 20 + temprano)]
        informe = prueba_hacia_atras(clips, historiales, {}, base + 40 * 86400, cfg)["kaicenat"]
        self.assertEqual(informe["momentos_evaluados"], 30)
        self.assertGreaterEqual(informe["aciertos"], 1)
        self.assertLess(informe["avisados"], informe["con_nota"] / 2)

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

    def _resumenes(self, telegram):
        return [m for m in telegram.mensajes if m.startswith("Detector funcionando")]

    def test_resumen_de_funcionamiento_llega_en_silencio_y_respeta_el_intervalo(self):
        tw = TwitchFalso([clip("a", 900, AHORA - timedelta(minutes=10)), clip("b", 50, AHORA - timedelta(hours=3))])
        _, tg = self._ejecutar(tw)
        self.assertEqual(len(self._resumenes(tg)), 1)
        self.assertIn("clip a", self._resumenes(tg)[0])
        _, tg = self._ejecutar(tw, AHORA + timedelta(hours=1))
        self.assertEqual(self._resumenes(tg), [])
        _, tg = self._ejecutar(tw, AHORA + timedelta(hours=6))
        self.assertEqual(len(self._resumenes(tg)), 1)

    def test_resumen_desactivado_con_cero(self):
        _, tg = self._ejecutar(TwitchFalso([]), cfg=config_base(resumen_de_funcionamiento_cada_horas=0))
        self.assertEqual(self._resumenes(tg), [])

    def test_exportar_con_hora_espanola(self):
        self._ejecutar(TwitchFalso([clip("a", 10, AHORA - timedelta(minutes=30))]))
        fichas, _, momentos = self.bd.exportar(Path(self.tmp.name), "Europe/Madrid")
        with momentos.open(newline="", encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)
        with fichas.open(newline="", encoding="utf-8") as f:
            fila = next(csv.DictReader(f))
        self.assertEqual(fila["creado_espana"], "30/10/2026 12:30:00")

    def test_volumen_grande_marca_tramos_truncados(self):
        muchos = [clip(f"c{i}", i % 50, AHORA - timedelta(minutes=5 + i % 50)) for i in range(2500)]
        r, _ = self._ejecutar(TwitchFalso(muchos), cfg=config_base(max_paginas_de_100_clips_por_trozo=2))
        self.assertGreater(r["tramos_truncados"], 0)


if __name__ == "__main__":
    unittest.main()
