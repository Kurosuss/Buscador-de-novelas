"""Pruebas rápidas sin red:  python tests/test_basico.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import novel_search as ns  # noqa: E402


def rows():
    data = [("Iron AI Hero", "The AI wakes up in a remote main hall."),
            ("Maid story", "an ai assistant helps a maid"),
            ("Harem Lord", "No harem, only Harem tags"),
            ("春天", "修仙 AI 系统 的故事"),
            ("夏天", "末世 重生 的故事")]
    return [{"site": "suduguu", "id": str(i), "title": t, "description": d, "author": "", "status": "", "words": None}
            for i, (t, d) in enumerate(data)]


def titles(q, **kw):
    inc, exc = ns.parse_query(q)
    return sorted(r["row"]["title"] for r in ns.search(rows(), inc, exc, limit=None, dedupe=False, **kw))


def test_mayusculas():
    assert titles("AI") == ["Iron AI Hero", "春天"]                 # con mayúsculas: exacto
    assert "Maid story" in titles("ai")                              # minúsculas: ignora mayúsculas


def test_palabra_completa():
    assert titles("ai", whole=True) == ["Iron AI Hero", "Maid story", "春天"]


def test_exclusion_y_sinonimos():
    assert titles("harem -Harem") == []
    assert titles("cultivación") == ["春天"]                          # sinónimo en chino (修仙)


def test_importar_y_exportar():
    d = tempfile.mkdtemp()
    page = os.path.join(d, "lista.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write('<html><head><link rel="canonical" href="https://www.suduguu.com/zuixin/"></head><body>'
                '<h3><a href="/51/">某某传</a></h3><p>连载中 玄幻小说 作者：甲</p></body></html>')
    db = os.path.join(d, "c.db")
    r, n, v = ns.import_saved_pages([d], db_path=db, log=lambda m: None)
    assert (r, n, v) == (1, 1, 1)
    assert ns.export_catalog(os.path.join(d, "c.json"), db_path=db)[0] == 1


def test_traduccion_en_lotes():
    import sqlite3
    import traductor as T
    calls = []

    def fake(text):
        calls.append(text)
        return "\n".join("ES:" + ln for ln in text.split("\n"))
    rs = [{"site": "s", "id": str(i), "title": f"标题{i}"} for i in range(120)]
    e = T.ENGINES[T.DEFAULT_ENGINE]
    done, failed, reason = T.translate_rows(sqlite3.connect(":memory:"), rs, fake, engine=e, pause=0,
                                            one_by_one=False, log=lambda m: None)
    assert (done, failed, reason) == (120, 0, None)
    assert all(r["title_es"] == "ES:" + r["title"] for r in rs)
    assert len(calls) < 10                                           # en lotes, no uno por uno


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
