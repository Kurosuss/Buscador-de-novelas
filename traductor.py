"""Traducción chino -> español de títulos y sinopsis, con caché en la base de datos local.

Usa Google Translate a través de la librería «deep-translator» (no oficial, sin clave de API).
Cada texto se traduce una sola vez: el resultado queda guardado y se reutiliza.
"""
import re
import time
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS translations(
    site TEXT NOT NULL, id TEXT NOT NULL, field TEXT NOT NULL, lang TEXT NOT NULL,
    src_hash TEXT, text TEXT, PRIMARY KEY (site, id, field, lang));
"""
FIELDS = (("title", "title_es"), ("description", "description_es"))
TITLE_FIELDS = (("title", "title_es"),)      # lo que traduce el buscador: solo los títulos
CJK = re.compile(r"[一-鿿]")
MAX_CHUNK = 4000          # Google acepta ~5000 caracteres por petición
MAX_CONSECUTIVE_FAILS = 3
BATCH_CHARS = 3000        # tamaño máximo de un lote de títulos
BATCH_ITEMS = 40


def make_google(source="auto", target="es"):
    """Devuelve una función texto -> texto. Lanza RuntimeError si falta la librería."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError as e:
        raise RuntimeError("Falta la librería de traducción. Instálala con:  pip install deep-translator") from e
    tr = GoogleTranslator(source=source, target=target)
    return lambda text: tr.translate(text)


def _need_lib():
    try:
        import deep_translator  # noqa: F401
    except ImportError as e:
        raise RuntimeError("Falta la librería de traducción. Instálala con:  pip install deep-translator") from e


def make_mymemory(source="auto", target="es"):
    """MyMemory (gratuito, sin clave): ~5 000 caracteres al día por IP. Textos de hasta ~150 caracteres chinos."""
    _need_lib()
    from deep_translator import MyMemoryTranslator
    cache = {}

    def fn(text):
        src = "zh-CN" if CJK.search(text) else "en-GB"
        if src not in cache:
            cache[src] = MyMemoryTranslator(source=src, target="es-ES")
        out = cache[src].translate(text) or ""
        if "MYMEMORY WARNING" in out.upper() or "ALL AVAILABLE FREE TRANSLATIONS" in out.upper():
            raise RateLimited("MyMemory: se agotó la cuota gratuita de hoy (429 too many requests).")
        return out
    return fn


LINGVA_INSTANCES = ["https://lingva.ml", "https://lingva.thedaviddelta.com",
                    "https://translate.plausibility.cloud", "https://lingva.garudalinux.org"]


def lingva_instances():
    """Instancias públicas de Lingva (front-end libre de Google Translate). Puedes añadir las tuyas, una por
    línea, en  ~/.novel_grabber/lingva_instancias.txt"""
    from pathlib import Path
    extra = []
    try:
        extra = [ln.strip().rstrip("/") for ln in
                 (Path.home() / ".novel_grabber" / "lingva_instancias.txt").read_text("utf-8").splitlines()
                 if ln.strip().startswith("http")]
    except OSError:
        pass
    return extra + LINGVA_INSTANCES


def make_lingva(source="auto", target="es"):
    """Lingva Translate: gratuito y sin clave, pero depende de instancias públicas (a veces caídas):
    se prueban una tras otra."""
    import requests
    from urllib.parse import quote
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (novel-search personal use)"
    insts = lingva_instances()
    state = {"i": 0}

    def fn(text):
        errs = []
        for k in range(len(insts)):
            base = insts[(state["i"] + k) % len(insts)]
            try:
                r = sess.get(f"{base}/api/v1/{source}/{target}/{quote(text, safe='')}", timeout=25)
                if r.status_code == 429:
                    raise RateLimited(f"{base}: 429 too many requests")
                r.raise_for_status()
                out = r.json().get("translation")
                if out is None:
                    raise ValueError("respuesta sin «translation»")
                state["i"] = (state["i"] + k) % len(insts)
                return out
            except RateLimited:
                raise
            except Exception as e:  # noqa: BLE001  (instancia caída, red, etc.)
                errs.append(f"{base.split('//')[-1]}: {type(e).__name__}")
        raise RuntimeError("Ninguna instancia de Lingva respondió (" + "; ".join(errs[:4]) + ")")
    return fn


# clave -> motor. chunk/batch_*: límites por petición; batch_items=1 = un título por petición.
ENGINES = {
    "google": {"label": "Google Translate", "make": make_google, "chunk": MAX_CHUNK,
               "batch_chars": BATCH_CHARS, "batch_items": BATCH_ITEMS,
               "note": "el más completo; puede bloquear si se abusa"},
    "lingva": {"label": "Lingva (Google, sin clave)", "make": make_lingva, "chunk": 1200,
               "batch_chars": 1000, "batch_items": 15,
               "note": "gratuito; depende de servidores públicos"},
    "mymemory": {"label": "MyMemory (gratis, cuota diaria)", "make": make_mymemory, "chunk": 150,
                 "batch_chars": 150, "batch_items": 1,
                 "note": "~5 000 caracteres/día; un título por petición"},
}
DEFAULT_ENGINE = "google"


def split_chunks(text, limit=MAX_CHUNK):
    """Parte un texto largo en trozos <= limit, cortando en saltos de línea o signos de fin de frase."""
    parts, cur = [], ""
    for seg in re.split(r"(?<=[。！？!?\n])", text):
        if cur and len(cur) + len(seg) > limit:
            parts.append(cur)
            cur = ""
        while len(seg) > limit:
            parts.append(seg[:limit])
            seg = seg[limit:]
        cur += seg
    if cur:
        parts.append(cur)
    return parts


class RateLimited(Exception):
    """Google respondió «demasiadas peticiones»: hay que parar, no insistir."""


def _is_rate_limit(e):
    msg = (type(e).__name__ + " " + str(e)).lower()
    return "toomanyrequests" in msg or "too many requests" in msg or "429" in msg


def _call(fn, text, tries=4, base=5.0):
    for i in range(tries):
        try:
            return fn(text)
        except Exception as e:  # noqa: BLE001  (bloqueo temporal, red, etc.)
            if _is_rate_limit(e):
                raise RateLimited(str(e)) from e      # insistir solo alarga el bloqueo
            if i == tries - 1:
                raise
            time.sleep(base * 2 ** i)


def _wait(pause, stop=None):
    """Pausa entre peticiones; se corta enseguida si el usuario cancela."""
    if pause > 0:
        if stop is not None:
            stop.wait(pause)
        else:
            time.sleep(pause)


def translate_text(text, fn, pause=1.0, limit=MAX_CHUNK):
    """Traduce un texto de cualquier longitud (por trozos). Devuelve el texto en español."""
    out = []
    for i, chunk in enumerate(split_chunks(text, limit)):
        if not chunk.strip():
            out.append(chunk)
            continue
        if i:
            time.sleep(pause)
        out.append(_call(fn, chunk) or "")
    return "".join(out).strip()


def _hash(text):
    return format(zlib.crc32(text.encode("utf-8")), "08x")


def attach_cached(con, rows, lang="es"):
    """Rellena row['title_es'] / row['description_es'] con lo ya traducido (sin red)."""
    con.executescript(SCHEMA)
    cache = {(s, i, f): (h, t) for s, i, f, h, t in con.execute(
        "SELECT site,id,field,src_hash,text FROM translations WHERE lang=?", (lang,))}
    n = 0
    for r in rows:
        for src_f, dst_f in FIELDS:
            src = r.get(src_f) or ""
            hit = cache.get((r["site"], r["id"], src_f))
            if src and hit and hit[0] == _hash(src):
                r[dst_f] = hit[1]
                n += 1
    return n


def translate_rows(con, rows, fn, lang="es", pause=3.0, stop=None, progress=None, on_row=None,
                   log=print, fields=TITLE_FIELDS, engine=None, force=False, one_by_one=True):
    """Traduce los títulos de `rows` (con fields=FIELDS, también las sinopsis).
    Los textos de una sola línea se agrupan en lotes (muchas traducciones por petición) para no
    saturar a Google. Devuelve (traducidos, fallidos, motivo_de_parada o None)."""
    eng = engine or ENGINES[DEFAULT_ENGINE]
    chunk, b_chars, b_items = eng["chunk"], eng["batch_chars"], eng["batch_items"]
    name = eng["label"]
    if one_by_one:                               # una petición por título, sin agrupar
        b_items = 1
    con.executescript(SCHEMA)
    if force:                                    # volver a traducir con este motor, ignorando lo guardado
        for r in rows:
            for _sf, df in fields:
                r.pop(df, None)
    else:
        attach_cached(con, rows, lang)
    todo = [(r, sf, df) for sf, df in fields for r in rows
            if (r.get(sf) or "").strip() and df not in r]
    st = {"done": 0, "failed": 0, "n": 0}

    def save(item, es):
        r, sf, df = item
        con.execute("INSERT OR REPLACE INTO translations(site,id,field,lang,src_hash,text) "
                    "VALUES (?,?,?,?,?,?)", (r["site"], r["id"], sf, lang, _hash(r[sf]), es))
        r[df] = es
        st["done"] += 1
        if on_row:
            on_row(r)

    def tick(k=1):
        st["n"] += k
        if progress:
            progress(min(st["n"], len(todo)), len(todo))

    # ---- agrupar en unidades (lotes de textos cortos de una línea; el resto, de uno en uno) ----
    units, cur, cur_len = [], [], 0
    for item in todo:
        src = item[0][item[1]]
        if not re.search(r"[A-Za-z]|[一-鿿]", src):
            save(item, src)                      # sin letras (números, signos): nada que traducir
            tick()
            continue
        if b_items > 1 and "\n" not in src.strip() and len(src) <= min(400, b_chars):
            if cur and (cur_len + len(src) + 1 > b_chars or len(cur) >= b_items):
                units.append(cur)
                cur, cur_len = [], 0
            cur.append(item)
            cur_len += len(src) + 1
        else:
            units.append([item])
    if cur:
        units.append(cur)

    streak, last_err = 0, ""
    for unit in units:
        if stop is not None and stop.is_set():
            con.commit()
            return st["done"], st["failed"], "cancelado"
        try:
            srcs = [it[0][it[1]].strip() for it in unit]
            outs = None
            if len(unit) > 1:
                parts = translate_text("\n".join(srcs), fn, pause, chunk).split("\n")
                if len(parts) == len(unit):
                    outs = [p.strip() for p in parts]
                if unit is not units[-1]:
                    _wait(pause, stop)             # pausa entre lotes
            if outs is None:                     # lote desalineado o texto largo: de uno en uno
                outs = []
                for src in srcs:
                    outs.append(translate_text(src, fn, pause, chunk))
                    _wait(pause, stop)
            for it, es in zip(unit, outs):
                save(it, es)
            streak = 0
        except RateLimited:
            con.commit()
            return st["done"], st["failed"], (
                f"{name} limitó temporalmente las traducciones («demasiadas peticiones» o cuota agotada). Me "
                "detuve para no alargar el bloqueo. Lo ya traducido está guardado. Espera un rato (a veces hasta "
                "24 h), cambia de motor en la lista «Motor» o prueba desde otra red, y vuelve a pulsar Traducir.")
        except Exception as e:  # noqa: BLE001
            st["failed"] += len(unit)
            streak += 1
            last_err = f"{type(e).__name__}: {str(e).strip()[:200]}"
            log(f"  ✗ {last_err}")
            if streak >= MAX_CONSECUTIVE_FAILS:
                con.commit()
                return st["done"], st["failed"], (
                    f"{name} dejó de responder. Último error → {last_err}. "
                    "Espera 10–15 minutos o prueba otro motor y vuelve a pulsar Traducir: lo ya traducido está guardado.")
        tick(len(unit))
        con.commit()
    con.commit()
    return st["done"], st["failed"], None
