import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.viralidad import velocidad_por_hora

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
                " primera_vez_s, ultima_vez_s, visualizaciones) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clip["id"], streamer, clip.get("title", ""), clip.get("url", ""), clip.get("creator_name", ""),
                 creado_s, clip.get("duration"), clip.get("video_id", ""), offset, ahora_s, ahora_s, vistas))
            cambio = True
        else:
            cambio = vistas != fila["visualizaciones"]
            self.con.execute(
                "UPDATE clips SET ultima_vez_s = ?, visualizaciones = ?, titulo = ?,"
                " vod_offset_s = COALESCE(vod_offset_s, ?) WHERE clip_id = ?",
                (ahora_s, vistas, clip.get("title", fila["titulo"]), offset, clip["id"]))
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

    def velocidades_de_referencia(self, streamer, desde_s) -> list:
        return sorted(r[0] for r in self.con.execute(
            "SELECT velocidad_maxima FROM clips WHERE streamer = ? AND creado_s >= ? AND velocidad_maxima IS NOT NULL",
            (streamer, desde_s)))

    def poner_nota(self, clip_id, nota):
        self.con.execute(
            "UPDATE clips SET nota_actual = ?, nota_maxima = CASE WHEN ? IS NULL THEN nota_maxima"
            " ELSE MAX(COALESCE(nota_maxima, 0), ?) END WHERE clip_id = ?", (nota, nota, nota, clip_id))

    def marcar_avisado(self, clip_ids):
        self.con.executemany("UPDATE clips SET avisado = 1 WHERE clip_id = ?", [(c,) for c in clip_ids])

    def esta_avisado(self, clip_id) -> bool:
        fila = self.con.execute("SELECT avisado FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
        return bool(fila and fila[0])

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
        self.con.commit()
        return {"clips_borrados": borrados, "historiales_resumidos": resumidos}

    def compactar(self):
        self.con.commit()
        self.con.execute("VACUUM")

    def mejores_clips(self, desde_s, cuantos) -> list:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM clips WHERE ultima_vez_s >= ? ORDER BY nota_maxima IS NULL, nota_maxima DESC,"
            " velocidad_maxima DESC LIMIT ?", (desde_s, cuantos))]

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
                               "avisado", "historial_resumido"])
            for r in self.con.execute("SELECT * FROM clips ORDER BY creado_s DESC"):
                escritor.writerow([
                    r["clip_id"], r["streamer"], r["titulo"], r["url"], r["creador"], texto_local(r["creado_s"], zona),
                    r["duracion_s"], r["vod_id"], r["vod_offset_s"], texto_local(r["primera_vez_s"], zona),
                    texto_local(r["ultima_vez_s"], zona), r["visualizaciones"],
                    "" if r["velocidad_actual"] is None else round(r["velocidad_actual"], 1),
                    "" if r["velocidad_maxima"] is None else round(r["velocidad_maxima"], 1),
                    r["nota_actual"], r["nota_maxima"], texto_local(r["hito_alcanzado_s"], zona),
                    "si" if r["avisado"] else "no", "si" if r["historial_resumido"] else "no"])
        historial = carpeta / "export_historial.csv"
        with historial.open("w", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            escritor.writerow(["clip_id", "streamer", "titulo", "hora_espana", "visualizaciones"])
            for r in self.con.execute(
                    "SELECT h.clip_id, c.streamer, c.titulo, h.instante_s, h.visualizaciones FROM historial h"
                    " JOIN clips c USING (clip_id) ORDER BY h.clip_id, h.instante_s"):
                escritor.writerow([r[0], r[1], r[2], texto_local(r[3], zona), r[4]])
        return [fichas, historial]
