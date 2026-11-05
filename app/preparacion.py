import json
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

from app.herramientas import ErrorHerramienta, ejecutar

TAMANO_MAXIMO_BYTES = 49 * 1024 * 1024
DURACION_MAXIMA_PALABRA_MS = 800
DURACION_TIPICA_PALABRA_MS = 400
MARCAS_DE_MUSICA = ("[music", "(music", "♪", "[musica", "(musica")
PATRONES_DE_TITULO_DE_DIRECTO = (re.compile(r"\[\d+/\d+\]"), re.compile(r"\bclick\b", re.I), re.compile(r"\blive\b", re.I))


def _es_emoji_o_simbolo(caracter):
    categoria = unicodedata.category(caracter)
    return categoria.startswith("S") or categoria in ("Co", "Cn", "Cs", "Cf") or ord(caracter) > 0xFFFF


def limpiar(texto: str) -> str:
    texto = "".join(" " if _es_emoji_o_simbolo(c) else c for c in texto or "")
    return re.sub(r"\s+", " ", texto).strip(" -|·•:")


def _emojis(texto: str) -> int:
    return sum(1 for c in texto or "" if _es_emoji_o_simbolo(c))


def es_ingles(texto: str) -> bool:
    letras = [c for c in texto if c.isalpha()]
    return bool(letras) and sum(c.isascii() for c in letras) / len(letras) >= 0.9


def titulo_valido(titulo: str) -> bool:
    limpio = limpiar(titulo)
    return (len(limpio.split()) >= 3 and len(limpio) <= 60 and es_ingles(limpio) and _emojis(titulo) <= 2
            and not any(p.search(limpio) for p in PATRONES_DE_TITULO_DE_DIRECTO))


def censurar(texto: str, palabras) -> str:
    for palabra in palabras:
        texto = re.sub(rf"\b{re.escape(palabra)}\w*", lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1),
                       texto, flags=re.I)
    return texto


def frase_de_subtitulos(segmentos) -> str:
    for s in segmentos:
        palabras = limpiar(s["texto"]).split()
        if len(palabras) >= 3 and es_ingles(" ".join(palabras)):
            return " ".join(palabras[:8])
    return ""


def gancho(titulo, segmentos, nombre_streamer, palabras_censura) -> tuple:
    if titulo_valido(titulo):
        return censurar(limpiar(titulo), palabras_censura), "titulo"
    frase = frase_de_subtitulos(segmentos)
    if frase:
        return censurar(f'"{frase}"', palabras_censura), "subtitulos"
    return nombre_streamer, "streamer"


def _sin_gritos(texto: str) -> str:
    return texto[:1].upper() + texto[1:].lower() if texto.isupper() else texto


def hashtags_para(cuenta, juego, con_aviso):
    lista = []
    if con_aviso and cuenta["campana"].get("hashtag_obligatorio"):
        lista.append(cuenta["campana"]["hashtag_obligatorio"])
    lista += cuenta.get("hashtags", [])
    if juego and juego.get("hashtag"):
        lista.append(juego["hashtag"])
    vistos, resultado = set(), []
    for h in lista:
        h = h if h.startswith("#") else f"#{h}"
        if h.lower() not in vistos:
            vistos.add(h.lower())
            resultado.append(h)
    return resultado


def textos(cuenta, clip, juego, texto_gancho, con_aviso):
    nombre = cuenta.get("nombre_streamer") or clip["streamer"]
    frase = _sin_gritos(texto_gancho)
    etiquetas = hashtags_para(cuenta, juego, con_aviso)
    lineas = (["#Ad"] if con_aviso else []) + [frase, f"Credit: twitch.tv/{clip['streamer']}"]
    titulo = frase if frase.strip('"').lower().startswith(nombre.lower()) else f"{nombre}: {frase}"
    return {
        "youtube_titulo": titulo[:100],
        "youtube_descripcion": "\n".join(lineas + [" ".join(etiquetas[:3])])[:5000],
        "instagram_texto": "\n".join(lineas + [" ".join(etiquetas[:5])])[:2200],
    }


def cuentas_destino(clip, cuentas, juegos, lanzamiento_gta_vi: date):
    juego = juegos.get(clip.get("juego_id") or "")
    creado = datetime.fromtimestamp(clip["creado_s"], timezone.utc).date()
    if juego and juego.get("es_gta_vi") and creado < lanzamiento_gta_vi:
        return [], "bloqueado: clip de GTA VI anterior al lanzamiento (posible filtracion)"
    destino = []
    for c in cuentas:
        if c["tipo"] == "streamer" and c["streamer"] == clip["streamer"]:
            campana = c["campana"]
            dentro = campana.get("activa") and campana.get("desde", "") <= creado.isoformat() <= campana.get("hasta", "9999")
            destino.append((c, bool(dentro)))
        elif c["tipo"] == "gta" and juego and juego.get("es_gta"):
            destino.append((c, False))
    return destino, "" if destino else "ninguna cuenta recibe este clip (streamer o juego no configurados)"


def _tiempo_ass(ms: int) -> str:
    cs = max(0, ms) // 10
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _texto_ass(texto: str) -> str:
    return texto.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _color(hexadecimal: str) -> str:
    h = hexadecimal.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def documento_ass(segmentos, texto_gancho, con_aviso, tema, duracion_ms, palabras_censura) -> str:
    fuente = tema["fuente"]
    texto, resalte, borde = _color(tema["color_texto"]), _color(tema["color_resalte"]), _color(tema["color_borde"])
    lineas = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Subtitulo,{fuente},{tema['tamano_subtitulos']},{texto},{texto},{borde},&H80000000,1,0,0,0,100,100,0,0,1,5,0,2,60,60,520,1",
        f"Style: Gancho,{fuente},{tema['tamano_gancho']},{resalte},{resalte},{borde},&H80000000,1,0,0,0,100,100,0,0,1,6,0,8,60,60,330,1",
        f"Style: Aviso,{fuente},44,{texto},{texto},{borde},&H80000000,1,0,0,0,100,100,0,0,1,3,0,7,40,40,60,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 1,{_tiempo_ass(0)},{_tiempo_ass(tema['segundos_gancho'] * 1000)},Gancho,,0,0,0,,"
        f"{_texto_ass(texto_gancho.upper())}",
    ]
    if con_aviso:
        lineas.append(f"Dialogue: 2,{_tiempo_ass(0)},{_tiempo_ass(duracion_ms)},Aviso,,0,0,0,,#Ad")
    for s in segmentos:
        contenido = limpiar(censurar(s["texto"], palabras_censura))
        if contenido and not any(m in s["texto"].lower() for m in MARCAS_DE_MUSICA):
            lineas.append(f"Dialogue: 0,{_tiempo_ass(s['desde_ms'])},{_tiempo_ass(s['hasta_ms'])},Subtitulo,,0,0,0,,"
                          f"{_texto_ass(contenido)}")
    return "\n".join(lineas) + "\n"


def leer_transcripcion(ruta_json: Path) -> tuple:
    datos = json.loads(ruta_json.read_text(encoding="utf-8", errors="replace"))
    idioma = (datos.get("result") or {}).get("language", "")
    segmentos = [{"desde_ms": int(t["offsets"]["from"]), "hasta_ms": int(t["offsets"]["to"]), "texto": t["text"].strip()}
                 for t in datos.get("transcription", []) if t.get("text", "").strip()]
    return segmentos, idioma


def leer_palabras(ruta_json: Path) -> list:
    datos = json.loads(ruta_json.read_text(encoding="utf-8", errors="replace"))
    palabras = []
    for segmento in datos.get("transcription", []):
        for token in segmento.get("tokens") or []:
            texto = token.get("text", "")
            if not texto.strip() or texto.strip().startswith("[_"):
                continue
            desde, hasta = int(token["offsets"]["from"]), int(token["offsets"]["to"])
            if palabras and not texto.startswith(" "):
                palabras[-1]["texto"] += texto
                palabras[-1]["hasta_ms"] = max(palabras[-1]["hasta_ms"], hasta)
            else:
                palabras.append({"texto": texto.strip(), "desde_ms": desde, "hasta_ms": hasta})
    palabras = [p for p in palabras if not any(m in p["texto"] for m in ("[", "]", "(", ")", "♪"))]
    for p in palabras:
        if p["hasta_ms"] - p["desde_ms"] > DURACION_MAXIMA_PALABRA_MS:
            p["desde_ms"] = p["hasta_ms"] - DURACION_TIPICA_PALABRA_MS
    return palabras


def agrupar_palabras(palabras, max_palabras, max_caracteres=26, pausa_ms=600) -> list:
    grupos, actual = [], []
    for p in palabras:
        texto_nuevo = " ".join([w["texto"] for w in actual] + [p["texto"]])
        if actual and (len(actual) >= max_palabras or len(texto_nuevo) > max_caracteres
                       or p["desde_ms"] - actual[-1]["hasta_ms"] > pausa_ms):
            grupos.append(actual)
            actual = []
        actual.append(p)
    if actual:
        grupos.append(actual)
    resultado = []
    for g in grupos:
        desde, hasta = g[0]["desde_ms"], max(g[-1]["hasta_ms"], g[0]["desde_ms"] + 300)
        resultado.append({"texto": " ".join(w["texto"] for w in g), "desde_ms": desde, "hasta_ms": hasta})
    for anterior, siguiente in zip(resultado, resultado[1:]):
        anterior["hasta_ms"] = min(anterior["hasta_ms"], siguiente["desde_ms"])
    return resultado


def avisos_de_contenido(segmentos, idioma) -> list:
    avisos = []
    if any(m in s["texto"].lower() for s in segmentos for m in MARCAS_DE_MUSICA):
        avisos.append("posible musica de fondo")
    if idioma and idioma != "en":
        avisos.append(f"el audio no parece estar en ingles ({idioma})")
    if not segmentos:
        avisos.append("no se ha reconocido voz: el clip va sin subtitulos")
    return avisos


FILTRO_DISENO_1 = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                   "boxblur=luma_radius=40:luma_power=2[fondo];[b]scale=1080:-2[frente];"
                   "[fondo][frente]overlay=(W-w)/2:(H-h)/2,ass=subtitulos_{sufijo}.ass[v]")


class Preparador:
    def __init__(self, config, carpeta_videos: Path, ruta_modelo: Path, ejecutor=ejecutar):
        self.config = config
        self.carpeta_videos = carpeta_videos
        self.ruta_modelo = ruta_modelo
        self.ejecutar = ejecutor

    def descargar_y_transcribir(self, clip) -> tuple:
        carpeta = self.carpeta_videos / clip["clip_id"]
        carpeta.mkdir(parents=True, exist_ok=True)
        original = carpeta / "original.mp4"
        if not original.exists():
            self.ejecutar([self.config.ruta_yt_dlp, "--no-playlist", "--quiet", "--no-warnings", "-f",
                           "best[ext=mp4]/best", "--ffmpeg-location", self.config.ruta_ffmpeg, "-o", str(original),
                           clip["url"]], timeout=300)
        if not original.exists():
            raise ErrorHerramienta("yt-dlp no ha dejado el archivo del clip")
        self.ejecutar([self.config.ruta_ffmpeg, "-y", "-loglevel", "error", "-i", "original.mp4", "-ar", "16000",
                       "-ac", "1", "-c:a", "pcm_s16le", "audio.wav"], carpeta=carpeta, timeout=300)
        self.ejecutar([self.config.ruta_whisper, "-m", str(self.ruta_modelo), "-f", "audio.wav", "-l", "en", "-nf",
                       "-oj", "-ojf", "-of", "transcripcion", "-np"], carpeta=carpeta, timeout=600)
        ruta = carpeta / "transcripcion.json"
        segmentos, idioma = leer_transcripcion(ruta)
        palabras = leer_palabras(ruta)
        if palabras:
            segmentos_pantalla = agrupar_palabras(palabras, self.config.tema.get("palabras_por_subtitulo", 3))
        else:
            segmentos_pantalla = segmentos
        return carpeta, segmentos, idioma, segmentos_pantalla

    def renderizar(self, carpeta: Path, documento: str, sufijo: str) -> Path:
        (carpeta / f"subtitulos_{sufijo}.ass").write_text(documento, encoding="utf-8")
        salida = carpeta / f"vertical_{sufijo}.mp4"
        for calidad in (23, 28, 32):
            self.ejecutar([self.config.ruta_ffmpeg, "-y", "-loglevel", "error", "-i", "original.mp4",
                           "-filter_complex", FILTRO_DISENO_1.format(sufijo=sufijo), "-map", "[v]", "-map", "0:a?",
                           "-c:v", "libx264", "-crf", str(calidad), "-preset", "medium", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", salida.name],
                          carpeta=carpeta, timeout=900)
            if salida.stat().st_size <= TAMANO_MAXIMO_BYTES:
                return salida
        raise ErrorHerramienta("el video final ocupa mas de 49 MB incluso comprimido")

    def preparar(self, clip, cuentas_y_aviso, juegos) -> dict:
        carpeta, segmentos, idioma, segmentos_pantalla = self.descargar_y_transcribir(clip)
        palabras = self.config.palabras_a_censurar
        nombre = cuentas_y_aviso[0][0].get("nombre_streamer") or clip["streamer"]
        texto_gancho, origen = gancho(clip.get("titulo") or "", segmentos, nombre, palabras)
        duracion_ms = int((clip.get("duracion_s") or 60) * 1000) + 1000
        videos = {}
        versiones = []
        juego = juegos.get(clip.get("juego_id") or "")
        for cuenta, con_aviso in cuentas_y_aviso:
            sufijo = "con_aviso" if con_aviso else "sin_aviso"
            if sufijo not in videos:
                documento = documento_ass(segmentos_pantalla, texto_gancho, con_aviso, self.config.tema, duracion_ms,
                                          palabras)
                videos[sufijo] = self.renderizar(carpeta, documento, sufijo)
            versiones.append({"cuenta": cuenta["id"], "con_aviso": con_aviso, "video": str(videos[sufijo]),
                              "textos": textos(cuenta, clip, juego, texto_gancho, con_aviso)})
        return {"versiones": versiones, "gancho": texto_gancho, "origen_gancho": origen,
                "avisos": avisos_de_contenido(segmentos, idioma)}
