// ==UserScript==
// @name         Catálogo avanzado de novelas
// @namespace    buscador-novelas v1
// @version      1.0.0
// @description  Búsqueda avanzada (sinónimos, exclusiones, filtros, orden) sobre tu catálogo exportado, y títulos en español junto a las novelas de suduguu, washai, ixdzs8 y fictionzone.
// @match        https://www.suduguu.com/*
// @match        https://suduguu.com/*
// @match        https://book.washai.net/*
// @match        https://ixdzs8.com/*
// @match        https://www.ixdzs8.com/*
// @match        https://fictionzone.net/*
// @grant        GM.getValue
// @grant        GM.setValue
// @grant        GM.deleteValue
// @run-at       document-idle
// ==/UserScript==

/* Cómo se usa:
 *  1) En el buscador de escritorio: Catálogo → «Exportar catálogo (navegador)» → catalogo.json
 *  2) En cualquiera de los sitios: botón «🔎 Catálogo» (abajo a la derecha) → «Cargar catalogo.json».
 *     Se guarda en el navegador; para actualizarlo, exporta y carga de nuevo.
 *  Todo ocurre en tu equipo: el script no envía nada a ningún servidor.
 */

// =========================================================== núcleo (se puede probar con Node)
const Core = (() => {
  const normKey = s => String(s ?? "").trim().toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "");
  const ident = s => String(s ?? "").normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");

  function buildLookup(glossary) {
    const alias = new Map(), term = new Map();
    const push = (m, k, arr) => { if (!m.has(k)) m.set(k, []); m.get(k).push(...arr); };
    for (const [aliases, syns] of glossary || []) {
      for (const a of aliases) push(alias, normKey(a), syns);
      for (const t of syns) push(term, normKey(t), syns);
    }
    return { alias, term };
  }

  const uniq = items => {
    const seen = new Set(), out = [];
    for (const x of items) { const k = x.toLowerCase(); if (k && !seen.has(k)) { seen.add(k); out.push(x); } }
    return out;
  };

  // 'cultivación sistema|system -harén "frase"' -> {include: [[alt…]…], exclude: [[alt…]…]}
  function parseQuery(q, lookup, expand = true) {
    const include = [], exclude = [];
    q = q.replace(/[，,、；;]/g, " ");
    const rx = /(-?)"([^"]+)"|(-?)(\S+)/g;
    let m;
    while ((m = rx.exec(q))) {
      const neg = (m[1] || m[3]) === "-";
      const text = (m[2] || m[4] || "").trim();
      if (!text || text === "-") continue;
      let group = [];
      for (let alt of text.split(/[|｜]/)) {
        alt = alt.trim();
        if (!alt) continue;
        group.push(alt);
        if (expand && lookup && alt === alt.toLowerCase()) {   // con mayúsculas: exacto, sin sinónimos
          const k = normKey(alt);
          group.push(...(lookup.alias.get(k) || []), ...(lookup.term.get(k) || []));
        }
      }
      group = uniq(group);
      if (group.length) (neg ? exclude : include).push(group);
    }
    return { include, exclude };
  }

  const count = (s, sub) => {
    if (!sub) return 0;
    let n = 0, i = 0;
    while ((i = s.indexOf(sub, i)) !== -1) { n++; i += sub.length; }
    return n;
  };
  const isDone = st => (st || "").includes("完");

  function dedupe(results) {
    const byTitle = new Map();
    for (const r of results) {
      const k = ident(r.row.title);
      if (!byTitle.has(k)) byTitle.set(k, []);
      byTitle.get(k).push(r);
    }
    const groups = [];
    for (const [key, items] of byTitle) {
      if (!key) { items.forEach(r => groups.push([r])); continue; }
      const clusters = new Map();
      for (const r of items) {
        const a = ident(r.row.author);
        if (!clusters.has(a)) clusters.set(a, []);
        clusters.get(a).push(r);
      }
      const known = [...clusters.keys()].filter(a => a);
      if (clusters.has("") && known.length === 1) {
        clusters.get(known[0]).push(...clusters.get(""));
        clusters.delete("");
      }
      for (const [a, lst] of clusters) {
        if (a || !known.length) groups.push(lst);
        else lst.forEach(r => groups.push([r]));
      }
    }
    const cmp = (x, y) => { for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
    const quality = r => [r.row.words || 0, r.row.full ? 1 : 0, (r.row.description || "").length, r.score];
    const out = [];
    for (const grp of groups) {
      let best = grp[0];
      for (const r of grp) if (cmp(quality(r), quality(best)) > 0) best = r;
      const rank = grp.reduce((b, r) => (r.matched > b[0] || (r.matched === b[0] && r.score > b[1])) ? [r.matched, r.score] : b,
                              [grp[0].matched, grp[0].score]);
      out.push({ ...best, dups: grp.filter(r => r.row !== best.row).map(r => r.row), _rank: rank });
    }
    out.sort((a, b) => (b._rank[0] - a._rank[0]) || (b._rank[1] - a._rank[1]));
    return out;
  }

  // Si lo escrito lleva MAYÚSCULAS se busca exactamente así; todo en minúsculas ignora mayúsculas.
  // whole: palabra completa para términos que empiezan y terminan en letra/número latino.
  function matcher(alt, whole) {
    const cs = alt !== alt.toLowerCase();
    const needle = cs ? alt : alt.toLowerCase();
    let rx = null;
    if (whole && /^[A-Za-z0-9]/.test(needle) && /[A-Za-z0-9]$/.test(needle))
      rx = new RegExp("(?<![A-Za-z0-9])" + needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![A-Za-z0-9])", "g");
    return { needle, cs, rx };
  }
  const mcount = (m, orig, low) => {
    const s = m.cs ? orig : low;
    if (m.rx) { m.rx.lastIndex = 0; return (s.match(m.rx) || []).length; }
    return count(s, m.needle);
  };
  const mfind = (m, orig, low) => {
    const s = m.cs ? orig : low;
    if (m.rx) { m.rx.lastIndex = 0; const r = m.rx.exec(s); return r ? r.index : -1; }
    return s.indexOf(m.needle);
  };

  // opts: {mode:'todas'|'alguna'|N, status:null|'ongoing'|'done', sites:Set|null, minWords, maxWords, limit, dedupe, whole}
  function search(rows, include, exclude, opts = {}) {
    if (!include.length) return [];
    const { mode = "todas", status = null, sites = null, minWords = null, maxWords = null, limit = 0, whole = false } = opts;
    const cands = rows.filter(r => {
      if (sites && !sites.has(r.site)) return false;
      if (status === "done" && !isDone(r.status)) return false;
      if (status === "ongoing" && isDone(r.status)) return false;
      const w = r.words;
      if (minWords != null && (w == null || w < minWords)) return false;
      if (maxWords != null && (w == null || w > maxWords)) return false;
      return true;
    });
    const inc = include.map(g => g.map(a => matcher(a, whole)));
    const exc = exclude.map(g => g.map(a => matcher(a, whole)));
    const G = inc.length;
    const T = cands.map(r => [r.title || "", r.description || ""]);
    const L = T.map(([t, d]) => [t.toLowerCase(), d.toLowerCase()]);
    // los títulos y sinopsis traducidos también cuentan como texto buscable (para escribir en español)
    const E = cands.map(r => ((r.title_es || "") + " " + (r.description_es || "")));
    const EL = E.map(e => e.toLowerCase());
    const hits = T.map(([t, d], i) => inc.map(g => [
      g.reduce((s, m) => s + mcount(m, t, L[i][0]), 0),
      g.reduce((s, m) => s + mcount(m, d, L[i][1]) + mcount(m, E[i], EL[i]), 0)]));
    const df = inc.map((_, gi) => hits.filter(h => h[gi][0] || h[gi][1]).length);
    const idf = df.map(n => Math.log(1 + cands.length / (1 + n)));
    let need = mode === "todas" ? G : mode === "alguna" ? 1 : parseInt(mode, 10);
    if (!Number.isFinite(need)) need = G;
    need = Math.max(1, Math.min(need, G));
    let results = [];
    cands.forEach((r, i) => {
      const h = hits[i];
      const matched = [];
      for (let gi = 0; gi < G; gi++) if (h[gi][0] || h[gi][1]) matched.push(gi);
      if (matched.length < need) return;
      if (exc.length) {
        const full = T[i][0] + "\n" + T[i][1] + "\n" + E[i], fullL = L[i][0] + "\n" + L[i][1] + "\n" + EL[i];
        if (exc.some(g => g.some(m => mcount(m, full, fullL) > 0))) return;
      }
      const score = matched.reduce((s, gi) => s + idf[gi] * (1 + 0.5 * Math.log(1 + h[gi][1]) + (h[gi][0] ? 1.5 : 0)), 0);
      const desc = T[i][1], terms = [], snips = [];
      for (const gi of matched) {
        let best = inc[gi][0], bc = -1;
        for (const m of inc[gi]) { const c = mcount(m, desc, L[i][1]) + mcount(m, T[i][0], L[i][0]); if (c > bc) { bc = c; best = m; } }
        terms.push(best.needle);
        const pos = mfind(best, desc, L[i][1]);
        if (pos >= 0 && snips.length < 2) {
          const n = best.needle.length, a = Math.max(0, pos - 24), b = Math.min(desc.length, pos + n + 24);
          snips.push((a ? "…" : "") + desc.slice(a, pos) + "【" + desc.slice(pos, pos + n) + "】" +
                     desc.slice(pos + n, b) + (b < desc.length ? "…" : ""));
        }
      }
      results.push({ row: r, score, matched: matched.length, of: G, terms, snips });
    });
    results.sort((a, b) => (b.matched - a.matched) || (b.score - a.score));
    if (opts.dedupe !== false) results = dedupe(results);
    return limit ? results.slice(0, limit) : results;
  }

  // [sitio,id,título,título_es,autor,categoría,estado,万字,url,sinopsis,sinopsis_es,completa] -> objeto
  const rowFrom = a => ({ site: a[0], id: a[1], title: a[2], title_es: a[3], author: a[4], category: a[5],
                          status: a[6], words: a[7], url: a[8], description: a[9], description_es: a[10], full: !!a[11], updated: a[12] || "" });
  return { normKey, ident, buildLookup, parseQuery, search, dedupe, rowFrom };
})();

if (typeof module !== "undefined") module.exports = Core;

// =========================================================== interfaz (solo en el navegador)
if (typeof window !== "undefined" && typeof document !== "undefined" && typeof GM !== "undefined") {
  (async () => {
    const CHUNK = 1_500_000;
    const store = {
      async save(text) {
        const old = (await GM.getValue("cat_chunks", 0)) | 0;
        for (let i = 0; i < old; i++) await GM.deleteValue("cat_" + i);
        const n = Math.ceil(text.length / CHUNK);
        for (let i = 0; i < n; i++) await GM.setValue("cat_" + i, text.slice(i * CHUNK, (i + 1) * CHUNK));
        await GM.setValue("cat_chunks", n);
      },
      async load() {
        const n = (await GM.getValue("cat_chunks", 0)) | 0;
        if (!n) return null;
        let t = "";
        for (let i = 0; i < n; i++) t += (await GM.getValue("cat_" + i, ""));
        return t;
      },
    };

    const S = { rows: [], lookup: null, sites: {}, exported: "", index: new Map(), results: [], sort: { col: null, desc: false } };

    function ingest(text) {
      const d = JSON.parse(text);
      S.rows = d.novels.map(Core.rowFrom);
      S.lookup = Core.buildLookup(d.glossary);
      S.sites = d.sites || {};
      S.exported = d.exported || "";
      S.index = new Map(S.rows.map(r => [r.site + ":" + r.id, r]));
    }

    // ---------------- enlace de la página -> (sitio, id) ----------------
    function siteKey(host) {
      host = host.replace(/^www\./, "");
      return { "suduguu.com": "suduguu", "book.washai.net": "washai", "ixdzs8.com": "ixdzs", "fictionzone.net": "fictionzone" }[host] || null;
    }
    const HERE = siteKey(location.host);          // cada página solo muestra los resultados de su propio dominio
    function refFromHref(a) {
      let u; try { u = new URL(a.href, location.href); } catch { return null; }
      const site = siteKey(u.host); if (!site) return null;
      let m;
      if (site === "suduguu" && (m = u.pathname.match(/^\/(\d+)\/?$/))) return [site, m[1]];
      if (site === "washai" && (m = u.pathname.match(/^\/book\/(\d+)\.html$/))) return [site, m[1]];
      if (site === "ixdzs" && (m = u.pathname.match(/^\/read\/(\d+)\/?$/))) return [site, m[1]];
      if (site === "fictionzone" && (m = u.pathname.match(/^\/omniportal\/fq\/(\d+)\/?$/))) return [site, "fq-" + m[1]];
      return null;
    }

    // ---------------- títulos en español junto a cada novela ----------------
    let decorTimer = null;
    function decorate() {
      if (!S.index.size) return;
      for (const a of document.querySelectorAll("a[href]:not([data-ncs])")) {
        if (a.closest("#ncs-root")) continue;
        const ref = refFromHref(a); if (!ref) continue;
        const r = S.index.get(ref[0] + ":" + ref[1]);
        a.dataset.ncs = "1";
        if (!r || !(a.textContent || "").trim()) continue;
        const b = document.createElement("span");
        b.className = "ncs-badge";
        b.textContent = " 📖" + (r.title_es && r.title_es !== r.title ? " " + r.title_es : "");
        b.title = (r.description_es || r.description || "").slice(0, 600);
        b.style.cssText = "font-size:.8em;color:#0a7;background:#e8fff6;border-radius:4px;padding:0 4px;margin-left:4px;font-weight:normal;cursor:help";
        a.after(b);
      }
    }
    function scheduleDecor() { clearTimeout(decorTimer); decorTimer = setTimeout(decorate, 400); }

    // ---------------- panel ----------------
    const host = document.createElement("div");
    host.id = "ncs-root";
    host.style.cssText = "position:fixed;z-index:2147483647;right:12px;bottom:12px;font:13px/1.35 system-ui,sans-serif";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
<style>
  *{box-sizing:border-box}
  .fab{background:#1f6feb;color:#fff;border:0;border-radius:20px;padding:8px 14px;cursor:pointer;box-shadow:0 2px 8px #0004}
  .panel{display:none;width:min(900px,94vw);max-height:80vh;overflow:auto;background:#fff;color:#111;border:1px solid #bbb;border-radius:8px;padding:10px;box-shadow:0 6px 24px #0005}
  .panel.open{display:block}
  .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}
  input[type=text]{flex:1;min-width:220px;padding:5px}
  button,select{padding:4px 8px}
  .hint{color:#555;font-size:12px}
  table{border-collapse:collapse;width:100%;margin-top:6px}
  th{cursor:pointer;background:#f0f0f0;text-align:left;padding:4px;position:sticky;top:0;white-space:nowrap}
  td{border-top:1px solid #e3e3e3;padding:4px;vertical-align:top}
  a{color:#1f6feb;text-decoration:none} a:hover{text-decoration:underline}
  .snip{color:#444;font-size:12px} .dup{color:#888;font-size:11px}
  .num{text-align:right}
</style>
<button class="fab" id="fab">🔎 Catálogo</button>
<div class="panel" id="panel">
  <div class="row"><b>Catálogo avanzado</b><span class="hint" id="info"></span>
    <span style="flex:1"></span><button id="load">Cargar catalogo.json</button><button id="close">✕</button>
    <input type="file" id="file" accept=".json,application/json" style="display:none"></div>
  <div class="row"><input type="text" id="q" placeholder='cultivación sistema|system -harén "frase exacta"'>
    <button id="go">Buscar</button></div>
  <div class="row">
    <select id="mode"><option value="todas">Todas las palabras</option><option value="alguna">Alguna palabra</option><option value="2">Al menos 2</option></select>
    <select id="status"><option value="">Todos</option><option value="ongoing">En curso</option><option value="done">Finalizadas</option></select>
    <label><input type="checkbox" id="syn" checked> Sinónimos</label>
    <label><input type="checkbox" id="dd" checked> Ocultar repetidos</label>
    <label><input type="checkbox" id="whole"> Palabra completa</label>
    <label>万字 ≥ <input type="text" id="minw" style="width:55px;min-width:0"></label>
    <label>≤ <input type="text" id="maxw" style="width:55px;min-width:0"></label>
  </div>
  <div class="hint" id="expl">varias palabras = todas deben aparecer · a|b = una u otra · -palabra = excluir · "frase exacta" · con MAYÚSCULAS se busca exactamente así</div>
  <div id="out"></div>
</div>`;
    document.documentElement.appendChild(host);
    const $ = id => root.getElementById(id);
    const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

    function refreshInfo() {
      const name = S.sites[HERE] || location.host;
      const mine = S.rows.filter(r => r.site === HERE).length;
      $("info").textContent = S.rows.length
        ? `${name}: ${mine.toLocaleString()} novelas en el catálogo · exportado ${S.exported}`
        : "Sin catálogo: exporta desde el buscador y pulsa «Cargar catalogo.json».";
    }

    const COLS = [["title", "Título"], ["title_es", "Título (español)"],
                  ["category", "Categoría"], ["status", "Estado"], ["updated", "Actualizada"]];
    function cellValue(r, c) {
      const w = r.row;
      switch (c) {
        case "pts": return r.score;
        case "words": return w.words;
        default: return w[c] || "";
      }
    }
    function sortBy(col) {
      const desc = S.sort.col === col ? !S.sort.desc : (col === "pts" || col === "words");
      S.sort = { col, desc };
      const keyed = S.results.map(r => [cellValue(r, col), r]);
      const full = keyed.filter(k => k[0] !== "" && k[0] != null);
      const empty = keyed.filter(k => k[0] === "" || k[0] == null);
      full.sort((a, b) => (typeof a[0] === "number" ? a[0] - b[0] : String(a[0]).toLowerCase().localeCompare(String(b[0]).toLowerCase())) * (desc ? -1 : 1));
      S.results = [...full, ...empty].map(k => k[1]);
      render();
    }
    function render() {
      const out = $("out");
      if (!S.results.length) { out.innerHTML = '<p class="hint">Sin resultados. Prueba «Alguna palabra» o menos términos.</p>'; return; }
      const head = COLS.map(([c, t]) => `<th data-col="${c}">${t}${S.sort.col === c ? (S.sort.desc ? " ▼" : " ▲") : ""}</th>`).join("");
      const rows = S.results.map(r => {
        const w = r.row;
        const dup = r.dups && r.dups.length ? ` <span class="dup">(+${r.dups.length} repetidos)</span>` : "";
        return `<tr>
          <td><a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.title)}</a>${dup}
              <div class="snip">${r.snips.map(esc).join("<br>")}</div></td>
          <td>${esc(w.title_es)}</td><td>${esc(w.category)}</td><td>${esc(w.status)}</td>
          <td>${esc(w.updated)}</td></tr>`;
      }).join("");
      out.innerHTML = `<div class="hint">${S.results.length} resultados</div><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
      out.querySelectorAll("th").forEach(th => th.addEventListener("click", () => sortBy(th.dataset.col)));
    }
    function doSearch() {
      if (!S.rows.length) { $("out").innerHTML = '<p class="hint">Primero carga el catálogo.</p>'; return; }
      const { include, exclude } = Core.parseQuery($("q").value, S.lookup, $("syn").checked);
      if (!include.length) { $("out").innerHTML = '<p class="hint">Escribe al menos una palabra clave.</p>'; return; }
      const sites = new Set([HERE]);                 // solo el dominio de esta página
      const num = id => { const v = parseFloat($(id).value.replace(",", ".")); return Number.isFinite(v) ? v : null; };
      S.results = Core.search(S.rows, include, exclude, {
        mode: $("mode").value, status: $("status").value || null, sites,
        minWords: num("minw"), maxWords: num("maxw"), limit: 0, dedupe: $("dd").checked, whole: $("whole").checked });
      S.sort = { col: null, desc: false };
      $("expl").textContent = "Buscando: " + include.map(g => "(" + g.slice(0, 6).join(" | ") + (g.length > 6 ? " …" : "") + ")").join("  Y  ") +
        (exclude.length ? "   EXCLUYENDO " + exclude.map(g => g[0]).join(", ") : "");
      render();
    }

    $("fab").onclick = () => $("panel").classList.toggle("open");
    $("close").onclick = () => $("panel").classList.remove("open");
    $("load").onclick = () => $("file").click();
    $("go").onclick = doSearch;
    $("q").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
    $("file").onchange = async e => {
      const f = e.target.files[0]; if (!f) return;
      $("info").textContent = "Cargando…";
      try {
        const text = await f.text();
        ingest(text);
        await store.save(text);
        refreshInfo(); decorate();
      } catch (err) { $("info").textContent = "No se pudo cargar el archivo: " + err.message; }
    };

    // ---------------- arranque ----------------
    try { const t = await store.load(); if (t) ingest(t); } catch (e) { console.warn("[catálogo] no se pudo leer lo guardado", e); }
    refreshInfo();
    decorate();
    new MutationObserver(scheduleDecor).observe(document.body, { childList: true, subtree: true });
  })();
}
