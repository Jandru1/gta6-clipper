import csv
import json
import os
from pathlib import Path

COLUMNAS_EJECUCIONES = [
    "hora_espana", "inicio_utc", "duracion_s", "resultado", "modo", "repaso", "llamadas_twitch",
    "ratelimit_restante", "streamers_en_directo", "espectadores", "tramos_consultados", "tramos_truncados",
    "clips_recibidos", "clips_nuevos", "clips_con_cambios", "avisos_enviados", "clips_borrados",
    "historiales_resumidos", "clips_en_base", "filas_historial", "error",
]


class Almacen:
    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self.ruta_estado = carpeta / "estado.json"
        self.ruta_ejecuciones = carpeta / "registro_ejecuciones.csv"
        self.ruta_base_datos = carpeta / "clips.db"
        self.ruta_bloqueo = carpeta / ".ejecucion.lock"
        self.estado = self._cargar_estado()

    def _cargar_estado(self) -> dict:
        if self.ruta_estado.exists():
            try:
                return json.loads(self.ruta_estado.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.ruta_estado.rename(self.ruta_estado.with_suffix(".corrupto.json"))
        return {}

    def guardar_estado(self):
        temporal = self.ruta_estado.with_suffix(".tmp")
        temporal.write_text(json.dumps(self.estado, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporal, self.ruta_estado)

    def registrar_ejecucion(self, fila: dict):
        ruta = self.ruta_ejecuciones
        if ruta.exists():
            with ruta.open(newline="", encoding="utf-8") as f:
                cabecera = next(csv.reader(f), [])
            if cabecera != COLUMNAS_EJECUCIONES:
                ruta.rename(ruta.with_name(f"{ruta.stem}_formato_antiguo.csv"))
        nuevo = not ruta.exists()
        with ruta.open("a", newline="", encoding="utf-8") as f:
            escritor = csv.DictWriter(f, fieldnames=COLUMNAS_EJECUCIONES, extrasaction="ignore")
            if nuevo:
                escritor.writeheader()
            escritor.writerow(fila)

    def leer_ejecuciones(self) -> list:
        if not self.ruta_ejecuciones.exists():
            return []
        with self.ruta_ejecuciones.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
