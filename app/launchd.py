import os
import plistlib
import subprocess
import sys
from pathlib import Path

ETIQUETA = "com.gta6clipper.detector"
CARPETAS_PROTEGIDAS = ("Desktop", "Documents", "Downloads")


def ruta_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{ETIQUETA}.plist"


def _dominio() -> str:
    return f"gui/{os.getuid()}"


def aviso_carpeta(proyecto: Path):
    relativa = proyecto.resolve().relative_to(Path.home()) if proyecto.resolve().is_relative_to(Path.home()) else None
    if relativa and relativa.parts and relativa.parts[0] in CARPETAS_PROTEGIDAS:
        return (f"Atencion: el proyecto esta dentro de ~/{relativa.parts[0]}. macOS puede impedir que launchd "
                f"acceda a esa carpeta. Recomendado: mover el proyecto a ~/gta6-clipper")
    return None


def instalar(proyecto: Path, intervalo_minutos: int) -> str:
    datos = proyecto / "datos"
    datos.mkdir(exist_ok=True)
    contenido = {
        "Label": ETIQUETA,
        "ProgramArguments": [sys.executable, str(proyecto / "clipper.py"), "ejecutar"],
        "WorkingDirectory": str(proyecto),
        "StartInterval": intervalo_minutos * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(datos / "launchd_salida.log"),
        "StandardErrorPath": str(datos / "launchd_errores.log"),
    }
    destino = ruta_plist()
    destino.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_dominio()}/{ETIQUETA}"], capture_output=True)
    with destino.open("wb") as f:
        plistlib.dump(contenido, f)
    r = subprocess.run(["launchctl", "bootstrap", _dominio(), str(destino)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap fallo: {r.stderr.strip() or r.stdout.strip()}")
    return str(destino)


def desinstalar() -> bool:
    subprocess.run(["launchctl", "bootout", f"{_dominio()}/{ETIQUETA}"], capture_output=True)
    if ruta_plist().exists():
        ruta_plist().unlink()
        return True
    return False


def estado() -> str:
    r = subprocess.run(["launchctl", "print", f"{_dominio()}/{ETIQUETA}"], capture_output=True, text=True)
    if r.returncode != 0:
        return "No instalado (launchd no conoce el detector)."
    lineas = [l.strip() for l in r.stdout.splitlines()]
    claves = ("state =", "run interval =", "runs =", "last exit code =", "program =")
    return "\n".join(l for l in lineas if l.startswith(claves)) or r.stdout
