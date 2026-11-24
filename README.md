# Buscador de novelas

Búsqueda avanzada sobre **tu propio catálogo** de novelas web chinas: palabras clave con sinónimos
(escribes «cultivación» y también busca 修仙, 修炼…), exclusiones, frases exactas, respeto de mayúsculas,
filtros, columnas ordenables, ocultar repetidos y traducción de títulos al español.

> **Este programa no descarga novelas ni se conecta a ningún sitio para obtener el catálogo.**
> Tú guardas las páginas con tu navegador (Ctrl+S) y el programa las lee desde tu disco.
> El catálogo vive solo en tu equipo.

## Instalación

**Con el programa compilado (Windows):** descarga `BuscadorNovelas.zip` desde *Releases*, descomprímelo y abre
`BuscadorNovelas\BuscadorNovelas.exe` (deja la carpeta completa, el .exe necesita los archivos que lo acompañan).
Es un programa sin firma digital: Windows SmartScreen puede avisar de que no lo reconoce y algún antivirus
puede dar un falso positivo (es habitual en programas empaquetados con PyInstaller). Cada versión indica su
huella SHA-256 y el enlace a su análisis en VirusTotal para que lo verifiques. Si prefieres no fiarte de un
ejecutable, ejecuta el código fuente (abajo): es corto y legible.

**Desde el código:** Python 3.9+ y

```
pip install -r requirements.txt
python novel_search.py
```

Para generar el programa tú mismo: `build.bat` (crea `dist\BuscadorNovelas\` y el `.zip`).

## Uso

1. **Catálogo → Importar páginas guardadas.** En tu navegador abre una página de listado o la ficha de una
   novela, guárdala con **Ctrl+S → «Página web, completa»** y **sin traducir con el navegador**.
   Importa uno o muchos archivos, o una carpeta entera.

   | Sitio | Listados | Fichas de novela (sinopsis completa) |
   |---|---|---|
   | suduguu.com | `/zuixin/` | `/ID/` |
   | book.washai.net | `/book/bookclass.html` | — |
   | ixdzs8.com | `/sort/N/` | `/read/ID/` |
   | fictionzone.net | `/browse` | — |

2. **Buscar.** Ejemplos de consulta:

   | Escribes | Significa |
   |---|---|
   | `修仙 系统` (cultivación sistema) | deben aparecer las dos ideas |
   | `系统\|system` (sistema o system) | una u otra |
   | `-后宫` (excluir «harén») | excluir esa palabra |
   | `"都市修仙"` («cultivación urbana») | frase literal |
   | `AI` | con **mayúsculas** se busca *exactamente* así (sin sinónimos y distinguiendo mayúsculas) |
   | `ai` + casilla «Palabra completa» | no encuentra «m**ai**n» ni «vill**ai**n» |

3. **Traducir títulos al español** (opcional). El motor por defecto, **«Google rápido (como Calibre)»**, usa la
   misma vía que el plugin Ebook Translator de Calibre: manda los títulos en lotes y hace varias peticiones a la
   vez, así que cientos de títulos se traducen en segundos. Si Google te bloquea, sube la «Pausa»; si algún título
   sale mezclado con otro, marca «Uno por uno». Las traducciones se guardan y no se repiten.

4. **Exportar catálogo (navegador):** genera `catalogo.json` para el userscript de `userscript/`
   (Violentmonkey o Tampermonkey), que añade el mismo buscador y los títulos en español dentro de las propias páginas.

Línea de comandos: `python novel_search.py --help` (`importar`, `buscar`, `exportar`, `estado`).

## Privacidad

- El catálogo (`~/.novel_grabber/catalogo.db`) y las exportaciones se quedan en tu equipo. No hay telemetría.
- **La traducción es lo único que usa internet:** envía los *títulos* a Google Translate, Lingva o MyMemory,
  según el motor que elijas. Si no quieres eso, no uses el botón de traducir.

## Avisos

- Herramienta independiente: **no tiene relación** con ninguno de los sitios mencionados ni con sus marcas.
  Los nombres aparecen solo para indicar qué páginas guardadas entiende.
- Úsala respetando los términos de uso de cada sitio y los derechos de autor de las obras. Las sinopsis y los
  títulos pertenecen a sus autores y editores: **no publiques ni compartas tu catálogo**.
- Los servicios de traducción gratuitos son de terceros, tienen cuotas y pueden dejar de funcionar.
- Si un sitio cambia su diseño, puede que haya que actualizar la lectura de sus páginas.
- Se ofrece «tal cual», sin garantía (ver `LICENSE`).

## Desarrollo

`python tests/test_basico.py` ejecuta las pruebas sin red. Licencia MIT (ver `LICENSE`).
