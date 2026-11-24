#!/usr/bin/env python3
"""
Buscador de novelas — búsqueda avanzada sobre un catálogo local.

El programa NO se conecta a ningún sitio para descargar el catálogo ni las novelas. Tú abres las páginas
en tu navegador, las guardas (Ctrl+S, en su idioma original) y las importas aquí; el catálogo se guarda
solo en tu equipo (SQLite). Después puedes buscar con sinónimos, exclusiones, frases exactas, filtros,
ordenar columnas y traducir los títulos al español.

Ejemplos:
    python novel_search.py                                   (abre la ventana)
    python novel_search.py importar C:\\paginas_guardadas
    python novel_search.py buscar "cultivación sistema -harén" --estado finalizadas
    python novel_search.py buscar "AI" --palabra-completa    (con MAYÚSCULAS se busca exactamente así)
    python novel_search.py exportar -o catalogo.json         (para el userscript del navegador)
    python novel_search.py estado
"""
import argparse
import html
import json
import math
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import webbrowser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from glosario import GLOSARIO
import traductor

# Sitios cuyo catálogo se puede importar desde páginas guardadas con el navegador.
SITES = {
    "suduguu": {"name": "suduguu.com", "base": "https://www.suduguu.com", "hosts": {"www.suduguu.com", "suduguu.com"}},
    "washai": {"name": "book.washai.net", "base": "https://book.washai.net", "hosts": {"book.washai.net"}},
    "ixdzs": {"name": "ixdzs8.com", "base": "https://ixdzs8.com", "hosts": {"ixdzs8.com", "www.ixdzs8.com"}},
    "fictionzone": {"name": "fictionzone.net", "base": "https://fictionzone.net", "hosts": {"fictionzone.net", "www.fictionzone.net"}},
}
SEARCH_SITES = tuple(SITES)
# ixdzs8.com: categorías /sort/ID/ (se usan para etiquetar las páginas importadas)
IXDZS_SORTS = {1: "玄幻", 2: "仙侠", 3: "都市", 4: "历史", 5: "游戏", 6: "科幻",
               7: "言情", 8: "耽美", 9: "古言", 10: "武侠", 12: "悬疑", 18: "同人"}
DATA_DIR = Path.home() / ".novel_grabber"
DB_PATH = DATA_DIR / "catalogo.db"
USER_GLOSSARY = DATA_DIR / "glosario_usuario.json"
CJK_RX = re.compile(r"[\u4e00-\u9fff]")
DESC_PENDING, DESC_FULL, DESC_SNIPPET, DESC_NONE = 0, 1, 2, 3   # estado de la sinopsis
HEADINGS = ("h1", "h2", "h3", "h4")
STATUS_RX = re.compile(r"(连载中|已完结|已完本|完结|连载)")
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")
DATE_FIND = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS novels(
    site TEXT NOT NULL, id TEXT NOT NULL, title TEXT, author TEXT, category TEXT, status TEXT,
    words REAL, description TEXT DEFAULT '', desc_state INTEGER DEFAULT 0, url TEXT,
    updated TEXT, seen_at TEXT, PRIMARY KEY (site, id));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# =============================================================================== base de datos
def open_db(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA + traductor.SCHEMA)
    return con


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def upsert_novel(con, site, rec):
    """Inserta o actualiza una novela. Devuelve True si es nueva."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    row = con.execute("SELECT desc_state FROM novels WHERE site=? AND id=?", (site, rec["id"])).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO novels(site,id,title,author,category,status,words,description,desc_state,"
            "url,updated,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (site, rec["id"], rec.get("title"), rec.get("author"), rec.get("category"),
             rec.get("status"), rec.get("words"), rec.get("description") or "",
             rec.get("desc_state", DESC_PENDING), rec["url"], rec.get("updated"), now))
        return True
    con.execute(
        "UPDATE novels SET title=COALESCE(NULLIF(?,''),title), author=COALESCE(NULLIF(?,''),author), "
        "category=COALESCE(NULLIF(?,''),category), status=COALESCE(NULLIF(?,''),status), "
        "words=COALESCE(?,words), url=?, updated=COALESCE(?,updated), seen_at=? WHERE site=? AND id=?",
        (rec.get("title"), rec.get("author"), rec.get("category"), rec.get("status"),
         rec.get("words"), rec["url"], rec.get("updated"), now, site, rec["id"]))
    new = rec.get("desc_state")
    if new is not None and not (row["desc_state"] == DESC_FULL and new != DESC_FULL):
        con.execute("UPDATE novels SET description=?, desc_state=? WHERE site=? AND id=?",
                    (rec.get("description") or "", new, site, rec["id"]))
    return False


def load_rows(con, sites=None):
    q, params = "SELECT * FROM novels", []
    if sites:
        q += " WHERE site IN (%s)" % ",".join("?" * len(sites))
        params = list(sites)
    return [dict(r) for r in con.execute(q, params)]


def catalog_stats(con):
    out = {}
    for site in SEARCH_SITES:
        r = con.execute(
            "SELECT COUNT(*) n, SUM(desc_state=?) full_, SUM(desc_state=?) snip, SUM(desc_state=?) pend "
            "FROM novels WHERE site=?", (DESC_FULL, DESC_SNIPPET, DESC_PENDING, site)).fetchone()
        out[site] = {"total": r["n"] or 0, "completas": r["full_"] or 0,
                     "fragmentos": r["snip"] or 0, "pendientes": r["pend"] or 0}
    return out


def format_stats(stats):
    lines = []
    for site, s in stats.items():
        lines.append(f"{site_name(site)}: {s['total']} novelas | sinopsis completas: {s['completas']} | "
                     f"solo fragmento: {s['fragmentos']} | sin sinopsis: {s['pendientes']}")
    return "\n".join(lines)


# =============================================================================== lectura de páginas
def following_texts(tag, stop=HEADINGS, max_chars=700):
    """Textos que siguen a `tag` hasta el siguiente encabezado (la 'tarjeta' de una novela)."""
    out, total = [], 0
    for el in tag.next_elements:
        if isinstance(el, Tag):
            if el.name in stop and el is not tag and tag not in el.parents:
                break
            continue
        if type(el) is NavigableString and tag not in el.parents:
            t = el.strip()
            if t:
                out.append(t)
                total += len(t)
                if total > max_chars:
                    break
    return out


def site_name(site):
    return SITES[site]["name"]


def parse_words(text):
    """'127.9万字' -> 127.9 (en 万 de caracteres)."""
    m = re.search(r"([\d.]+)\s*(万)?\s*字", text or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if m.group(2) else v / 10000


def _resolve_nuxt(data):
    """Decodifica el formato compacto de Nuxt (__NUXT_DATA__): lista donde los valores son índices."""
    cache = {}
    wrappers = ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef")

    def res(i, depth=0):
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(data) or depth > 14:
            return None
        if i in cache:
            return cache[i]
        v = data[i]
        if isinstance(v, dict):
            out = cache[i] = {}
            for k, j in v.items():
                out[k] = res(j, depth + 1)
            return out
        if isinstance(v, list):
            if len(v) == 2 and v[0] in wrappers:
                out = cache[i] = res(v[1], depth + 1)
                return out
            out = cache[i] = [res(j, depth + 1) for j in v]
            return out
        cache[i] = v
        return v
    return res


def _names(seq):
    out = []
    for x in seq or []:
        if isinstance(x, dict):
            x = x.get("name") or x.get("title") or x.get("label")
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def parse_fictionzone_page(soup):
    """Página /browse guardada: las novelas están en el bloque JSON __NUXT_DATA__ (título, sinopsis,
    estado, capítulos, géneros y etiquetas)."""
    tag = soup.find("script", id="__NUXT_DATA__")
    if not tag:
        return []
    try:
        data = json.loads(tag.get_text())
    except ValueError:
        return []
    res = _resolve_nuxt(data)
    base = SITES["fictionzone"]["base"]
    out = []
    for i, v in enumerate(data):
        if not (isinstance(v, dict) and "items" in v and "total" in v):
            continue
        for it in (res(i) or {}).get("items") or []:
            if not isinstance(it, dict) or not it.get("slug") or not it.get("title"):
                continue
            slug = str(it["slug"])
            genres, tags = _names(it.get("genres")), _names(it.get("tags"))
            syn = (it.get("synopsis") or "").strip()
            extra = " | ".join(x for x in (", ".join(genres), ", ".join(tags)) if x)
            desc = f"{syn}\n{extra}".strip() if extra else syn
            url = f"{base}/omniportal/fq/{slug[3:]}" if slug.startswith("fq-") else f"{base}/novel/{slug}"
            st = str(it.get("status") or "").lower()
            out.append({"id": slug, "title": str(it["title"]).strip(), "url": url, "author": "",
                        "category": genres[0] if genres else "",
                        "status": "已完结" if st == "completed" else ("连载中" if st == "ongoing" else ""),
                        "words": None, "description": desc,
                        "desc_state": DESC_SNIPPET if desc else DESC_NONE})
    return out


def parse_ixdzs_list(soup, base, category=""):
    """Tarjeta: <h3><a href="/read/ID/">título</a></h3>, autor (/author/), «主角：…», «247.7万字连载中»,
    sinopsis y «最新: capítulo fecha»."""
    out, seen = [], set()
    for h in soup.find_all(HEADINGS):
        a = h.find("a", href=True)
        if not a:
            continue
        m = re.match(r"^/read/(\d+)/?$", urlparse(urljoin(base, a["href"])).path)
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not m or not title or m.group(1) in seen:
            continue
        nid = m.group(1)
        seen.add(nid)
        texts = following_texts(h, max_chars=2500)
        author_a = next((x for x in h.find_all_next("a", href=True, limit=6)
                         if "/author/" in x["href"]), None)
        author = author_a.get_text(strip=True) if author_a else ""
        si = next((i for i, t in enumerate(texts) if STATUS_RX.search(t) and "字" in t), None)
        if si is None:
            si = next((i for i, t in enumerate(texts) if STATUS_RX.search(t)), None)
        status = STATUS_RX.search(texts[si]).group(1) if si is not None else ""
        wi = next((i for i, t in enumerate(texts[:12]) if re.search(r"[\d.]+\s*万?\s*字", t)), None)
        words = parse_words(texts[wi]) if wi is not None else None
        cut = next((i for i, t in enumerate(texts) if re.match(r"^最新\s*[:：]?", t)), len(texts))
        cand = [t for i, t in enumerate(texts[:cut])
                if i not in (si, wi) and t != author and not t.startswith("主角") and not DATE_RX.match(t)]
        # el nombre del protagonista queda en un texto aparte justo después de «主角：»
        pi = next((i for i, t in enumerate(texts[:cut]) if t.startswith("主角")), None)
        if pi is not None and pi + 1 < len(texts) and texts[pi + 1] in cand and len(texts[pi + 1]) < 40:
            cand.remove(texts[pi + 1])
        desc = max(cand, key=len, default="").strip() if cand else ""
        dm = next((DATE_FIND.search(t) for t in texts[cut:cut + 6] if DATE_FIND.search(t)), None)
        out.append({"id": nid, "title": title, "url": f"{base}/read/{nid}/", "author": author,
                    "category": category, "status": status, "words": words, "description": desc,
                    "desc_state": DESC_SNIPPET if desc else DESC_NONE,
                    "updated": dm.group(0) if dm else None})
    return out


def parse_sudu_list(soup, base):
    out, seen = [], set()
    for h in soup.find_all(HEADINGS):
        a = h.find("a", href=True)
        if not a:
            continue
        m = re.match(r"^/(\d+)/?$", urlparse(urljoin(base, a["href"])).path)
        title = a.get_text(strip=True)
        if not m or not title or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        blob = " ".join(following_texts(h, max_chars=300))
        sm = re.search(r"(连载中|已完结|已完本|完结|连载)\s*([^\s作：:·\d]{1,8}?小说)?", blob)
        am = re.search(r"作者[：:]\s*(\S+)", blob)
        out.append({"id": m.group(1), "title": title, "url": f"{base}/{m.group(1)}/",
                    "author": am.group(1) if am else "", "status": sm.group(1) if sm else "",
                    "category": (sm.group(2) or "").replace("小说", "") if sm else ""})
    return out


def parse_washai_list(soup, base):
    out, seen = [], set()
    for h in soup.find_all(HEADINGS):
        a = h.find("a", href=True)
        if not a:
            continue
        m = re.match(r"^/book/(\d+)\.html$", urlparse(urljoin(base, a["href"])).path)
        title = a.get_text(strip=True)
        if not m or not title or m.group(1) in seen:
            continue
        nid = m.group(1)
        seen.add(nid)
        texts = following_texts(h, max_chars=900)
        cat_a = h.find_previous("a", href=re.compile(r"bookclass\.html\?c=\d+"))
        author = ""
        for i, t in enumerate(texts):
            if t.startswith("作者"):
                rest = re.split(r"[：:]", t, maxsplit=1)[-1].strip() if re.search(r"[：:]", t) else ""
                author = rest or (texts[i + 1] if i + 1 < len(texts) else "")
                break
        si = next((i for i, t in enumerate(texts) if STATUS_RX.search(t)), None)
        status = STATUS_RX.search(texts[si]).group(1) if si is not None else ""
        words = parse_words(" ".join(texts[si:si + 3])) if si is not None else None
        # el capítulo más reciente es un enlace /book/ID/CAPITULO.html; la sinopsis va justo después
        start = (si + 1) if si is not None else 0
        ch_a = next((x for x in h.find_all_next("a", href=True, limit=12)
                     if re.match(rf"^/book/{nid}/\d+\.html$", urlparse(urljoin(base, x["href"])).path)), None)
        if ch_a is not None:
            ct = ch_a.get_text(strip=True)
            ci = next((i for i, t in enumerate(texts) if t == ct), None)
            if ci is not None:
                start = ci + 1
        desc_parts, updated = texts[start:], None
        for i, t in enumerate(desc_parts):      # la fecha cierra la tarjeta: lo que sigue es la novela siguiente
            if DATE_RX.match(t):
                updated, desc_parts = t, desc_parts[:i]
                break
        desc = " ".join(desc_parts).strip()
        out.append({"id": nid, "title": title, "url": f"{base}/book/{nid}.html", "author": author,
                    "category": cat_a.get_text(strip=True) if cat_a else "", "status": status,
                    "words": words, "description": desc, "desc_state": DESC_SNIPPET if desc else DESC_NONE,
                    "updated": updated})
    return out


def parse_sudu_detail(soup):
    h1 = soup.find("h1")
    words, blob = None, ""
    if h1:
        words = parse_words(h1.get_text(" "))
        blob = " ".join(following_texts(h1, stop=("h2", "h3"), max_chars=300))
    sm = re.search(r"(连载中|已完结|已完本|完结|连载)\s*([^\s作：:·\d]{1,8}?小说)?", blob)
    a = soup.find("a", href=re.compile(r"/zuozhe/"))
    author = re.sub(r"^作者[：:]\s*", "", a.get_text(strip=True)) if a else ""
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if ln == "TXT下载"), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if ln == "开始阅读"), None)
    desc = ""
    if start is not None:
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("更新时间")), None)
        if end is not None:
            desc = "\n".join(ln for ln in lines[start + 1:end] if ln not in ("开始阅读", "TXT下载"))[:4000]
    updated = None
    for j, ln in enumerate(lines):
        if ln.startswith("更新时间"):
            m = DATE_FIND.search(ln + " " + (lines[j + 1] if j + 1 < len(lines) else ""))
            updated = m.group(0) if m else None
            break
    return {"words": words, "status": sm.group(1) if sm else "",
            "category": (sm.group(2) or "").replace("小说", "") if sm else "",
            "author": author, "description": desc, "updated": updated}


# =============================================================================== importar HTML guardado
def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and (tag.get("content") or "").strip():
            return tag["content"].strip()
    return None


def _page_url(soup, raw):
    """Dirección original de una página guardada (canonical, og:url o el comentario «saved from url»)."""
    for tag in (soup.find("link", rel="canonical"), soup.find("meta", property="og:url")):
        u = (tag.get("href") or tag.get("content") or "") if tag else ""
        if u.startswith("http"):
            return u
    m = re.search(rb"saved from url=\(\d+\)(\S+)", raw[:4000])
    return m.group(1).decode("ascii", "ignore") if m else ""


def _site_from_url(url):
    host = (urlparse(url).hostname or "").lower() if url else ""
    return next((k for k, c in SITES.items() if host in c["hosts"]), None)


def _ixdzs_category(name):
    return next((n for n in IXDZS_SORTS.values() if (name or "").startswith(n)), name or "")


def parse_ixdzs_detail(soup, url):
    """Ficha de una novela de ixdzs8.com (/read/ID/): trae la sinopsis completa y la fecha de actualización."""
    name = _meta(soup, "og:novel:book_name")
    m = re.search(r"/read/(\d+)", url or "") or re.search(r"/read/(\d+)", _meta(soup, "og:novel:read_url") or "")
    if not (name and m):
        return None
    nid = m.group(1)
    og = _meta(soup, "og:description") or ""
    wm = re.search(r"全书\s*([\d.]+)\s*万字", og)
    intro = soup.find(id="intro")
    desc = " ".join(intro.get_text(" ", strip=True).split()) if intro else ""
    if not CJK_RX.search(desc):                      # traducida por el navegador: se usa el resumen original
        desc = re.split(r"提供简体全文", og)[0].split("（全书")[-1].split("）。", 1)[-1].strip() if og else ""
    upd = (_meta(soup, "og:novel:update_time") or "").replace("T", " ")[:16]
    return {"id": nid, "title": name, "url": f"{SITES['ixdzs']['base']}/read/{nid}/",
            "author": _meta(soup, "og:novel:author") or "", "category": _ixdzs_category(_meta(soup, "og:novel:category")),
            "status": _meta(soup, "og:novel:status") or "", "words": float(wm.group(1)) if wm else None,
            "description": desc, "desc_state": DESC_FULL if desc else DESC_NONE, "updated": upd or None}


def parse_sudu_detail_page(soup, url):
    """Ficha de una novela de suduguu.com (/ID/) guardada: sinopsis completa, estado, palabras y fecha."""
    m = re.match(r"^/(\d+)/?$", urlparse(url or "").path)
    h1 = soup.find("h1")
    if not (m and h1 and re.search(r"TXT下载|开始阅读", soup.get_text(" "))):
        return None
    d = parse_sudu_detail(soup)
    og = _meta(soup, "og:title")
    title = og or re.sub(r"\s*[\d.]+\s*万?字.*$", "", h1.get_text(" ", strip=True)).strip()
    nid = m.group(1)
    return {"id": nid, "title": title, "url": f"{SITES['suduguu']['base']}/{nid}/", "author": d["author"],
            "category": d["category"], "status": d["status"], "words": d["words"], "description": d["description"],
            "desc_state": DESC_FULL if d["description"] else DESC_NONE, "updated": d.get("updated")}


def parse_saved_page(soup, url=""):
    """Reconoce el sitio y el tipo de página. Devuelve (sitio, tipo, registros) con tipo «listado» o «ficha»;
    (None, None, []) si no se reconoce."""
    hint = _site_from_url(url)
    sites = [hint] if hint else ["fictionzone", "ixdzs", "washai", "suduguu"]
    for site in sites:
        base = SITES[site]["base"]
        if site == "fictionzone":
            recs = parse_fictionzone_page(soup)
            if recs:
                return site, "listado", recs
        elif site == "ixdzs":
            rec = parse_ixdzs_detail(soup, url)
            if rec:
                return site, "ficha", [rec]
            m = re.search(r"/sort/(\d+)/", url or "")
            recs = parse_ixdzs_list(soup, base, IXDZS_SORTS.get(int(m.group(1)), "") if m else "")
            if recs:
                return site, "listado", recs
        elif site == "washai":
            recs = parse_washai_list(soup, base)
            if recs:
                return site, "listado", recs
        elif site == "suduguu":
            rec = parse_sudu_detail_page(soup, url)
            if rec:
                return site, "ficha", [rec]
            recs = parse_sudu_list(soup, base)
            if recs:
                return site, "listado", recs
    return None, None, []


def import_saved_pages(paths, log=print, db_path=None, progress=None, stop=None):
    """Indexa páginas guardadas con el navegador (Ctrl+S): listados y fichas de novela de los cuatro sitios.
    No se conecta a ningún sitio. `paths`: archivos .htm/.html y/o carpetas.
    Devuelve (archivos_leídos, novelas_nuevas, novelas_vistas)."""
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in (".htm", ".html") and f.is_file()
                            and not any(part.endswith(("_files", "_archivos")) for part in f.parts))
        elif p.is_file():
            files.append(p)
    con = open_db(db_path)
    read = new = seen = 0
    try:
        for i, f in enumerate(files, 1):
            if stop is not None and stop.is_set():
                log("Importación cancelada (lo importado hasta aquí queda guardado).")
                break
            if progress:
                progress(i, len(files))
            try:
                raw = f.read_bytes()
                soup = BeautifulSoup(raw, "html.parser")          # detecta la codificación por sí mismo
            except OSError as e:
                log(f"  ✗ {f.name}: {e}")
                continue
            site, kind, recs = parse_saved_page(soup, _page_url(soup, raw))
            if not recs:
                log(f"  – {f.name}: no se reconoce como listado ni ficha de novela de los sitios compatibles "
                    "(¿se guardó la página completa y sin traducir?)")
                continue
            if not any(CJK_RX.search(r["title"]) for r in recs) and site != "fictionzone":
                log(f"  ⚠ {f.name}: parece traducida por el navegador; guárdala en su idioma original.")
            read += 1
            seen += len(recs)
            added = sum(1 for r in recs if upsert_novel(con, site, r))
            new += added
            log(f"  ✓ {f.name}: {kind} de {site_name(site)} — {len(recs)} novelas ({added} nuevas)")
            if i % 20 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    return read, new, seen


# =============================================================================== exportar
def export_catalog(path, db_path=None):
    """Guarda todo el catálogo (con las traducciones y el glosario) en un JSON compacto para el userscript
    del navegador. Devuelve (novelas, bytes)."""
    con = open_db(db_path)
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM novels ORDER BY site, CAST(id AS INTEGER), id")]
        traductor.attach_cached(con, rows)
    finally:
        con.close()
    glossary = [[list(a), list(syn)] for a, syn in GLOSARIO]
    if USER_GLOSSARY.exists():
        try:
            for key, syns in json.loads(USER_GLOSSARY.read_text("utf-8")).items():
                if isinstance(syns, list):
                    glossary.append([[k for k in key.split("|") if k.strip()], [str(x) for x in syns]])
        except (OSError, ValueError):
            pass
    data = {"v": 1, "exported": time.strftime("%Y-%m-%d %H:%M"),
            "sites": {k: site_name(k) for k in SEARCH_SITES}, "glossary": glossary,
            # [sitio, id, título, título_es, autor, categoría, estado, 万字, url, sinopsis, sinopsis_es, completa, actualizada]
            "novels": [[r["site"], r["id"], r["title"] or "", r.get("title_es") or "", r.get("author") or "",
                        r.get("category") or "", r.get("status") or "", r.get("words"), r["url"],
                        r.get("description") or "", r.get("description_es") or "",
                        1 if r.get("desc_state") == DESC_FULL else 0, (r.get("updated") or "")[:16]] for r in rows]}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    Path(path).write_text(text, "utf-8")
    return len(rows), len(text.encode("utf-8"))


# =============================================================================== búsqueda
def norm_key(s):
    s = unicodedata.normalize("NFKD", str(s).strip().casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def build_lookup(extra=None):
    """alias (sin acentos) -> sinónimos chinos;  término chino -> su lista de sinónimos."""
    entries = [(tuple(a), list(s)) for a, s in GLOSARIO]
    if extra is None and USER_GLOSSARY.exists():
        try:
            extra = json.loads(USER_GLOSSARY.read_text("utf-8"))
        except (OSError, ValueError):
            extra = None
    for key, syns in (extra or {}).items():
        if isinstance(syns, list):
            entries.append((tuple(k for k in key.split("|") if k.strip()), [str(s) for s in syns]))
    by_alias, by_term = {}, {}
    for aliases, syns in entries:
        for a in aliases:
            by_alias.setdefault(norm_key(a), []).extend(syns)
        for s in syns:
            by_term.setdefault(norm_key(s), []).extend(syns)
    return {"alias": by_alias, "term": by_term}


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def parse_query(q, lookup=None, expand=True):
    """'cultivación sistema|system -harén "palabra suelta"' -> (grupos, exclusiones).
    Cada grupo es una lista de alternativas (OR); entre grupos se combina según el modo."""
    lookup = lookup if lookup is not None else build_lookup()
    include, exclude = [], []
    q = q.replace("，", " ").replace(",", " ").replace("、", " ").replace("；", " ").replace(";", " ")
    for m in re.finditer(r'(-?)"([^"]+)"|(-?)(\S+)', q):
        neg = (m.group(1) or m.group(3)) == "-"
        text = (m.group(2) or m.group(4) or "").strip()
        if not text or text == "-":
            continue
        group = []
        for alt in re.split(r"[|｜]", text):
            alt = alt.strip()
            if not alt:
                continue
            group.append(alt)
            if expand and alt == alt.lower():      # con mayúsculas: búsqueda exacta, sin sinónimos
                k = norm_key(alt)
                group += lookup["alias"].get(k, []) + lookup["term"].get(k, [])
        group = _dedupe(group)
        if group:
            (exclude if neg else include).append(group)
    return include, exclude


def _is_done(status):
    return "完" in (status or "")


def _ident(text):
    """Texto normalizado para comparar títulos/autores: sin espacios, signos ni diferencias de ancho/mayúsculas."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text or "")).casefold())


def dedupe_results(results):
    """Deja una sola entrada por novela repetida (mismo título y autor, en el mismo sitio o en ambos):
    la de más texto (≈ más capítulos). La entrada elegida lleva en r['dups'] las versiones ocultas
    y ocupa la posición de la mejor del grupo. Si el autor falta en alguna, se une al único autor posible."""
    by_title = {}
    for r in results:
        by_title.setdefault(_ident(r["row"].get("title")), []).append(r)
    groups = []
    for key, items in by_title.items():
        if not key:
            groups += [[r] for r in items]
            continue
        clusters = {}
        for r in items:
            clusters.setdefault(_ident(r["row"].get("author")), []).append(r)
        known = [a for a in clusters if a]
        if "" in clusters and len(known) == 1:                 # autor desconocido + un solo autor posible
            clusters[known[0]] += clusters.pop("")
        for a, lst in clusters.items():
            if a or not known:                  # autor conocido, o ninguno lo conoce: misma novela
                groups.append(lst)
            else:                               # sin autor y varios autores posibles: no se puede saber
                groups += [[r] for r in lst]
    out = []
    for grp in groups:
        def quality(r):
            row = r["row"]
            return (row.get("words") or 0, 1 if row.get("desc_state") == DESC_FULL else 0,
                    len(row.get("description") or ""), r["score"])
        rep_ = max(grp, key=quality)
        rep_ = dict(rep_)
        rep_["dups"] = [r["row"] for r in grp if r["row"] is not rep_["row"]]
        rep_["_rank"] = max(((r["matched"], r["score"]) for r in grp))
        out.append(rep_)
    out.sort(key=lambda x: (-x["_rank"][0], -x["_rank"][1]))
    return out


def _matcher(alt, whole=False):
    """(texto, distingue_mayúsculas, regex|None). Si lo escrito lleva alguna MAYÚSCULA se busca exactamente así
    (distingue mayúsculas de minúsculas); si está todo en minúsculas se ignoran. `whole`: palabra completa
    (no dentro de otra) para términos que empiezan y terminan en letra/número latino."""
    cs = alt != alt.lower()
    needle = alt if cs else alt.lower()
    rx = None
    if whole and re.match(r"[A-Za-z0-9]", needle[:1]) and re.match(r"[A-Za-z0-9]", needle[-1:]):
        rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])")
    return needle, cs, rx


def _mcount(m, orig, low):
    needle, cs, rx = m
    s = orig if cs else low
    return len(rx.findall(s)) if rx else s.count(needle)


def _mfind(m, orig, low):
    needle, cs, rx = m
    s = orig if cs else low
    if rx:
        mo = rx.search(s)
        return mo.start() if mo else -1
    return s.find(needle)


def search(rows, include, exclude=(), mode="todas", status=None, min_words=None, max_words=None, limit=100,
           dedupe=True, whole=False):
    """Devuelve [{'row','score','matched','of','terms','snips'}] ordenado por relevancia.
    mode: 'todas' | 'alguna' | número (mínimo de grupos que deben aparecer).
    status: None | 'ongoing' | 'done'.
    Los términos con mayúsculas se buscan tal cual (distingue mayúsculas); `whole` exige palabra completa."""
    if not include:
        return []
    cands = []
    for r in rows:
        if status == "done" and not _is_done(r.get("status")):
            continue
        if status == "ongoing" and _is_done(r.get("status")):
            continue
        w = r.get("words")
        if (min_words is not None and (w is None or w < min_words)) or \
           (max_words is not None and (w is None or w > max_words)):
            continue
        cands.append(r)
    inc = [[_matcher(a, whole) for a in grp] for grp in include]
    exc = [[_matcher(a, whole) for a in grp] for grp in exclude]
    G = len(inc)
    texts = [((r.get("title") or ""), (r.get("description") or "")) for r in cands]
    lows = [(t.lower(), d.lower()) for t, d in texts]
    hits = [[(sum(_mcount(m, t, tl) for m in grp), sum(_mcount(m, d, dl) for m in grp)) for grp in inc]
            for (t, d), (tl, dl) in zip(texts, lows)]
    df = [sum(1 for h in hits if h[gi][0] or h[gi][1]) for gi in range(G)]
    idf = [math.log(1 + len(cands) / (1 + n)) for n in df]
    try:
        need = G if mode == "todas" else 1 if mode == "alguna" else int(mode)
    except (TypeError, ValueError):
        need = G
    need = max(1, min(need, G))
    results = []
    for r, h, (t, d), (tl, dl) in zip(cands, hits, texts, lows):
        matched = [gi for gi in range(G) if h[gi][0] or h[gi][1]]
        if len(matched) < need:
            continue
        if exc:
            full, full_l = t + "\n" + d, tl + "\n" + dl
            if any(_mcount(m, full, full_l) for grp in exc for m in grp):
                continue
        score = sum(idf[gi] * (1 + 0.5 * math.log(1 + h[gi][1]) + (1.5 if h[gi][0] else 0)) for gi in matched)
        found, snips = [], []
        for gi in matched:
            counts = [(_mcount(m, d, dl) + _mcount(m, t, tl), m) for m in inc[gi]]
            best = max(counts, key=lambda c: c[0])[1]
            found.append(best[0])
            pos = _mfind(best, d, dl)
            if pos >= 0 and len(snips) < 2:
                n = len(best[0])
                a, b = max(0, pos - 24), min(len(d), pos + n + 24)
                snips.append(("…" if a else "") + d[a:pos] + "【" + d[pos:pos + n] + "】"
                             + d[pos + n:b] + ("…" if b < len(d) else ""))
        results.append({"row": r, "score": score, "matched": len(matched), "of": G,
                        "terms": found, "snips": snips})
    results.sort(key=lambda x: (-x["matched"], -x["score"]))
    if dedupe:
        results = dedupe_results(results)
    return results if not limit else results[:limit]      # limit=None/0: sin límite


def google_translate_url(text, limit=7000, sl="zh-CN", tl="es"):
    """URL de Google Translate con `text` ya cargado. Si no cabe en la URL (límite `limit` caracteres
    codificados) se recorta en un salto de línea o fin de frase. Devuelve (url, recortado)."""
    base = f"https://translate.google.com/?sl={sl}&tl={tl}&op=translate&text="
    text = text.strip()
    budget = limit - len(base)
    if len(quote(text)) <= budget:
        return base + quote(text), False
    lo, hi = 0, len(text)
    while lo < hi:                                   # mayor prefijo cuya codificación cabe
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if len(quote(text[:mid])) <= budget else (lo, mid - 1)
    cut = text[:lo]
    k = max(cut.rfind("\n"), cut.rfind("。"), cut.rfind("."))
    if k > len(cut) * 0.6:
        cut = cut[:k + 1]
    return base + quote(cut.strip()), True


def open_in_google_translate(text):
    """Abre Google Translate con el texto cargado. Usa una página local que redirige, porque algunos
    sistemas limitan la longitud de las URL que se pasan directamente al navegador.
    Devuelve True si el texto tuvo que recortarse."""
    url, truncated = google_translate_url(text)
    page = Path(tempfile.gettempdir()) / "buscador_novelas_traducir.html"
    page.write_text(
        '<!doctype html><meta charset="utf-8"><title>Abriendo Google Translate…</title>'
        f'<script>location.replace("{url}")</script>'
        f'<p>Abriendo Google Translate… si no ocurre nada, <a href="{html.escape(url)}">pulsa aquí</a>.</p>',
        "utf-8")
    webbrowser.open(page.as_uri())
    return truncated


STATUS_CHOICES = {"todas": None, "todos": None, "encurso": "ongoing", "curso": "ongoing", "en-curso": "ongoing",
                  "finalizadas": "done", "finalizada": "done", "completas": "done", "terminadas": "done"}


# =============================================================================== consola
def cmd_estado():
    con = open_db()
    print(format_stats(catalog_stats(con)))
    print(f"Base de datos: {DB_PATH}")
    con.close()


def cmd_buscar(a):
    con = open_db()
    sites = list(SEARCH_SITES) if a.sitio == "ambos" else [a.sitio]
    rows = load_rows(con, sites)
    con.close()
    if not rows:
        sys.exit("El catálogo está vacío. Ejecuta primero:  python novel_search.py indexar")
    include, exclude = parse_query(a.consulta, expand=not a.sin_sinonimos)
    if not include:
        sys.exit("Escribe al menos una palabra clave.")
    print("Buscando: " + "  Y  ".join("(" + " | ".join(grp[:6]) + (" …" if len(grp) > 6 else "") + ")"
                                       for grp in include)
          + ("   EXCLUYENDO " + ", ".join(x[0] for x in exclude) if exclude else ""))
    res = search(rows, include, exclude, a.modo, STATUS_CHOICES.get(a.estado.lower()), a.min_palabras,
                 a.max_palabras, a.limite, dedupe=not a.con_repetidos, whole=a.palabra_completa)
    if not res:
        print("Sin resultados. Prueba con --modo alguna o con menos palabras.")
        return
    if a.traducir:
        try:
            eng = traductor.ENGINES[a.motor]
            fn = eng["make"]()
            con = open_db()
            done, failed, reason = traductor.translate_rows(
                con, [r["row"] for r in res], fn, engine=eng, force=a.retraducir,
                pause=eng.get("pause", 3.0) if a.pausa is None else a.pausa,
                one_by_one=(False if a.lotes else True if a.uno_por_uno else eng.get("one_by_one", True)),
                progress=lambda d, t: print(f"\r  traduciendo {d}/{t}", end="", flush=True))
            con.close()
            print()
            if reason:
                print(reason)
        except RuntimeError as e:
            print(e)
    for i, r in enumerate(res, 1):
        row = r["row"]
        w = f"{row['words']:.0f}万字" if row.get("words") else "?"
        print(f"\n{i:3d}. [{r['matched']}/{r['of']}] {row['title']} — {row.get('author') or '?'} | "
              f"{row.get('category') or '?'} | {row.get('status') or '?'} | {w} | {site_name(row['site'])}")
        if row.get("title_es"):
            print(f"     {row['title_es']}")
        print(f"     {row['url']}")
        for d in r.get("dups", []):
            dw = f"{d['words']:.0f}万字" if d.get("words") else "?"
            print(f"     (repetida oculta: {site_name(d['site'])}, {dw}) {d['url']}")
        if row.get("description_es"):
            print(f"     {row['description_es'][:300]}" + ("…" if len(row["description_es"]) > 300 else ""))
        else:
            for s in r["snips"]:
                print(f"     {s}")


# =============================================================================== ventana
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Buscador de novelas")
    # tamaño según la pantalla: deja margen para la barra de tareas y el título de la ventana
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    ww, wh = min(1100, sw - 40), min(760, sh - 120)
    root.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+10")
    root.minsize(min(760, ww), min(460, wh))
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    tab_s, tab_i = ttk.Frame(nb, padding=8), ttk.Frame(nb, padding=8)
    nb.add(tab_s, text="Buscar")
    nb.add(tab_i, text="Catálogo")
    lookup = build_lookup()
    state = {"results": [], "terms": []}
    status_bar = tk.StringVar(value="")

    # ---------------------------------------------------------------- pestaña Buscar
    query_v, expand_v = tk.StringVar(), tk.BooleanVar(value=True)
    site_vars = {k: tk.BooleanVar(value=True) for k in SEARCH_SITES}
    status_v, mode_v = tk.StringVar(value="Todos"), tk.StringVar(value="Todas las palabras")
    row1 = ttk.Frame(tab_s)
    row1.pack(fill="x")
    ttk.Label(row1, text="Palabras clave:").pack(side="left")
    entry = ttk.Entry(row1, textvariable=query_v)
    entry.pack(side="left", fill="x", expand=True, padx=6)
    entry.focus()
    btn_search = ttk.Button(row1, text="Buscar")
    btn_search.pack(side="left")
    row2 = ttk.Frame(tab_s)
    row2.pack(fill="x", pady=4)
    for k, v in site_vars.items():
        ttk.Checkbutton(row2, text=site_name(k), variable=v).pack(side="left", padx=(0, 8))
    ttk.Combobox(row2, textvariable=status_v, width=18, state="readonly",
                 values=["Todos", "En curso (连载)", "Finalizadas (完结)"]).pack(side="left", padx=4)
    ttk.Combobox(row2, textvariable=mode_v, width=20, state="readonly",
                 values=["Todas las palabras", "Alguna palabra", "Al menos 2"]).pack(side="left", padx=4)
    ttk.Checkbutton(row2, text="Usar sinónimos", variable=expand_v).pack(side="left", padx=8)
    dedupe_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(row2, text="Ocultar repetidos", variable=dedupe_v).pack(side="left", padx=4)
    whole_v = tk.BooleanVar(value=False)
    ttk.Checkbutton(row2, text="Palabra completa", variable=whole_v).pack(side="left", padx=4)
    expl = tk.StringVar(value='Consejos: varias palabras = todas deben aparecer · a|b = una u otra · '
                              '-palabra = excluir · "frase exacta" · con MAYÚSCULAS se busca exactamente así')
    ttk.Label(tab_s, textvariable=expl, foreground="#555", wraplength=980).pack(anchor="w")

    cols = ("pts", "sitio", "titulo", "titulo_es", "autor", "cat", "estado", "palabras", "actualizada")
    heads = {"pts": ("Coinc.", 55), "sitio": ("Sitio", 105), "titulo": ("Título", 200), "titulo_es": ("Título (español)", 220), "autor": ("Autor", 110),
             "cat": ("Categoría", 75), "estado": ("Estado", 65), "palabras": ("万字", 60),
             "actualizada": ("Actualizada", 120)}
    bottom = ttk.Frame(tab_s)                 # botones y estado: siempre visibles abajo
    bottom.pack(side="bottom", fill="x")
    box = ttk.Frame(tab_s)
    box.pack(fill="both", expand=True, pady=4)
    tree = ttk.Treeview(box, columns=cols, show="headings", height=8, selectmode="browse")
    sort_state = {"col": None, "desc": False}
    NUMERIC = ("pts", "palabras")           # la 1.ª vez se ordenan de mayor a menor

    def sort_key(col, iid):
        if col == "pts":                    # relevancia real de la búsqueda
            i = int(iid)
            return state["results"][i]["score"] if i < len(state["results"]) else 0.0
        v = str(tree.set(iid, col)).strip()
        if col == "palabras":
            try:
                return float(v)
            except ValueError:
                return None
        return v.casefold() if v else None

    def sort_by(col):
        desc = (not sort_state["desc"]) if sort_state["col"] == col else (col in NUMERIC)
        sort_state.update(col=col, desc=desc)
        ids = list(tree.get_children(""))
        keyed = [(sort_key(col, i), i) for i in ids]
        full = sorted((k for k in keyed if k[0] is not None), key=lambda k: k[0], reverse=desc)
        empty = [k for k in keyed if k[0] is None]            # los vacíos siempre al final
        for pos, (_k, iid) in enumerate(full + empty):
            tree.move(iid, "", pos)
        for c in cols:
            tree.heading(c, text=heads[c][0] + ((" ▼" if desc else " ▲") if c == col else ""))
        sel = tree.selection()
        if sel:
            tree.see(sel[0])

    def reset_sort():
        sort_state.update(col=None, desc=False)
        for c in cols:
            tree.heading(c, text=heads[c][0])

    for c in cols:
        tree.heading(c, text=heads[c][0], command=lambda c=c: sort_by(c))
        tree.column(c, width=heads[c][1], anchor="w")
    sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    info = tk.Text(tab_s, height=7, wrap="word", state="disabled")
    info.tag_configure("hl", background="#fff2a8")
    info.tag_configure("h", font=("TkDefaultFont", 11, "bold"))
    info.pack(fill="both", expand=True)
    row3 = ttk.Frame(bottom)
    row3.pack(fill="x", pady=(4, 2))
    row4 = ttk.Frame(bottom)
    row4.pack(fill="x", pady=(0, 2))
    btn_open = ttk.Button(row3, text="Abrir en el navegador")
    btn_open.pack(side="left")
    btn_copy = ttk.Button(row3, text="Copiar URL")
    btn_copy.pack(side="left", padx=6)
    btn_gt = ttk.Button(row3, text="Abrir en Google Translate (navegador)")
    btn_gt.pack(side="left")
    btn_tr = ttk.Button(row4, text="Traducir títulos al español")
    btn_tr.pack(side="left", padx=(0, 6))
    ttk.Label(row4, text="Motor:").pack(side="left")
    eng_labels = {v["label"]: k for k, v in traductor.ENGINES.items()}
    def_eng = traductor.ENGINES[traductor.DEFAULT_ENGINE]
    engine_v = tk.StringVar(value=def_eng["label"])
    eng_box = ttk.Combobox(row4, textvariable=engine_v, values=list(eng_labels), state="readonly", width=28)
    eng_box.pack(side="left", padx=(2, 6))
    single_v = tk.BooleanVar(value=def_eng.get("one_by_one", True))
    ttk.Checkbutton(row4, text="Uno por uno", variable=single_v).pack(side="left", padx=(0, 6))
    retr_v = tk.BooleanVar(value=False)
    ttk.Checkbutton(row4, text="Volver a traducir", variable=retr_v).pack(side="left", padx=(0, 6))
    ttk.Label(row4, text="Pausa (s):").pack(side="left")
    pause_v = tk.StringVar(value=f"{def_eng.get('pause', 3.0):g}")
    ttk.Spinbox(row4, from_=0, to=60, increment=0.1, width=5, textvariable=pause_v).pack(side="left", padx=(2, 8))

    def on_engine(_=None):
        """Al cambiar de motor se ponen su pausa y su «Uno por uno» recomendados (se pueden cambiar a mano)."""
        e = traductor.ENGINES[eng_labels.get(engine_v.get(), traductor.DEFAULT_ENGINE)]
        pause_v.set(f"{e.get('pause', 3.0):g}")
        single_v.set(e.get("one_by_one", True))
        set_status(f"Motor: {e['label']} — {e.get('note', '')}")
    eng_box.bind("<<ComboboxSelected>>", on_engine)
    ttk.Label(bottom, textvariable=status_bar, foreground="#555", wraplength=1000).pack(anchor="w")

    def set_status(msg):
        root.after(0, lambda: status_bar.set(msg))

    def selected():
        sel = tree.selection()
        if not sel:
            return None
        i = int(sel[0])
        return state["results"][i] if i < len(state["results"]) else None

    def show_detail(_=None):
        r = selected()
        info.configure(state="normal")
        info.delete("1.0", "end")
        if r:
            row = r["row"]
            w = f"{row['words']:.1f}万字" if row.get("words") else "?"
            if row.get("title_es"):
                info.insert("end", f"{row['title_es']}\n", "h")
                info.insert("end", f"{row['title']}\n")
            else:
                info.insert("end", f"{row['title']}\n", "h")
            info.insert("end", f"{row.get('author') or '?'} · {row.get('category') or '?'} · "
                               f"{row.get('status') or '?'} · {w} · {site_name(row['site'])}\n{row['url']}\n\n")
            for d in r.get("dups", []):
                dw = f"{d['words']:.1f}万字" if d.get("words") else "?"
                info.insert("end", f"Repetida oculta: {site_name(d['site'])} · {dw} · {d['url']}\n")
            if r.get("dups"):
                info.insert("end", "\n")
            kind = {DESC_FULL: "Sinopsis", DESC_SNIPPET: "Fragmento de sinopsis (el sitio solo publica el principio)"}
            info.insert("end", f"{kind.get(row['desc_state'], 'Sin sinopsis')}:\n")
            if row.get("description_es"):
                info.insert("end", row["description_es"] + "\n\n— Original en chino —\n")
            base = float(info.index("end-1c").split(".")[0])
            desc = row.get("description") or ""
            start_line = int(base)
            info.insert("end", desc)
            low = desc.lower()
            for t in {x.lower() for grp in state["terms"] for x in grp}:
                for m in re.finditer(re.escape(t), low):
                    info.tag_add("hl", f"{start_line}.0+{m.start()}c", f"{start_line}.0+{m.end()}c")
        info.configure(state="disabled")

    def do_search(_=None):
        q = query_v.get().strip()
        if not q:
            return
        sites = [k for k, v in site_vars.items() if v.get()]
        con = open_db()
        rows = load_rows(con, sites)
        con.close()
        if not rows:
            messagebox.showinfo("Catálogo vacío", "Primero indexa los catálogos en la pestaña «Catálogo».")
            return
        include, exclude = parse_query(q, lookup, expand_v.get())
        if not include:
            return
        status = {"Todos": None, "En curso (连载)": "ongoing", "Finalizadas (完结)": "done"}[status_v.get()]
        mode = {"Todas las palabras": "todas", "Alguna palabra": "alguna", "Al menos 2": "2"}[mode_v.get()]
        res = search(rows, include, exclude, mode, status, limit=None, dedupe=dedupe_v.get(), whole=whole_v.get())
        tr_state["stop"].set()                      # cancela una traducción en curso
        try:
            con = open_db()
            traductor.attach_cached(con, [r["row"] for r in res])   # traducciones ya guardadas
            con.close()
        except sqlite3.Error:
            pass
        state["results"], state["terms"] = res, include
        tree.delete(*tree.get_children())
        reset_sort()
        for i, r in enumerate(res):
            row = r["row"]
            tree.insert("", "end", iid=str(i), values=(
                f"{r['matched']}/{r['of']}", site_name(row["site"]),
                row["title"] + (f"  (+{len(r['dups'])} repetidos)" if r.get("dups") else ""), row.get("title_es", ""),
                row.get("author") or "",
                row.get("category") or "", row.get("status") or "",
                f"{row['words']:.0f}" if row.get("words") else "", (row.get("updated") or "")[:16]))
        expl.set("Buscando: " + "  Y  ".join("(" + " | ".join(grp[:6]) + (" …" if len(grp) > 6 else "") + ")"
                                             for grp in include)
                 + (("   EXCLUYENDO " + ", ".join(x[0] for x in exclude)) if exclude else ""))
        hidden = sum(len(r.get("dups", [])) for r in res)
        set_status(f"{len(res)} resultados de {len(rows)} novelas indexadas"
                   + (f" ({hidden} repetidos ocultos; se queda la versión con más texto)." if hidden else "."))
        show_detail()

    def open_url():
        r = selected()
        if r:
            webbrowser.open(r["row"]["url"])

    def copy_url():
        r = selected()
        if r:
            root.clipboard_clear()
            root.clipboard_append(r["row"]["url"])
            set_status("URL copiada.")

    tr_state = {"fn": None, "engine": None, "stop": threading.Event(), "busy": False}

    def refresh_rows():
        """Vuelca las traducciones disponibles a la tabla y al detalle."""
        for i, r in enumerate(state["results"]):
            if tree.exists(str(i)):
                tree.set(str(i), "titulo_es", r["row"].get("title_es", ""))
        show_detail()

    def translate_results():
        if tr_state["busy"]:
            tr_state["stop"].set()
            return
        res = list(state["results"])
        if not res:
            set_status("Primero haz una búsqueda.")
            return
        tr_state["stop"] = stop_ev = threading.Event()
        tr_state["busy"] = True
        root.after(0, lambda: btn_tr.configure(text="Detener traducción"))
        rows = [r["row"] for r in res]
        try:
            pause = max(0.0, min(60.0, float(pause_v.get().replace(",", "."))))
        except ValueError:
            pause = 0.3
        eng_key = eng_labels.get(engine_v.get(), traductor.DEFAULT_ENGINE)
        eng, force, single = traductor.ENGINES[eng_key], retr_v.get(), single_v.get()

        def job():
            try:
                if tr_state["fn"] is None or tr_state["engine"] != eng_key:
                    tr_state["fn"] = eng["make"]()
                    tr_state["engine"] = eng_key
                con = open_db()
                try:
                    last = [0.0]

                    def on_row(_r):
                        if time.time() - last[0] > 1.0:       # no redibujar en cada texto
                            last[0] = time.time()
                            root.after(0, refresh_rows)

                    done, failed, reason = traductor.translate_rows(
                        con, rows, tr_state["fn"], pause=pause, stop=stop_ev, on_row=on_row, engine=eng, force=force, one_by_one=single,
                        progress=lambda d, t: set_status(f"Traduciendo con {eng['label']}… {d}/{t}"),
                        log=set_status)
                finally:
                    con.close()
                root.after(0, refresh_rows)
                if reason == "cancelado":
                    set_status(f"Traducción detenida ({done} títulos traducidos y guardados).")
                elif reason:
                    set_status(reason)
                else:
                    set_status(f"Traducción terminada: {done} títulos" + (f", {failed} fallidos" if failed else "") + ".")
            except Exception as e:  # noqa: BLE001
                set_status(f"Error al traducir: {e}")
            finally:
                tr_state["busy"] = False
                root.after(0, lambda: btn_tr.configure(text="Traducir títulos al español"))
        threading.Thread(target=job, daemon=True).start()

    def open_google():
        text = "\n".join(ln for ln in info.get("1.0", "end").splitlines()
                         if not ln.strip().startswith(("http://", "https://")) and "— Original en chino —" not in ln)
        if not text.strip():
            set_status("No hay texto que traducir: selecciona primero una novela.")
            return
        root.clipboard_clear()
        root.clipboard_append(text)                  # el texto completo queda también en el portapapeles
        truncated = open_in_google_translate(text)
        set_status("Abierto en Google Translate. " + (
            "El texto era largo y se cargó solo el principio: el completo está copiado, pégalo con Ctrl+V."
            if truncated else "Texto completo cargado (también copiado al portapapeles)."))

    btn_gt.configure(command=open_google)
    btn_tr.configure(command=translate_results)
    btn_search.configure(command=do_search)
    entry.bind("<Return>", do_search)
    tree.bind("<<TreeviewSelect>>", show_detail)
    btn_open.configure(command=open_url)
    btn_copy.configure(command=copy_url)

    # ---------------------------------------------------------------- pestaña Catálogo
    stats_v = tk.StringVar()
    ttk.Label(tab_i, textvariable=stats_v, justify="left").pack(anchor="w", pady=(0, 6))
    ttk.Label(tab_i, foreground="#555", wraplength=980, justify="left", text=(
        "Este programa no descarga nada de ningún sitio. Abre las páginas en tu navegador, guárdalas con Ctrl+S "
        "(«Página web completa», en su idioma original, sin traducir) y pulsa «Importar páginas guardadas». "
        "Puedes elegir muchos archivos a la vez, o una carpeta entera.\n"
        "Qué guardar — listados: suduguu.com /zuixin/ · book.washai.net /book/bookclass.html · "
        "ixdzs8.com /sort/N/ · fictionzone.net /browse (y sus páginas siguientes). "
        "Fichas de novela (traen la sinopsis completa): suduguu.com /ID/ · ixdzs8.com /read/ID/.\n"
        "El catálogo se guarda solo en tu equipo y nunca se comparte.")).pack(anchor="w")
    brow = ttk.Frame(tab_i)
    brow.pack(fill="x", pady=8)
    b_imp = ttk.Button(brow, text="Importar páginas guardadas…")
    b_exp = ttk.Button(brow, text="Exportar catálogo (navegador)")
    b_stop = ttk.Button(brow, text="Cancelar", state="disabled")
    for b in (b_imp, b_exp, b_stop):
        b.pack(side="left", padx=(0, 6))
    bar = ttk.Progressbar(tab_i, mode="determinate")
    bar.pack(fill="x", pady=4)
    logbox = tk.Text(tab_i, height=10, state="disabled", wrap="word")
    logbox.pack(fill="both", expand=True)
    stop = threading.Event()

    def refresh_stats():
        try:
            con = open_db()
            stats_v.set(format_stats(catalog_stats(con)))
            con.close()
        except sqlite3.Error as e:
            stats_v.set(f"(sin datos: {e})")

    def log(msg):
        def w():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, w)

    def prog(d, t):
        root.after(0, lambda: bar.configure(maximum=t, value=d))

    def busy(on):
        def w():
            b_imp.configure(state="disabled" if on else "normal")
            b_stop.configure(state="normal" if on else "disabled")
            if not on:
                refresh_stats()
        root.after(0, w)

    def do_import():
        from tkinter import filedialog
        files = filedialog.askopenfilenames(title="Páginas guardadas (.html) de los sitios compatibles",
                                            filetypes=[("Páginas web", "*.htm *.html"), ("Todos", "*.*")])
        paths = list(files)
        if not paths:
            folder = filedialog.askdirectory(title="…o elige una carpeta con páginas guardadas")
            paths = [folder] if folder else []
        if not paths:
            return
        stop.clear()
        busy(True)

        def job():
            try:
                r, n, v = import_saved_pages(paths, log=log, progress=prog, stop=stop)
                log(f"Importación terminada: {r} páginas leídas, {v} novelas vistas, {n} nuevas.")
            except Exception as e:  # noqa: BLE001
                log(f"Error: {e}")
            finally:
                busy(False)
        threading.Thread(target=job, daemon=True).start()

    def do_export():
        from tkinter import filedialog
        f = filedialog.asksaveasfilename(title="Guardar catálogo para el navegador", defaultextension=".json",
                                         initialfile="catalogo.json", filetypes=[("JSON", "*.json")])
        if not f:
            return
        try:
            n, size = export_catalog(f)
            log(f"Catálogo exportado: {n} novelas ({size / 1e6:.1f} MB) → {f}")
        except Exception as e:  # noqa: BLE001
            log(f"Error al exportar: {e}")

    b_imp.configure(command=do_import)
    b_exp.configure(command=do_export)
    b_stop.configure(command=stop.set)
    refresh_stats()
    root.mainloop()


# =============================================================================== CLI
def main():
    ap = argparse.ArgumentParser(description="Buscador de novelas sobre un catálogo local (se importa desde páginas guardadas)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("buscar", help="busca palabras clave en las sinopsis")
    p.add_argument("consulta", help='p. ej. "cultivación sistema -harén"  ·  a|b = una u otra')
    p.add_argument("--sitio", choices=list(SEARCH_SITES) + ["ambos"], default="ambos")
    p.add_argument("--estado", default="todas", help="todas | encurso | finalizadas")
    p.add_argument("--modo", default="todas", help="todas | alguna | N (mínimo de palabras que deben aparecer)")
    p.add_argument("--sin-sinonimos", action="store_true")
    p.add_argument("--min-palabras", type=float, help="mínimo en 万字")
    p.add_argument("--max-palabras", type=float, help="máximo en 万字")
    p.add_argument("--limite", type=int, default=30, help="máximo de resultados (0 = sin límite)")
    p.add_argument("--palabra-completa", action="store_true",
                   help="los términos latinos deben aparecer como palabra completa (no dentro de otra)")
    p.add_argument("--con-repetidos", action="store_true", help="no ocultar las novelas repetidas")
    p.add_argument("--motor", choices=list(traductor.ENGINES), default=traductor.DEFAULT_ENGINE,
                   help="motor de traducción gratuito: " + ", ".join(traductor.ENGINES))
    p.add_argument("--lotes", action="store_true", help="agrupar varios títulos por petición")
    p.add_argument("--uno-por-uno", action="store_true", help="un título por petición (más lento, más preciso)")
    p.add_argument("--retraducir", action="store_true", help="ignora las traducciones guardadas")
    p.add_argument("--pausa", type=float, default=None,
                   help="segundos mínimos entre peticiones (por defecto, la recomendada del motor)")
    p.add_argument("--traducir", action="store_true", help="traduce los títulos al español (Google Translate)")
    p = sub.add_parser("importar", help="importa páginas guardadas con el navegador (Ctrl+S) de los sitios compatibles")
    p.add_argument("rutas", nargs="+", help="archivos .html o carpetas")
    p = sub.add_parser("exportar", help="guarda el catálogo en un JSON para el userscript del navegador")
    p.add_argument("-o", "--salida", default="catalogo.json")
    sub.add_parser("estado", help="muestra cuántas novelas hay indexadas")
    a = ap.parse_args()
    try:
        if a.cmd == "buscar":
            cmd_buscar(a)
        elif a.cmd == "exportar":
            n, size = export_catalog(a.salida)
            print(f"Exportadas {n} novelas ({size / 1e6:.1f} MB) → {a.salida}")
        elif a.cmd == "importar":
            r, n, v = import_saved_pages(a.rutas)
            print(f"Páginas leídas: {r} · novelas vistas: {v} · nuevas: {n}")
        elif a.cmd == "estado":
            cmd_estado()
        else:
            run_gui()
    except KeyboardInterrupt:
        sys.exit("\nInterrumpido (lo importado hasta ahora queda guardado).")


if __name__ == "__main__":
    main()
