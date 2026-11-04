import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.viralidad import velocidad_por_hora

VERSION_ESQUEMA = 2
COLUMNAS_NUEVAS_CLIPS = {"creador_id": "TEXT", "juego_id": "TEXT", "momento_id": "TEXT", "puntuacion_maxima": "REAL"}
CAMPOS_MOMENTO = ("momento_id", "streamer", "inicio_s", "clips", "representante", "vistas", "vistas_mejor_clip",
                  "consenso", "velocidad_temprana", "velocidad_actual", "velocidad_maxima", "tamano_relativo",
                  "puntuacion", "puntuacion_maxima", "nota_actual", "nota_maxima", "desglose", "actualizado_s",
                  "avisado")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clips (
    clip_id TEXT PRIMARY KEY,
    streamer TEXT NOT NULL,
    titulo TEXT,
    url TEXT,
    creador TEXT,
    creado_s INTEGER NOT NULL,
    duracion_s REAL,
    vod_id TEXT,
    vod_offset_s INTEGER,
    primera_vez_s INTEGER NOT NULL,
    ultima_vez_s INTEGER NOT NULL,
    visualizaciones INTEGER NOT NULL,
    velocidad_actual REAL,
    velocidad_maxima REAL,
    nota_actual INTEGER,
    nota_maxima INTEGER,
    hito_alcanzado_s INTEGER,
    avisado INTEGER NOT NULL DEFAULT 0,
    historial_resumido INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS clips_por_streamer ON clips (streamer, creado_s);
CREATE TABLE IF NOT EXISTS historial (
    clip_id TEXT NOT NULL,
    instante_s INTEGER NOT NULL,
    visualizaciones INTEGER NOT NULL,
    PRIMARY KEY (clip_id, instante_s)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS directos (
    streamer TEXT NOT NULL,
    instante_s INTEGER NOT NULL,
    espectadores INTEGER NOT NULL,
    juego TEXT,
    PRIMARY KEY (streamer, instante_s)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS momentos (
    momento_id TEXT PRIMARY KEY,
    streamer TEXT NOT NULL,
    inicio_s INTEGER NOT NULL,
    clips INTEGER NOT NULL,
    representante TEXT NOT NULL,
    vistas INTEGER NOT NULL,
    vistas_mejor_clip INTEGER NOT NULL,
    consenso INTEGER,
    velocidad_temprana REAL,
    velocidad_actual REAL,
    velocidad_maxima REAL,
    tamano_relativo REAL,
    puntuacion REAL,
    puntuacion_maxima REAL,
    nota_actual INTEGER,
    nota_maxima INTEGER,
    desglose TEXT,
    actualizado_s INTEGER NOT NULL,
    avisado INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS momentos_por_streamer ON momentos (streamer, inicio_s);
CREATE TABLE IF NOT EXISTS cola_preparacion (
    clip_id TEXT PRIMARY KEY,
    momento_id TEXT,
    nota INTEGER,
    encolado_s INTEGER NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente',
    detalle TEXT
);
CREATE TABLE IF NOT EXISTS versiones (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id TEXT NOT NULL,
    momento_id TEXT,
    cuenta TEXT NOT NULL,
    con_aviso INTEGER NOT NULL,
    video TEXT,
    textos TEXT,
    gancho TEXT,
    origen_gancho TEXT,
    avisos TEXT,
    nota INTEGER,
    youtube_id TEXT,
    estado TEXT NOT NULL,
    detalle TEXT,
    mensaje_id INTEGER,
    creado_s INTEGER NOT NULL,
    publicado_s INTEGER,
    vistas_1h INTEGER,
    vistas_24h INTEGER,
    vistas_7d INTEGER
);
CREATE TABLE IF NOT EXISTS medicion_manual (
    semana TEXT NOT NULL,
    red TEXT NOT NULL,
    cuenta TEXT NOT NULL,
    visualizaciones INTEGER,
    porcentaje_eeuu INTEGER,
    no_recomendable TEXT,
    recibido_s INTEGER NOT NULL,
    PRIMARY KEY (semana, red, cuenta)
);
"""


def segundos(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def texto_local(instante_s, zona: str) -> str:
    if instante_s is None:
        return ""
    return datetime.fromtimestamp(instante_s, timezone.utc).astimezone(ZoneInfo(zona)).strftime("%d/%m/%Y %H:%M:%S")


class BaseDatos:
    def __init__(self, ruta: Path):
        self.ruta = ruta
        self.con = sqlite3.connect(ruta)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)
        self._migrar()

    def _migrar(self):
        if self.con.execute("PRAGMA user_version").fetchone()[0] >= VERSION_ESQUEMA:
            return
        existentes = {r["name"] for r in self.con.execute("PRAGMA table_info(clips)")}
        for columna, tipo in COLUMNAS_NUEVAS_CLIPS.items():
            if columna not in existentes:
                self.con.execute(f"ALTER TABLE clips ADD COLUMN {columna} {tipo}")
        self.con.execute("UPDATE clips SET nota_actual = NULL, nota_maxima = NULL")
        self.con.execute(f"PRAGMA user_version = {VERSION_ESQUEMA}")
        self.con.commit()

    def cerrar(self):
        self.con.commit()
        self.con.close()

    def guardar(self):
        self.con.commit()

    def observar_clip(self, streamer, clip, ahora_s, config) -> dict:
        creado_s = segundos(clip["created_at"])
        vistas = int(clip.get("view_count", 0))
        offset = clip.get("vod_offset")
        fila = self.con.execute("SELECT * FROM clips WHERE clip_id = ?", (clip["id"],)).fetchone()
        nuevo = fila is None
        if nuevo:
            self.con.execute(
                "INSERT INTO clips (clip_id, streamer, titulo, url, creador, creado_s, duracion_s, vod_id, vod_offset_s,"
                " primera_vez_s, ultima_vez_s, visualizaciones, creador_id, juego_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clip["id"], streamer, clip.get("title", ""), clip.get("url", ""), clip.get("creator_name", ""),
                 creado_s, clip.get("duration"), clip.get("video_id", ""), offset, ahora_s, ahora_s, vistas,
                 clip.get("creator_id", ""), clip.get("game_id", "")))
            cambio = True
        else:
            cambio = vistas != fila["visualizaciones"]
            self.con.execute(
                "UPDATE clips SET ultima_vez_s = ?, visualizaciones = ?, titulo = ?,"
                " vod_offset_s = COALESCE(vod_offset_s, ?), creador_id = COALESCE(NULLIF(creador_id, ''), ?),"
                " juego_id = COALESCE(NULLIF(juego_id, ''), ?) WHERE clip_id = ?",
                (ahora_s, vistas, clip.get("title", fila["titulo"]), offset, clip.get("creator_id", ""),
                 clip.get("game_id", ""), clip["id"]))
        resultado = {"nuevo": nuevo, "cambio": cambio, "clip_id": clip["id"], "streamer": streamer,
                     "creado_s": creado_s, "visualizaciones": vistas, "clip": clip, "velocidad": None}
        if not cambio or (not nuevo and vistas < config.guardar_historial_desde_visualizaciones):
            return resultado
        self.con.execute("INSERT OR REPLACE INTO historial VALUES (?, ?, ?)", (clip["id"], ahora_s, vistas))
        historial = [(r[0], r[1]) for r in self.con.execute(
            "SELECT instante_s, visualizaciones FROM historial WHERE clip_id = ? ORDER BY instante_s", (clip["id"],))]
        velocidad = velocidad_por_hora(historial, ahora_s, creado_s, config.viralidad_minutos_para_medir_crecimiento,
                                       config.viralidad_minutos_minimos_de_calculo)
        self.con.execute(
            "UPDATE clips SET velocidad_actual = ?, velocidad_maxima = MAX(COALESCE(velocidad_maxima, 0), ?),"
            " hito_alcanzado_s = CASE WHEN hito_alcanzado_s IS NULL AND ? >= ? THEN ? ELSE hito_alcanzado_s END"
            " WHERE clip_id = ?",
            (velocidad, velocidad, vistas, config.apuntar_cuando_llega_a_visualizaciones, ahora_s, clip["id"]))
        resultado["velocidad"] = velocidad
        return resultado

    def clips_desde(self, desde_s, hasta_s=None) -> list:
        consulta = "SELECT * FROM clips WHERE creado_s >= ?" + (" AND creado_s <= ?" if hasta_s is not None else "")
        return [dict(r) for r in self.con.execute(consulta, (desde_s,) if hasta_s is None else (desde_s, hasta_s))]

    def historiales_desde(self, desde_s) -> dict:
        resultado = defaultdict(list)
        for r in self.con.execute(
                "SELECT h.clip_id, h.instante_s, h.visualizaciones FROM historial h JOIN clips c USING (clip_id)"
                " WHERE c.creado_s >= ? ORDER BY h.clip_id, h.instante_s", (desde_s,)):
            resultado[r[0]].append((r[1], r[2]))
        return resultado

    def directos_desde(self, desde_s) -> dict:
        resultado = defaultdict(list)
        for r in self.con.execute("SELECT streamer, instante_s, espectadores FROM directos WHERE instante_s >= ?"
                                  " ORDER BY instante_s", (desde_s,)):
            resultado[r[0]].append((r[1], r[2]))
        return resultado

    def momentos_de_referencia(self, desde_s, hasta_s, minimo_vistas) -> dict:
        resultado = defaultdict(list)
        for r in self.con.execute(
                "SELECT * FROM momentos WHERE inicio_s >= ? AND inicio_s < ? AND vistas_mejor_clip >= ?",
                (desde_s, hasta_s, minimo_vistas)):
            m = dict(r)
            m["velocidad_actual"] = m["velocidad_maxima"]
            resultado[m["streamer"]].append(m)
        return resultado

    def guardar_momentos(self, momentos, desde_ventana_s, ahora_s):
        previos = {}
        for r in self.con.execute("SELECT clip_id, avisado, nota_maxima, puntuacion_maxima, velocidad_maxima FROM clips"
                                  " WHERE creado_s >= ?", (desde_ventana_s,)):
            previos[r[0]] = r
        self.con.execute("DELETE FROM momentos WHERE inicio_s >= ?", (desde_ventana_s,))
        for m in momentos:
            miembros = [previos[c] for c in m["clip_ids"] if c in previos]
            m["avisado"] = int(any(p["avisado"] for p in miembros))
            m["nota_maxima"] = max([p["nota_maxima"] for p in miembros if p["nota_maxima"] is not None]
                                   + ([m["nota"]] if m["nota"] is not None else []), default=None)
            m["puntuacion_maxima"] = max([p["puntuacion_maxima"] or 0 for p in miembros] + [m["puntuacion"]])
            m["velocidad_maxima"] = max([p["velocidad_maxima"] or 0 for p in miembros] + [m["velocidad_actual"]])
            fila = {**m, "nota_actual": m["nota"], "actualizado_s": ahora_s,
                    "desglose": json.dumps(m.get("desglose", {}), ensure_ascii=False)}
            self.con.execute(f"INSERT OR REPLACE INTO momentos ({', '.join(CAMPOS_MOMENTO)}) VALUES "
                             f"({', '.join('?' * len(CAMPOS_MOMENTO))})", [fila[c] for c in CAMPOS_MOMENTO])
            self.con.executemany(
                "UPDATE clips SET momento_id = ?, nota_actual = ?, nota_maxima = ?, puntuacion_maxima = ? WHERE clip_id = ?",
                [(m["momento_id"], m["nota"], m["nota_maxima"], m["puntuacion_maxima"], c) for c in m["clip_ids"]])

    def marcar_momento_avisado(self, momento):
        self.con.executemany("UPDATE clips SET avisado = 1 WHERE clip_id = ?", [(c,) for c in momento["clip_ids"]])
        self.con.execute("UPDATE momentos SET avisado = 1 WHERE momento_id = ?", (momento["momento_id"],))
        momento["avisado"] = 1

    def momentos_pendientes(self, desde_s, nota_minima, vistas_minimas, cuantos) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT m.*, c.titulo, c.url, c.visualizaciones AS vistas_representante FROM momentos m"
            " JOIN clips c ON c.clip_id = m.representante WHERE m.inicio_s >= ? AND m.nota_maxima >= ?"
            " AND m.vistas >= ? AND m.avisado = 0 ORDER BY m.nota_maxima DESC, m.puntuacion_maxima DESC LIMIT ?",
            (desde_s, nota_minima, vistas_minimas, cuantos))]

    def marcar_momentos_avisados_por_id(self, momento_ids):
        for momento_id in momento_ids:
            self.con.execute("UPDATE clips SET avisado = 1 WHERE momento_id = ?", (momento_id,))
            self.con.execute("UPDATE momentos SET avisado = 1 WHERE momento_id = ?", (momento_id,))

    def encolar(self, clip_id, momento_id, nota, ahora_s):
        self.con.execute("INSERT OR IGNORE INTO cola_preparacion (clip_id, momento_id, nota, encolado_s) VALUES (?, ?, ?, ?)",
                         (clip_id, momento_id, nota, ahora_s))

    def pendientes_de_preparar(self, cuantos) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM cola_preparacion WHERE estado = 'pendiente' ORDER BY encolado_s LIMIT ?", (cuantos,))]

    def marcar_cola(self, clip_id, estado, detalle=""):
        self.con.execute("UPDATE cola_preparacion SET estado = ?, detalle = ? WHERE clip_id = ?", (estado, detalle, clip_id))

    def nueva_version(self, datos: dict) -> int:
        columnas = ", ".join(datos)
        return self.con.execute(f"INSERT INTO versiones ({columnas}) VALUES ({', '.join('?' * len(datos))})",
                                list(datos.values())).lastrowid

    def actualizar_version(self, version_id, **cambios):
        asignaciones = ", ".join(f"{c} = ?" for c in cambios)
        self.con.execute(f"UPDATE versiones SET {asignaciones} WHERE version_id = ?", [*cambios.values(), version_id])

    def version(self, version_id) -> dict:
        fila = self.con.execute("SELECT * FROM versiones WHERE version_id = ?", (version_id,)).fetchone()
        return dict(fila) if fila else {}

    def versiones_para_medir(self, ahora_s) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM versiones WHERE estado = 'publicado' AND youtube_id IS NOT NULL AND ("
            " (vistas_1h IS NULL AND publicado_s <= ?) OR (vistas_24h IS NULL AND publicado_s <= ?)"
            " OR (vistas_7d IS NULL AND publicado_s <= ?))", (ahora_s - 3600, ahora_s - 86400, ahora_s - 7 * 86400))]

    def versiones_publicadas_desde(self, desde_s) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM versiones WHERE estado = 'publicado' AND publicado_s >= ?", (desde_s,))]

    def videos_antiguos(self, antes_s) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT version_id, video FROM versiones WHERE creado_s < ? AND video IS NOT NULL AND video != ''", (antes_s,))]

    def guardar_medicion_manual(self, semana, red, cuenta, visualizaciones, porcentaje, no_recomendable, ahora_s):
        self.con.execute("INSERT OR REPLACE INTO medicion_manual VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (semana, red, cuenta, visualizaciones, porcentaje, no_recomendable, ahora_s))

    def clip(self, clip_id) -> dict:
        fila = self.con.execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
        return dict(fila) if fila else {}

    def guardar_directo(self, streamer, ahora_s, espectadores, juego):
        self.con.execute("INSERT OR REPLACE INTO directos VALUES (?, ?, ?, ?)", (streamer, ahora_s, espectadores, juego))

    def limpiar(self, config, ahora_s) -> dict:
        no_viral = "(nota_maxima IS NULL OR nota_maxima < ?)"
        limite_rapida = ahora_s - config.limpieza_rapida_tras_horas * 3600
        condicion = f"creado_s < ? AND visualizaciones < ? AND {no_viral}"
        parametros = (limite_rapida, config.limpieza_rapida_si_tiene_menos_de_visualizaciones, config.no_limpiar_clips_con_nota_igual_o_mayor_que)
        self.con.execute(f"DELETE FROM historial WHERE clip_id IN (SELECT clip_id FROM clips WHERE {condicion})", parametros)
        borrados = self.con.execute(f"DELETE FROM clips WHERE {condicion}", parametros).rowcount

        limite_no_virales = ahora_s - config.limpieza_no_virales_tras_dias * 86400
        condicion = f"creado_s < ? AND historial_resumido = 0 AND {no_viral}"
        parametros = (limite_no_virales, config.no_limpiar_clips_con_nota_igual_o_mayor_que)
        self.con.execute(f"DELETE FROM historial WHERE clip_id IN (SELECT clip_id FROM clips WHERE {condicion})", parametros)
        resumidos = self.con.execute(f"UPDATE clips SET historial_resumido = 1 WHERE {condicion}", parametros).rowcount

        self.con.execute("DELETE FROM directos WHERE instante_s < ?", (limite_no_virales,))
        self.con.execute("DELETE FROM momentos WHERE inicio_s < ? AND (nota_maxima IS NULL OR nota_maxima < ?)",
                         (limite_no_virales, config.no_limpiar_clips_con_nota_igual_o_mayor_que))
        self.con.commit()
        return {"clips_borrados": borrados, "historiales_resumidos": resumidos}

    def compactar(self):
        self.con.commit()
        self.con.execute("VACUUM")

    def mejores_momentos(self, desde_s, cuantos) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT m.*, c.titulo, c.url, c.visualizaciones AS vistas_representante FROM momentos m"
            " JOIN clips c ON c.clip_id = m.representante WHERE m.actualizado_s >= ?"
            " ORDER BY m.nota_maxima IS NULL, m.nota_maxima DESC, m.puntuacion_maxima DESC LIMIT ?", (desde_s, cuantos))]

    def mas_vistos(self, desde_s, cuantos) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM clips WHERE ultima_vez_s >= ? ORDER BY visualizaciones DESC LIMIT ?", (desde_s, cuantos))]

    def clips_nuevos_desde(self, desde_s) -> int:
        return self.con.execute("SELECT COUNT(*) FROM clips WHERE primera_vez_s >= ?", (desde_s,)).fetchone()[0]

    def resumen_por_streamer(self) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT streamer, COUNT(*) AS clips, MAX(primera_vez_s) AS ultimo_guardado_s, MAX(visualizaciones) AS max_vis,"
            " SUM(nota_maxima IS NOT NULL) AS con_nota FROM clips GROUP BY streamer ORDER BY clips DESC")]

    def totales(self) -> dict:
        return {
            "clips_en_base": self.con.execute("SELECT COUNT(*) FROM clips").fetchone()[0],
            "filas_historial": self.con.execute("SELECT COUNT(*) FROM historial").fetchone()[0],
        }

    def exportar(self, carpeta: Path, zona: str) -> list:
        fichas = carpeta / "export_fichas.csv"
        with fichas.open("w", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            escritor.writerow(["clip_id", "streamer", "titulo", "url", "creador", "creado_espana", "duracion_s",
                               "vod_id", "vod_offset_s", "visto_por_primera_vez_espana", "ultima_consulta_espana",
                               "visualizaciones", "velocidad_actual_por_hora", "velocidad_maxima_por_hora",
                               "nota_viralidad_actual", "nota_viralidad_maxima", "hito_alcanzado_espana",
                               "avisado", "historial_resumido", "creador_id", "juego_id", "momento_id"])
            for r in self.con.execute("SELECT * FROM clips ORDER BY creado_s DESC"):
                escritor.writerow([
                    r["clip_id"], r["streamer"], r["titulo"], r["url"], r["creador"], texto_local(r["creado_s"], zona),
                    r["duracion_s"], r["vod_id"], r["vod_offset_s"], texto_local(r["primera_vez_s"], zona),
                    texto_local(r["ultima_vez_s"], zona), r["visualizaciones"],
                    "" if r["velocidad_actual"] is None else round(r["velocidad_actual"], 1),
                    "" if r["velocidad_maxima"] is None else round(r["velocidad_maxima"], 1),
                    r["nota_actual"], r["nota_maxima"], texto_local(r["hito_alcanzado_s"], zona),
                    "si" if r["avisado"] else "no", "si" if r["historial_resumido"] else "no",
                    r["creador_id"], r["juego_id"], r["momento_id"]])
        momentos = carpeta / "export_momentos.csv"
        with momentos.open("w", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            escritor.writerow(["momento_id", "streamer", "inicio_espana", "clips", "clip_representante", "vistas_totales",
                               "vistas_mejor_clip", "creadores_distintos", "vistas_por_minuto_tempranas",
                               "velocidad_actual_por_hora", "velocidad_maxima_por_hora", "vistas_por_1000_espectadores",
                               "puntuacion_actual", "puntuacion_maxima", "nota_actual", "nota_maxima",
                               "desglose_percentiles", "actualizado_espana", "avisado"])
            redondear = lambda v: "" if v is None else round(v, 1)
            for r in self.con.execute("SELECT * FROM momentos ORDER BY inicio_s DESC"):
                escritor.writerow([
                    r["momento_id"], r["streamer"], texto_local(r["inicio_s"], zona), r["clips"], r["representante"],
                    r["vistas"], r["vistas_mejor_clip"], r["consenso"], redondear(r["velocidad_temprana"]),
                    redondear(r["velocidad_actual"]), redondear(r["velocidad_maxima"]), redondear(r["tamano_relativo"]),
                    redondear(r["puntuacion"]), redondear(r["puntuacion_maxima"]), r["nota_actual"], r["nota_maxima"],
                    r["desglose"], texto_local(r["actualizado_s"], zona), "si" if r["avisado"] else "no"])
        historial = carpeta / "export_historial.csv"
        with historial.open("w", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            escritor.writerow(["clip_id", "streamer", "titulo", "hora_espana", "visualizaciones"])
            for r in self.con.execute(
                    "SELECT h.clip_id, c.streamer, c.titulo, h.instante_s, h.visualizaciones FROM historial h"
                    " JOIN clips c USING (clip_id) ORDER BY h.clip_id, h.instante_s"):
                escritor.writerow([r[0], r[1], r[2], texto_local(r[3], zona), r[4]])
        return [fichas, historial, momentos]
