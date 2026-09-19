#!/usr/bin/env python3
"""
Brújula Educativa — Servidor MCP
Fundación Startin

Expone los datos educativos abiertos de Colombia como herramientas MCP para que
un agente en Microsoft Foundry las consulte.

PRINCIPIOS DE DISEÑO — leer antes de agregar una herramienta:

1. NUNCA se devuelve información de un estudiante. Los microdatos son anónimos y
   aquí solo salen agregados. Un colegio con menos de MUESTRA_MINIMA evaluados no
   entrega promedio: entrega una advertencia.

2. TODA respuesta trae `fuente` y `corte`. Si el dato más reciente es de hace tres
   años, la respuesta lo dice. Es la diferencia entre una herramienta citable y un
   chatbot que suena seguro.

3. TODA respuesta puede traer `advertencias`. Un 0 % de deserción en un municipio
   con cobertura baja no es un logro, es un vacío de reporte, y el agente tiene que
   decirlo en vez de celebrarlo.

4. El agente NO dibuja gráficas. Cada herramienta devuelve un bloque `vis` que
   elige un componente de un catálogo cerrado y le pasa los datos. El front tiene
   esos componentes prefabricados en la marca Startin. Así no se ejecuta código
   generado por IA en un sitio público, no se gastan tokens dibujando, y todas las
   respuestas se ven igual de bien.

5. Las advertencias viajan DENTRO del bloque `vis`, no aparte. Una gráfica esconde
   las salvedades mejor que un párrafo; si la nota no va pegada al dibujo, se pierde.

Ejecutar:
    pip install "mcp[cli]" duckdb
    python server.py --datos ./data --puerto 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import unicodedata
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.fastmcp import FastMCP

LOG = logging.getLogger("brujula")

MUESTRA_MINIMA = 10          # evaluados mínimos para publicar un promedio
MAX_RESULTADOS = 15          # tope de coincidencias en una búsqueda

# Catálogo cerrado de visualizaciones. El front tiene un componente por cada una.
VIS_CIFRAS = "cifras"                 # tarjetas con números sueltos
VIS_BARRAS_COMP = "barras_comparadas" # tu colegio vs municipio vs país
VIS_LINEA = "linea_tiempo"            # evolución de un indicador
VIS_BARRAS_NIVEL = "barras_nivel"     # transición / primaria / secundaria / media
VIS_MEDIDOR = "medidor_brecha"        # porcentaje contra una referencia

AREAS = {
    "prom_lectura": "Lectura crítica",
    "prom_matematicas": "Matemáticas",
    "prom_sociales": "Sociales y ciudadanas",
    "prom_naturales": "Ciencias naturales",
    "prom_ingles": "Inglés",
}

mcp = FastMCP("brujula-educativa")
_con: duckdb.DuckDBPyConnection | None = None
_datos: Path = Path("./data")


# --------------------------------------------------------------------------- #
# Infraestructura
# --------------------------------------------------------------------------- #

def conexion() -> duckdb.DuckDBPyConnection:
    """Una sola conexión DuckDB que lee los parquet directamente desde disco."""
    global _con
    if _con is None:
        _con = duckdb.connect(database=":memory:")
        for vista, archivo in [
            ("saber11_agg", "saber11_agregado.parquet"),
            ("saber11_col", "saber11_colegios.parquet"),
            ("men", "men_municipios.parquet"),
            ("cpe", "computadores_educar.parquet"),
        ]:
            ruta = _datos / archivo
            if ruta.exists():
                _con.execute(f"CREATE VIEW {vista} AS SELECT * FROM read_parquet('{ruta}')")
                LOG.info("Vista %s lista desde %s", vista, archivo)
            else:
                LOG.warning("Falta %s — las herramientas que lo usan responderán sin datos", archivo)
    return _con


def sin_tildes(texto: str) -> str:
    """Para que 'Bogota' encuentre 'BOGOTÁ D.C.' y 'Magui' encuentre 'MAGÜÍ'."""
    t = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def vacio(mensaje: str, sugerencia: str = "") -> dict[str, Any]:
    return {"encontrado": False, "mensaje": mensaje, "sugerencia": sugerencia}


def redondear(valor: Any, decimales: int = 1) -> float | None:
    try:
        if valor is None:
            return None
        return round(float(valor), decimales)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Herramientas
# --------------------------------------------------------------------------- #

@mcp.tool()
def buscar_colegio(nombre: str, municipio: str = "", departamento: str = "") -> dict[str, Any]:
    """
    Busca colegios por nombre, opcionalmente acotado a un municipio o departamento.

    Úsala SIEMPRE antes de resultados_colegio: el usuario dice "el Gimnasio
    Femenino", no un código DANE. Devuelve las coincidencias con su código para
    que la siguiente llamada sea exacta. Si hay varias, pregúntale al usuario
    cuál es en vez de adivinar.
    """
    con = conexion()
    patron = f"%{sin_tildes(nombre)}%"
    filtros = ["strip_accents(upper(nombre_sede)) LIKE ?"]
    params: list[Any] = [patron]

    if municipio:
        filtros.append("strip_accents(upper(municipio)) LIKE ?")
        params.append(f"%{sin_tildes(municipio)}%")
    if departamento:
        filtros.append("strip_accents(upper(departamento)) LIKE ?")
        params.append(f"%{sin_tildes(departamento)}%")

    sql = f"""
        SELECT cod_dane_sede, any_value(nombre_sede) AS nombre,
               any_value(municipio) AS municipio, any_value(departamento) AS departamento,
               any_value(naturaleza) AS naturaleza,
               max(anio) AS ultimo_anio, sum(evaluados) AS total_evaluados
        FROM saber11_agg
        WHERE {' AND '.join(filtros)} AND cod_dane_sede IS NOT NULL
        GROUP BY cod_dane_sede
        ORDER BY total_evaluados DESC
        LIMIT {MAX_RESULTADOS}
    """
    try:
        filas = con.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        LOG.error("buscar_colegio: %s", exc)
        return vacio("No se pudo consultar el índice de colegios.")

    if not filas:
        return vacio(
            f"No encontré ningún colegio que coincida con «{nombre}».",
            "Prueba con menos palabras del nombre, o agrega el municipio.",
        )

    return {
        "encontrado": True,
        "coincidencias": [
            {
                "cod_dane_sede": f[0], "nombre": f[1], "municipio": f[2],
                "departamento": f[3], "naturaleza": f[4], "ultimo_anio": f[5],
            }
            for f in filas
        ],
        "nota": "Si hay más de una coincidencia, pregunta al usuario cuál antes de continuar.",
    }


@mcp.tool()
def resultados_colegio(cod_dane_sede: str, anio: int = 0) -> dict[str, Any]:
    """
    Resultados de Saber 11 de un colegio, comparados contra su municipio y el país.

    Devuelve el promedio de las cinco áreas, cuántos estudiantes presentaron, y la
    misma cifra para el municipio y el total nacional del mismo año, para que la
    respuesta diga si el colegio está por encima o por debajo y de qué.
    """
    con = conexion()
    filtro_anio = "AND anio = ?" if anio else ""
    params: list[Any] = [cod_dane_sede] + ([anio] if anio else [])

    fila = con.execute(f"""
        SELECT anio, any_value(nombre_sede), any_value(municipio), any_value(departamento),
               any_value(cod_municipio), any_value(naturaleza), sum(evaluados),
               {', '.join(f'avg({c})' for c in AREAS)}
        FROM saber11_agg
        WHERE cod_dane_sede = ? {filtro_anio}
        GROUP BY anio ORDER BY anio DESC LIMIT 1
    """, params).fetchone()

    if not fila:
        return vacio(
            "No hay resultados publicados para ese colegio en el periodo pedido.",
            "Recuerda que 2023 no existe como dato público por colegio.",
        )

    (anio_dato, nombre, municipio, departamento, cod_mcpio, naturaleza, evaluados, *promedios) = fila
    advertencias: list[str] = []

    if evaluados is not None and evaluados < MUESTRA_MINIMA:
        advertencias.append(
            f"Solo {int(evaluados)} estudiantes presentaron la prueba en esta sede. "
            "Con una muestra tan pequeña el promedio no es comparable con el de otros colegios."
        )

    # Referencias del mismo año: municipio y país.
    ref_mcpio = con.execute(f"""
        SELECT {', '.join(f'avg({c})' for c in AREAS)} FROM saber11_agg
        WHERE cod_municipio = ? AND anio = ?
    """, [cod_mcpio, anio_dato]).fetchone()
    ref_pais = con.execute(f"""
        SELECT {', '.join(f'avg({c})' for c in AREAS)} FROM saber11_agg WHERE anio = ?
    """, [anio_dato]).fetchone()

    series = []
    for i, (campo, etiqueta) in enumerate(AREAS.items()):
        series.append({
            "area": etiqueta,
            "colegio": redondear(promedios[i]),
            "municipio": redondear(ref_mcpio[i] if ref_mcpio else None),
            "pais": redondear(ref_pais[i] if ref_pais else None),
        })

    return {
        "encontrado": True,
        "colegio": {
            "nombre": nombre, "cod_dane_sede": cod_dane_sede, "municipio": municipio,
            "departamento": departamento, "naturaleza": naturaleza,
        },
        "anio": anio_dato,
        "evaluados": int(evaluados) if evaluados else None,
        "areas": series,
        "fuente": "ICFES — Resultados agregados Saber 11 por establecimiento",
        "corte": str(anio_dato),
        "advertencias": advertencias,
        "vis": {
            "tipo": VIS_BARRAS_COMP,
            "titulo": f"{nombre} frente a {municipio} y al país",
            "subtitulo": f"Saber 11 · {anio_dato} · {int(evaluados) if evaluados else '—'} evaluados",
            "categorias": [s["area"] for s in series],
            "series": [
                {"nombre": "Este colegio", "valores": [s["colegio"] for s in series], "enfasis": True},
                {"nombre": municipio, "valores": [s["municipio"] for s in series]},
                {"nombre": "Colombia", "valores": [s["pais"] for s in series]},
            ],
            "nota_fuente": f"ICFES · corte {anio_dato}",
            "advertencias": advertencias,
        },
    }


@mcp.tool()
def indicadores_municipio(municipio: str = "", cod_municipio: str = "", anio: int = 0) -> dict[str, Any]:
    """
    Cobertura, deserción, aprobación y repitencia de un municipio, desagregadas por
    nivel educativo (transición, primaria, secundaria y media).

    Es la herramienta para "¿cómo va la educación en mi municipio?" y para detectar
    en qué nivel exactamente se rompe la trayectoria de los estudiantes.
    """
    con = conexion()
    if cod_municipio:
        cond, params = "cod_municipio = ?", [cod_municipio]
    elif municipio:
        cond, params = "strip_accents(upper(municipio)) LIKE ?", [f"%{sin_tildes(municipio)}%"]
    else:
        return vacio("Indica el municipio por nombre o por código DANE.")

    if anio:
        cond += " AND anio = ?"
        params.append(anio)

    fila = con.execute(f"""
        SELECT anio, municipio, departamento, cod_municipio, poblacion_5_16,
               cobertura_neta, cobertura_neta_transicion, cobertura_neta_primaria,
               cobertura_neta_secundaria, cobertura_neta_media,
               desercion, desercion_primaria, desercion_secundaria, desercion_media,
               repitencia, repitencia_primaria, repitencia_secundaria, repitencia_media,
               aprobacion
        FROM men WHERE {cond} ORDER BY anio DESC LIMIT 1
    """, params).fetchone()

    if not fila:
        return vacio(f"No encontré datos municipales para «{municipio or cod_municipio}».")

    (a, nom, depto, cod, pobl, cn, cn_t, cn_p, cn_s, cn_m,
     des, des_p, des_s, des_m, rep, rep_p, rep_s, rep_m, apr) = fila

    advertencias: list[str] = []
    if des is not None and float(des) == 0 and cn is not None and float(cn) < 80:
        advertencias.append(
            "La deserción aparece reportada en 0 %. En un municipio con esta cobertura "
            "ese valor es improbable y suele indicar un vacío en el reporte, no ausencia "
            "de deserción. Conviene contrastarlo con la Secretaría de Educación."
        )

    niveles = [
        {"nivel": "Transición", "cobertura": redondear(cn_t)},
        {"nivel": "Primaria", "cobertura": redondear(cn_p), "desercion": redondear(des_p), "repitencia": redondear(rep_p)},
        {"nivel": "Secundaria", "cobertura": redondear(cn_s), "desercion": redondear(des_s), "repitencia": redondear(rep_s)},
        {"nivel": "Media", "cobertura": redondear(cn_m), "desercion": redondear(des_m), "repitencia": redondear(rep_m)},
    ]

    return {
        "encontrado": True,
        "municipio": nom, "departamento": depto, "cod_municipio": cod, "anio": a,
        "poblacion_5_16": int(pobl) if pobl else None,
        "cobertura_neta_total": redondear(cn),
        "desercion_total": redondear(des),
        "repitencia_total": redondear(rep),
        "aprobacion_total": redondear(apr),
        "por_nivel": niveles,
        "fuente": "Ministerio de Educación Nacional — Estadísticas por municipio",
        "corte": str(a),
        "advertencias": advertencias,
        "vis": {
            "tipo": VIS_BARRAS_NIVEL,
            "titulo": f"Cobertura neta por nivel en {nom}",
            "subtitulo": f"{depto} · {a} · población 5 a 16 años: {int(pobl) if pobl else '—'}",
            "categorias": [n["nivel"] for n in niveles],
            "series": [{"nombre": "Cobertura neta (%)", "valores": [n["cobertura"] for n in niveles]}],
            "referencia": {"nombre": "Total del municipio", "valor": redondear(cn)},
            "nota_fuente": f"MEN · corte {a}",
            "advertencias": advertencias,
        },
    }


@mcp.tool()
def brecha_digital(cod_municipio: str = "", municipio: str = "") -> dict[str, Any]:
    """
    Brecha digital del municipio medida por dos vías independientes: lo que el
    Estado entregó (Computadores Para Educar) y lo que los estudiantes declaran
    tener en casa (Saber 11).

    Las dos medidas juntas son lo que ninguna otra fuente pública ofrece: permiten
    distinguir entre un territorio al que llegaron equipos y uno donde los
    estudiantes efectivamente tienen con qué estudiar.
    """
    con = conexion()
    if not cod_municipio and municipio:
        f = con.execute(
            "SELECT cod_municipio FROM men WHERE strip_accents(upper(municipio)) LIKE ? LIMIT 1",
            [f"%{sin_tildes(municipio)}%"],
        ).fetchone()
        if not f:
            return vacio(f"No encontré el municipio «{municipio}».")
        cod_municipio = f[0]
    if not cod_municipio:
        return vacio("Indica el municipio por nombre o por código DANE.")

    terminal = con.execute("""
        SELECT anio, ninos_por_terminal, pc_entregados_mintic,
               tabletas_mintic_estudiantes, docentes_formados
        FROM cpe WHERE cod_municipio = ? ORDER BY anio DESC LIMIT 1
    """, [cod_municipio]).fetchone()

    hogar = con.execute("""
        SELECT anio, any_value(municipio),
               sum(con_internet) * 100.0 / nullif(sum(evaluados), 0),
               sum(con_computador) * 100.0 / nullif(sum(evaluados), 0),
               sum(evaluados)
        FROM saber11_col WHERE cod_municipio = ?
        GROUP BY anio ORDER BY anio DESC LIMIT 1
    """, [cod_municipio]).fetchone()

    nacional = con.execute("""
        SELECT sum(con_internet) * 100.0 / nullif(sum(evaluados), 0)
        FROM saber11_col WHERE anio = ?
    """, [hogar[0]]).fetchone() if hogar else None

    advertencias: list[str] = []
    if hogar and hogar[0] and int(hogar[0]) <= 2022:
        advertencias.append(
            f"Los datos de conectividad en el hogar son de {hogar[0]}: los microdatos "
            "de Saber 11 no se han publicado después de 2022. La situación real hoy "
            "puede ser distinta."
        )
    if not terminal:
        advertencias.append("Este municipio no registra entregas de Computadores Para Educar.")

    pct_internet = redondear(hogar[2]) if hogar else None
    pct_nacional = redondear(nacional[0]) if nacional and nacional[0] else None

    return {
        "encontrado": True,
        "cod_municipio": cod_municipio,
        "municipio": hogar[1] if hogar else None,
        "equipamiento": {
            "anio": terminal[0] if terminal else None,
            "ninos_por_terminal": redondear(terminal[1]) if terminal else None,
            "pc_entregados": int(terminal[2]) if terminal and terminal[2] else None,
            "tabletas_estudiantes": int(terminal[3]) if terminal and terminal[3] else None,
            "docentes_formados": int(terminal[4]) if terminal and terminal[4] else None,
        } if terminal else None,
        "hogar": {
            "anio": hogar[0], "pct_con_internet": pct_internet,
            "pct_con_computador": redondear(hogar[3]), "base_estudiantes": int(hogar[4] or 0),
        } if hogar else None,
        "referencia_nacional_internet": pct_nacional,
        "fuente": "MinTIC — Computadores Para Educar · ICFES — Microdatos Saber 11",
        "advertencias": advertencias,
        "vis": {
            "tipo": VIS_MEDIDOR,
            "titulo": f"Estudiantes con internet en casa — {hogar[1] if hogar else cod_municipio}",
            "valor": pct_internet,
            "unidad": "%",
            "referencia": {"nombre": "Promedio nacional", "valor": pct_nacional},
            "secundario": {
                "etiqueta": "Niños por computador",
                "valor": redondear(terminal[1]) if terminal else None,
            },
            "nota_fuente": f"ICFES · corte {hogar[0] if hogar else '—'}",
            "advertencias": advertencias,
        },
    }


@mcp.tool()
def evolucion_municipio(cod_municipio: str, indicador: str = "desercion") -> dict[str, Any]:
    """
    Serie histórica de un indicador municipal, de 2011 en adelante.

    indicador admite: cobertura_neta, desercion, repitencia, aprobacion.
    Sirve para responder "¿está mejorando o empeorando?", que es la pregunta que
    de verdad importa en política pública y que un dato de un solo año no responde.
    """
    permitidos = {"cobertura_neta", "desercion", "repitencia", "aprobacion"}
    if indicador not in permitidos:
        return vacio(f"Indicador no reconocido. Usa uno de: {', '.join(sorted(permitidos))}.")

    con = conexion()
    filas = con.execute(f"""
        SELECT anio, {indicador}, any_value(municipio) OVER () FROM men
        WHERE cod_municipio = ? AND {indicador} IS NOT NULL ORDER BY anio
    """, [cod_municipio]).fetchall()

    if not filas:
        return vacio("No hay serie histórica para ese municipio e indicador.")

    nombre = filas[0][2]
    anios = [f[0] for f in filas]
    valores = [redondear(f[1]) for f in filas]

    return {
        "encontrado": True,
        "municipio": nombre, "indicador": indicador,
        "serie": [{"anio": a, "valor": v} for a, v in zip(anios, valores)],
        "primer_anio": anios[0], "ultimo_anio": anios[-1],
        "fuente": "Ministerio de Educación Nacional — Estadísticas por municipio",
        "advertencias": [],
        "vis": {
            "tipo": VIS_LINEA,
            "titulo": f"{indicador.replace('_', ' ').capitalize()} en {nombre}",
            "subtitulo": f"{anios[0]}–{anios[-1]}",
            "categorias": anios,
            "series": [{"nombre": nombre, "valores": valores, "enfasis": True}],
            "nota_fuente": f"MEN · {anios[0]}–{anios[-1]}",
            "advertencias": [],
        },
    }


@mcp.tool()
def comparar_ocde(dominio: str = "lectura") -> dict[str, Any]:
    """
    Posición de Colombia frente al promedio de la OCDE en PISA.

    dominio admite: lectura, matematicas, ciencias.

    IMPORTANTE: PISA es una prueba MUESTRAL que caracteriza al país. No dice nada
    sobre un colegio ni sobre un municipio, y no debe mezclarse con los resultados
    de Saber 11. Si el usuario pregunta por su colegio, usa resultados_colegio.
    """
    ruta = _datos / "pisa_colombia.json"
    if not ruta.exists():
        return vacio(
            "La tabla de PISA todavía no está cargada en este despliegue.",
            "Debe llenarse a mano desde el informe oficial de la OCDE: no existe una "
            "API de PISA, así que los valores se curan y se citan uno por uno.",
        )

    tabla = json.loads(ruta.read_text(encoding="utf-8"))
    serie = tabla.get(dominio)
    if not serie:
        return vacio(f"No hay datos cargados para el dominio «{dominio}».")

    return {
        "encontrado": True,
        "dominio": dominio,
        "ciclos": serie["ciclos"],
        "colombia": serie["colombia"],
        "ocde": serie["ocde"],
        "fuente": tabla.get("fuente", "OCDE — PISA"),
        "corte": tabla.get("corte", ""),
        "advertencias": [
            "PISA es una prueba muestral del país. No permite conclusiones sobre un "
            "colegio o un municipio en particular."
        ],
        "vis": {
            "tipo": VIS_LINEA,
            "titulo": f"Colombia frente a la OCDE en {dominio}",
            "categorias": serie["ciclos"],
            "series": [
                {"nombre": "Colombia", "valores": serie["colombia"], "enfasis": True},
                {"nombre": "Promedio OCDE", "valores": serie["ocde"]},
            ],
            "nota_fuente": tabla.get("fuente", "OCDE — PISA"),
            "advertencias": ["Prueba muestral: caracteriza al país, no a un colegio."],
        },
    }


# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor MCP de Brújula Educativa")
    parser.add_argument("--datos", type=Path, default=Path("./data"))
    parser.add_argument("--puerto", type=int, default=8080)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    global _datos
    _datos = args.datos
    conexion()  # falla temprano si faltan los parquet, no en la primera pregunta

    # HTTP+SSE, no stdio: stdio solo sirve para desarrollo local.
    mcp.settings.port = args.puerto
    mcp.settings.host = "0.0.0.0"
    LOG.info("Brújula MCP escuchando en el puerto %s", args.puerto)
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
