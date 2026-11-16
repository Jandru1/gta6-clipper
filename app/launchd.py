import os
import plistlib
import subprocess
import sys
from pathlib import Path

ETIQUETA = "com.gta6clipper.detector"
ETIQUETA_CHAT = "com.gta6clipper.chat"
CARPETAS_PROTEGIDAS = ("Desktop", "Documents", "Downloads")


def ruta_plist(etiqueta=ETIQUETA) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{etiqueta}.plist"


def _dominio() -> str:
    return f"gui/{os.getuid()}"


def aviso_carpeta(proyecto: Path):
    relativa = proyecto.resolve().relative_to(Path.home()) if proyecto.resolve().is_relative_to(Path.home()) else None
    if relativa and relativa.parts and relativa.parts[0] in CARPETAS_PROTEGIDAS:
        return (f"Atencion: el proyecto esta dentro de ~/{relativa.parts[0]}. macOS puede impedir que launchd "
                f"acceda a esa carpeta. Recomendado: mover el proyecto a ~/gta6-clipper")
    return None


def _cargar(etiqueta, contenido) -> str:
    destino = ruta_plist(etiqueta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_dominio()}/{etiqueta}"], capture_output=True)
    with destino.open("wb") as f:
        plistlib.dump(contenido, f)
    r = subprocess.run(["launchctl", "bootstrap", _dominio(), str(destino)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap fallo: {r.stderr.strip() or r.stdout.strip()}")
    return str(destino)


def instalar(proyecto: Path, intervalo_minutos: int) -> str:
    datos = proyecto / "datos"
    datos.mkdir(exist_ok=True)
    return _cargar(ETIQUETA, {
        "Label": ETIQUETA,
        "ProgramArguments": [sys.executable, str(proyecto / "clipper.py"), "ejecutar"],
        "WorkingDirectory": str(proyecto),
        "StartInterval": intervalo_minutos * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(datos / "launchd_salida.log"),
        "StandardErrorPath": str(datos / "launchd_errores.log"),
    })


def instalar_chat(proyecto: Path) -> str:
    datos = proyecto / "datos"
    datos.mkdir(exist_ok=True)
    return _cargar(ETIQUETA_CHAT, {
        "Label": ETIQUETA_CHAT,
        "ProgramArguments": ["/usr/bin/caffeinate", "-s", sys.executable, "-u", str(proyecto / "clipper.py"),
                             "escuchar-chat"],
        "WorkingDirectory": str(proyecto),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": str(datos / "chat_salida.log"),
        "StandardErrorPath": str(datos / "chat_errores.log"),
    })


def desinstalar(etiqueta=ETIQUETA) -> bool:
    subprocess.run(["launchctl", "bootout", f"{_dominio()}/{etiqueta}"], capture_output=True)
    if ruta_plist(etiqueta).exists():
        ruta_plist(etiqueta).unlink()
        return True
    return False


def estado(etiqueta=ETIQUETA):
    r = subprocess.run(["launchctl", "print", f"{_dominio()}/{etiqueta}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    datos = {}
    for linea in r.stdout.splitlines():
        clave, _, valor = linea.strip().partition(" = ")
        if clave in ("state", "run interval", "runs", "last exit code") and clave not in datos:
            datos[clave] = valor.strip()
    argumentos = plistlib.loads(ruta_plist(etiqueta).read_bytes())["ProgramArguments"] if ruta_plist(etiqueta).exists() else []
    datos["script"] = next((a for a in argumentos if a.endswith("clipper.py")), "")
    return datos
