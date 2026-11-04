import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class ErrorRed(Exception):
    pass


@dataclass
class Respuesta:
    estado: int
    cabeceras: dict = field(default_factory=dict)
    cuerpo: object = None


def peticion(metodo, url, cabeceras=None, params=None, json_cuerpo=None, formulario=None, datos=None,
             tipo_datos=None, timeout=20) -> Respuesta:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    cabeceras = dict(cabeceras or {})
    if json_cuerpo is not None:
        datos = json.dumps(json_cuerpo).encode("utf-8")
        cabeceras["Content-Type"] = "application/json; charset=UTF-8"
    elif formulario is not None:
        datos = urllib.parse.urlencode(formulario).encode("utf-8")
        cabeceras["Content-Type"] = "application/x-www-form-urlencoded"
    elif datos is not None and tipo_datos:
        cabeceras["Content-Type"] = tipo_datos
    req = urllib.request.Request(url, data=datos, headers=cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Respuesta(r.status, {k.lower(): v for k, v in r.headers.items()}, _parsear(r.read()))
    except urllib.error.HTTPError as e:
        return Respuesta(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, _parsear(e.read()))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorRed(f"Sin conexion o error de red: {getattr(e, 'reason', e)}")


def _parsear(contenido: bytes):
    texto = contenido.decode("utf-8", errors="replace")
    if not texto:
        return {}
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return texto


def multipart(campos: dict, archivos: dict) -> tuple:
    frontera = "----gta6clipper7f3a9b"
    partes = []
    for nombre, valor in campos.items():
        partes.append(f'--{frontera}\r\nContent-Disposition: form-data; name="{nombre}"\r\n\r\n{valor}\r\n'.encode("utf-8"))
    for nombre, (nombre_fichero, contenido, tipo) in archivos.items():
        partes.append(f'--{frontera}\r\nContent-Disposition: form-data; name="{nombre}"; filename="{nombre_fichero}"\r\n'
                      f'Content-Type: {tipo}\r\n\r\n'.encode("utf-8") + contenido + b"\r\n")
    partes.append(f"--{frontera}--\r\n".encode("utf-8"))
    return b"".join(partes), f"multipart/form-data; boundary={frontera}"
