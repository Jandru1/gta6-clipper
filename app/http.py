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


def peticion(metodo, url, cabeceras=None, params=None, json_cuerpo=None, timeout=20) -> Respuesta:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    datos = None
    cabeceras = dict(cabeceras or {})
    if json_cuerpo is not None:
        datos = json.dumps(json_cuerpo).encode("utf-8")
        cabeceras["Content-Type"] = "application/json"
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
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return texto
