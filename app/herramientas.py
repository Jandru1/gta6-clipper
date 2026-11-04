import hashlib
import subprocess
import urllib.request
from pathlib import Path

MODELO_WHISPER = "ggml-large-v3-turbo-q5_0.bin"
URL_MODELO_WHISPER = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
SHA1_MODELO_WHISPER = "e050f7970618a659205450ad97eb95a18d69c9ee"


class ErrorHerramienta(Exception):
    pass


def ejecutar(comando, carpeta=None, timeout=600):
    try:
        r = subprocess.run([str(c) for c in comando], cwd=carpeta, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ErrorHerramienta(f"No encuentro el programa {comando[0]} (revisa la instalacion con Homebrew)")
    except subprocess.TimeoutExpired:
        raise ErrorHerramienta(f"{Path(str(comando[0])).name} tardo mas de {timeout} s y se cancelo")
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise ErrorHerramienta(f"{Path(str(comando[0])).name} fallo: {' | '.join(detalle)}")
    return r.stdout


def sha1_de(ruta: Path) -> str:
    h = hashlib.sha1()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def asegurar_modelo(carpeta_modelos: Path, abrir=urllib.request.urlopen) -> Path:
    ruta = carpeta_modelos / MODELO_WHISPER
    if ruta.exists():
        return ruta
    carpeta_modelos.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".descargando")
    with abrir(URL_MODELO_WHISPER, timeout=60) as r, temporal.open("wb") as f:
        for bloque in iter(lambda: r.read(1 << 20), b""):
            f.write(bloque)
    huella = sha1_de(temporal)
    if huella != SHA1_MODELO_WHISPER:
        temporal.unlink()
        raise ErrorHerramienta(f"El modelo descargado no coincide con la huella oficial ({huella}); se ha borrado")
    temporal.rename(ruta)
    return ruta


def comprobar(config, carpeta_modelos: Path) -> list:
    resultados = []
    for nombre, ruta, argumento in (("yt-dlp", config.ruta_yt_dlp, "--version"),
                                    ("ffmpeg-full", config.ruta_ffmpeg, "-version"),
                                    ("whisper-cli", config.ruta_whisper, "--help")):
        try:
            salida = ejecutar([ruta, argumento], timeout=60)
            resultados.append((nombre, True, salida.strip().splitlines()[0] if salida.strip() else "ok"))
        except ErrorHerramienta as e:
            resultados.append((nombre, False, str(e)))
    try:
        filtros = ejecutar([config.ruta_ffmpeg, "-hide_banner", "-filters"], timeout=60)
        tiene_ass = any(linea.split()[1:2] == ["ass"] for linea in filtros.splitlines() if linea.strip())
        resultados.append(("ffmpeg con subtitulos (filtro ass)", tiene_ass,
                           "ok" if tiene_ass else "este ffmpeg no puede poner subtitulos: instala ffmpeg-full"))
    except ErrorHerramienta as e:
        resultados.append(("ffmpeg con subtitulos (filtro ass)", False, str(e)))
    modelo = carpeta_modelos / MODELO_WHISPER
    resultados.append(("modelo de whisper", modelo.exists(),
                       "descargado" if modelo.exists() else "se descargara (547 MB) con 'preparar-herramientas'"))
    return resultados
