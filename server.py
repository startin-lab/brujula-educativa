#!/usr/bin/env python3
"""
Brújula Educativa — Servidor MCP
Fundación Startin

Expone los datos educativos abiertos de Colombia como herramientas MCP para que
un agente en Microsoft Foundry las consulte.

PRINCIPIOS DE DISEÑO — leer antes de agregar una herramienta:

1. TODA consulta de datos ocurre DENTRO DE UN TERRITORIO. No hay forma de pedir
   "el país" por la puerta principal. Quien pregunta elige primero departamento y
   municipio, y el agente trabaja ahí. Tres razones, en orden de peso:

     · Un diagnóstico es territorial. "¿Cómo está la educación en Colombia?" no
       tiene respuesta útil; "¿cómo está Soacha frente a Cundinamarca?" sí.
     · Comparar contra el promedio nacional es comparar contra Bogotá. La
       referencia que sirve para focalizar esfuerzos es el departamento.
     · Una consulta acotada es barata y predecible. Es lo que permite mantener
       esto abierto sin que el costo dependa de cuánta gente pregunte.

   Las herramientas nacionales existen, pero exigen un token que el proxy inyecta
   solo para las organizaciones con acceso completo. El agente no puede
   inventarlo, y sin él la herramienta responde que no, no adivina.

2. NUNCA se devuelve información de un estudiante. Los microdatos son anónimos y
   aquí solo salen agregados. Una sede con menos de MUESTRA_MINIMA evaluados no
   entrega promedio: entrega una advertencia.

3. TODA respuesta trae `fuente` y `corte`. Si el dato más reciente es de hace tres
   años, la respuesta lo dice. Es la diferencia entre una herramienta citable y un
   chatbot que suena seguro.

4. LAS SEÑALES NO SON HALLAZGOS. Una señal dice dónde revisar. El agente tiene
   prohibido presentarla como conclusión, y cada respuesta que las trae lleva esa
   advertencia pegada.

5. El agente NO dibuja gráficas. Cada herramienta devuelve un bloque `vis` que
   elige un componente de un catálogo cerrado y le pasa los datos. El front tiene
   esos componentes prefabricados en la marca Startin. Así no se ejecuta código
   generado por IA en un sitio público, no se gastan tokens dibujando, y todas las
   respuestas se ven igual de bien.

6. Las advertencias viajan DENTRO del bloque `vis`, no aparte. Una gráfica esconde
   las salvedades mejor que un párrafo; si la nota no va pegada al dibujo, se pierde.

DE DÓNDE SALEN LOS DATOS

  De las fichas precalculadas (`construir_fichas.py`), no de los parquet crudos.
  Responder es una búsqueda por clave, no una agregación. Eso mantiene el costo
  por pregunta cerca de cero y garantiza que la misma pregunta dé la misma cifra
  en dos reuniones distintas.

Ejecutar:
    pip install "mcp[cli]" duckdb
    BRUJULA_TOKEN_NACIONAL=... python server.py --datos ./data --puerto 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import unicodedata
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.fastmcp import FastMCP

LOG = logging.getLogger("brujula")

MUESTRA_MINIMA = 10          # evaluados mínimos para publicar un promedio
MAX_RESULTADOS = 15          # tope de coincidencias en una búsqueda
MAX_LISTA = 40               # tope de filas en un listado territorial

# Catálogo cerrado de visualizaciones. El front tiene un componente por cada una.
VIS_CIFRAS = "cifras"                 # tarjetas con números sueltos
VIS_BARRAS_COMP = "barras_comparadas" # sede vs municipio vs departamento
VIS_LINEA = "linea_tiempo"            # evolución de un indicador
VIS_BARRAS_NIVEL = "barras_nivel"     # transición / primaria / secundaria / media
VIS_MEDIDOR = "medidor_brecha"        # porcentaje contra una referencia
VIS_TABLA = "tabla"                   # listado ordenable
VIS_SENALES = "senales"               # puntos a revisar, con su explicación
VIS_MAPA = "mapa"                     # puntos sobre un mapa base

AREAS = {
    "prom_lectura": "Lectura crítica",
    "prom_matematicas": "Matemáticas",
    "prom_sociales": "Sociales y ciudadanas",
    "prom_naturales": "Ciencias naturales",
    "prom_ingles": "Inglés",
}

# Qué significa cada señal, en español y sin jerga. El agente NO improvisa esto:
# lo lee de aquí, para que la misma señal se explique igual siempre.
GLOSARIO_SENALES = {
    "desercion_cero_con_cobertura_baja":
        "Reporta 0 % de deserción teniendo cobertura neta baja. Casi siempre es un "
        "vacío de reporte, no un logro.",
    "sin_reporte_men_reciente":
        "Lleva tres años o más sin actualizar sus indicadores ante el Ministerio.",
    "cobertura_neta_baja":
        "Cobertura neta por debajo del 80 %: hay menores en edad escolar fuera del sistema.",
    "desercion_alta":
        "Deserción por encima del 5 %.",
    "brecha_digital_alta":
        "Menos del 40 % de los estudiantes declara tener internet en casa.",
    "revisar_contraste_recursos_resultados":
        "Contrata por encima de la mediana de su departamento y sus resultados están "
        "en el cuartil más bajo del mismo departamento. NO prueba desvío de recursos: "
        "SECOP ni siquiera registra dónde se ejecutó el gasto. Indica que ahí vale la "
        "pena pedir documentos.",
    "caida_sostenida":
        "Cinco puntos o más de caída en matemáticas a lo largo de tres periodos.",
    "por_debajo_de_su_departamento":
        "Cinco puntos o más por debajo de la mediana de su departamento.",
    "muestra_insuficiente":
        "Menos de diez evaluados: no admite promedio publicable.",
}

AVISO_SENALES = (
    "Las señales indican dónde revisar, no son hallazgos ni acusaciones. "
    "Verificar con documentos antes de afirmar nada."
)

AVISO_CPE = (
    "Computadores Para Educar es un acumulado histórico, no el estado actual del "
    "municipio. El programa entrega a más de 1.000 municipios al año hasta 2015, a "
    "65 en 2019, y el dataset deja de actualizarse en 2023. Sirve para saber qué se "
    "entregó ya, no cuántos equipos hay hoy ni en qué estado."
)

AVISO_MAPA = (
    "El mapa ubica, no mide. No existe ningún indicador educativo a nivel de "
    "vereda ni de centro poblado: el dato más fino es la sede, y su ubicación "
    "exacta no está publicada a nivel nacional."
)

AVISO_DISTANCIA = "Distancia en línea recta, no por carretera."

AVISO_ECONOMIA = (
    "El PIB solo se publica por departamento. Es contexto regional, no una cifra "
    "de este municipio."
)

AVISO_SECOP = (
    "SECOP registra el municipio de la entidad que contrata, no dónde se ejecutó el "
    "gasto, y el filtro por objeto es textual. Léase como orden de magnitud, nunca "
    "como inversión educativa del territorio."
)

mcp = FastMCP("brujula-educativa")
_con: duckdb.DuckDBPyConnection | None = None
_datos: Path = Path("./data")
_meta: dict[str, Any] = {}
_token_nacional: str = ""


# --------------------------------------------------------------------------- #
# Infraestructura
# --------------------------------------------------------------------------- #

VISTAS = [
    ("fichas_mun", "fichas_municipio.parquet"),
    ("fichas_sede", "fichas_sede.parquet"),
    ("men", "men_municipios.parquet"),
    ("saber_col", "saber11_colegios.parquet"),
    ("secop_top", "secop_contratos_mayores.parquet"),
    ("lugares", "fichas_lugar.parquet"),
    ("geo_cp", "territorio_centros_poblados.parquet"),
]


def conexion() -> duckdb.DuckDBPyConnection:
    """Una sola conexión DuckDB que lee los parquet directamente desde disco."""
    global _con, _meta
    if _con is None:
        _con = duckdb.connect(database=":memory:")
        for vista, archivo in VISTAS:
            ruta = _datos / archivo
            if ruta.exists():
                _con.execute(f"CREATE VIEW {vista} AS SELECT * FROM read_parquet('{ruta}')")
                # Los códigos DANE se rellenan aquí también, no solo en la
                # ingesta: el MEN publica el mismo municipio como "5002" y
                # "05002" según el año, y un código sin rellenar no cruza con
                # nada. No da error; hace desaparecer años enteros de una serie.
                columnas = {c[0] for c in _con.execute(f"DESCRIBE {vista}").fetchall()}
                anchos = {c: n for c, n in (("cod_municipio", 5), ("cod_departamento", 2))
                          if c in columnas}
                if anchos:
                    rellenos = ", ".join(f"lpad(CAST({c} AS VARCHAR), {n}, '0') AS {c}"
                                         for c, n in anchos.items())
                    _con.execute(
                        f"CREATE OR REPLACE VIEW {vista} AS "
                        f"SELECT * EXCLUDE ({', '.join(anchos)}), {rellenos} "
                        f"FROM read_parquet('{ruta}')"
                    )
                LOG.info("Vista %s lista desde %s", vista, archivo)
            else:
                LOG.warning("Falta %s — las herramientas que lo usan responderán sin datos", archivo)
        meta = _datos / "fichas_metadatos.json"
        if meta.exists():
            _meta = json.loads(meta.read_text(encoding="utf-8"))
    return _con


def existe(vista: str) -> bool:
    try:
        conexion().execute(f"SELECT 1 FROM {vista} LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


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


def corte() -> str:
    return _meta.get("generado", "")


def como_lista(valor: Any) -> list[Any]:
    """
    Las columnas de lista de un parquet vuelven como arreglos de numpy, y en un
    arreglo `or []` explota con «truth value is ambiguous». Esta función es la
    única forma en que el servidor toca una columna de lista.
    """
    if valor is None:
        return []
    try:
        return list(valor)
    except TypeError:
        return []


def explicar(senales: Any) -> list[dict[str, str]]:
    """Convierte la lista de claves de señal en algo que un rector pueda leer."""
    return [
        {"clave": s, "explicacion": GLOSARIO_SENALES.get(s, s)}
        for s in como_lista(senales)
    ]


# --------------------------------------------------------------------------- #
# Ámbito territorial
# --------------------------------------------------------------------------- #

FALTA_AMBITO = {
    "encontrado": False,
    "requiere_ambito": True,
    "mensaje": (
        "Antes de consultar hay que elegir un territorio. Pregúntale al usuario por "
        "el departamento y el municipio, o usa listar_departamentos y "
        "listar_municipios para ofrecerle las opciones."
    ),
}


def resolver_municipio(municipio: str, departamento: str = "") -> dict[str, Any] | None:
    """
    Del nombre que escribió una persona al municipio exacto de la ficha.

    Devuelve None cuando no hay coincidencia, y el agente tiene que decirlo en vez
    de escoger el municipio más parecido: hay 1.122 y varios comparten nombre en
    departamentos distintos.
    """
    if not municipio:
        return None
    con = conexion()
    filtros = ["strip_accents(upper(municipio)) = ?"]
    params: list[Any] = [sin_tildes(municipio)]
    if departamento:
        filtros.append("strip_accents(upper(departamento)) LIKE ?")
        params.append(f"%{sin_tildes(departamento)}%")

    filas = con.execute(
        f"SELECT * FROM fichas_mun WHERE {' AND '.join(filtros)}", params
    ).fetchdf()
    if filas.empty:
        # Segundo intento, por coincidencia parcial: la gente escribe "Santa Marta"
        # donde la ficha dice "SANTA MARTA (DIST. ESP.)".
        params[0] = f"%{sin_tildes(municipio)}%"
        filtros[0] = "strip_accents(upper(municipio)) LIKE ?"
        filas = con.execute(
            f"SELECT * FROM fichas_mun WHERE {' AND '.join(filtros)} LIMIT 5", params
        ).fetchdf()
    if filas.empty:
        return None
    if len(filas) > 1 and not departamento:
        return {
            "ambiguo": True,
            "opciones": filas[["cod_municipio", "municipio", "departamento"]].to_dict("records"),
        }
    return filas.iloc[0].to_dict()


# --------------------------------------------------------------------------- #
# Herramientas: elegir territorio
# --------------------------------------------------------------------------- #

@mcp.tool()
def listar_departamentos() -> dict[str, Any]:
    """
    Los departamentos disponibles, con cuántos municipios tiene cada uno.

    Es el primer paso de toda consulta. Ofrécelos al usuario para que elija antes
    de intentar responder nada con datos.
    """
    if not existe("fichas_mun"):
        return vacio("Las fichas no están cargadas en el servidor.")
    filas = conexion().execute("""
        SELECT departamento, count(*) AS municipios,
               sum(CASE WHEN n_senales > 0 THEN 1 ELSE 0 END) AS con_senales
        FROM fichas_mun WHERE departamento IS NOT NULL
        GROUP BY departamento ORDER BY departamento
    """).fetchall()
    return {
        "encontrado": True,
        "departamentos": [
            {"departamento": f[0], "municipios": f[1], "municipios_con_senales": f[2]}
            for f in filas
        ],
        "corte": corte(),
        "nota": "Pide al usuario que elija uno antes de continuar.",
    }


@mcp.tool()
def listar_municipios(departamento: str) -> dict[str, Any]:
    """
    Los municipios de un departamento. Segundo paso para fijar el territorio.

    Pásale el departamento tal como lo dijo el usuario; se normalizan tildes y
    mayúsculas.
    """
    if not existe("fichas_mun"):
        return vacio("Las fichas no están cargadas en el servidor.")
    filas = conexion().execute("""
        SELECT cod_municipio, municipio, n_senales
        FROM fichas_mun
        WHERE strip_accents(upper(departamento)) LIKE ?
        ORDER BY municipio
    """, [f"%{sin_tildes(departamento)}%"]).fetchall()
    if not filas:
        return vacio(
            f"No encontré el departamento «{departamento}».",
            "Usa listar_departamentos para ver los nombres exactos.",
        )
    return {
        "encontrado": True,
        "departamento": departamento,
        "municipios": [{"cod_municipio": f[0], "municipio": f[1], "senales": f[2]} for f in filas],
        "corte": corte(),
    }


# --------------------------------------------------------------------------- #
# Herramientas: territorio elegido
# --------------------------------------------------------------------------- #

@mcp.tool()
def ubicar_lugar(nombre: str, departamento: str = "") -> dict[str, Any]:
    """
    ¿Dónde queda este lugar? Busca un municipio o un poblado por su nombre y
    devuelve en qué municipio y departamento está, con sus coordenadas.

    Es la puerta de entrada para quien no conoce Colombia: alguien lee «Tibacuy»
    y no sabe si es un barrio, un pueblo o una región. No necesita que se haya
    fijado un territorio antes; al contrario, sirve para fijarlo.

    SI HAY VARIOS RESULTADOS, PREGUNTA. Los nombres se repiten muchísimo en el
    país: «Pueblo Nuevo» existe en decenas de municipios. Escoger el primero
    puede mandar un diagnóstico al territorio equivocado.
    """
    if not existe("lugares"):
        return vacio("El índice de lugares no está cargado en el servidor.")
    if not nombre:
        return vacio("Dime qué lugar buscar.")

    filtros = ["busqueda LIKE ?"]
    params: list[Any] = [f"%{sin_tildes(nombre)}%"]
    if departamento:
        filtros.append("strip_accents(upper(departamento)) LIKE ?")
        params.append(f"%{sin_tildes(departamento)}%")

    df = conexion().execute(f"""
        SELECT lugar, tipo, cod_municipio, municipio, departamento, lat, lon, homonimos
        FROM lugares WHERE {' AND '.join(filtros)}
        -- Coincidencia exacta primero, después las parciales; y dentro de cada
        -- grupo, los municipios antes que los poblados, porque quien escribe
        -- «Soacha» casi siempre busca el municipio.
        ORDER BY (busqueda = ?) DESC, (tipo = 'municipio') DESC, lugar
        LIMIT {MAX_RESULTADOS}
    """, params + [sin_tildes(nombre)]).fetchdf()

    if df.empty:
        return vacio(
            f"No encontré ningún lugar llamado «{nombre}».",
            "Puede estar escrito distinto, o ser una vereda: no existe una capa "
            "nacional de veredas en datos abiertos, solo municipios y centros poblados.",
        )

    lugares = [{
        "lugar": r.lugar,
        "tipo": r.tipo,
        "municipio": r.municipio,
        "departamento": r.departamento,
        "cod_municipio": r.cod_municipio,
        "lat": redondear(r.lat, 5),
        "lon": redondear(r.lon, 5),
    } for r in df.itertuples()]

    varios = len(lugares) > 1
    exactos = [l for l in lugares if sin_tildes(l["lugar"]) == sin_tildes(nombre)]

    advertencias = [AVISO_MAPA]
    if varios:
        advertencias.insert(0, (
            f"Hay {len(lugares)} lugares que coinciden con «{nombre}»"
            + (f", {len(exactos)} con ese nombre exacto" if len(exactos) > 1 else "")
            + ". Pregúntale al usuario cuál antes de seguir."
        ))

    primero = lugares[0]
    return {
        "encontrado": True,
        "varios_resultados": varios,
        "lugares": lugares,
        "fuente": "DANE — DIVIPOLA",
        "corte": corte(),
        "advertencias": advertencias,
        "nota": (
            "No escojas por tu cuenta cuando hay más de uno: mandar un diagnóstico "
            "al municipio equivocado es peor que preguntar."
        ) if varios else None,
        "vis": {
            "tipo": VIS_MAPA,
            "titulo": f"¿Dónde queda «{nombre}»?",
            "centro": {"lat": primero["lat"], "lon": primero["lon"]},
            "zoom": 11 if not varios else 6,
            "puntos": [
                {"nombre": l["lugar"], "etiqueta": f"{l['municipio']}, {l['departamento']}",
                 "tipo": l["tipo"], "lat": l["lat"], "lon": l["lon"],
                 "destacado": i == 0 and not varios}
                for i, l in enumerate(lugares) if l["lat"] is not None
            ],
            "nota_fuente": "DANE — DIVIPOLA",
            "advertencias": advertencias,
        },
    }


@mcp.tool()
def ficha_municipio(municipio: str, departamento: str = "") -> dict[str, Any]:
    """
    El diagnóstico completo de un municipio: cobertura, deserción, resultados de
    Saber 11, brecha digital, contratación con objeto educativo y las señales de
    lo que conviene revisar.

    Es la herramienta principal. Úsala apenas el usuario haya elegido territorio.
    Pásale el departamento si lo sabes: hay municipios con el mismo nombre en
    departamentos distintos.
    """
    if not existe("fichas_mun"):
        return vacio("Las fichas no están cargadas en el servidor.")
    if not municipio:
        return FALTA_AMBITO

    f = resolver_municipio(municipio, departamento)
    if f is None:
        return vacio(
            f"No encontré el municipio «{municipio}».",
            "Usa listar_municipios con el departamento para ver los nombres exactos.",
        )
    if f.get("ambiguo"):
        return {
            "encontrado": False,
            "ambiguo": True,
            "mensaje": f"«{municipio}» existe en varios departamentos. Pregúntale al usuario cuál.",
            "opciones": f["opciones"],
        }

    advertencias: list[str] = []
    anio_men = f.get("anio_men")
    if anio_men:
        advertencias.append(f"Indicadores del Ministerio con corte {int(anio_men)}.")
    if f.get("anio_desercion") and anio_men and f["anio_desercion"] != anio_men:
        advertencias.append(
            f"La deserción más reciente reportada es de {int(f['anio_desercion'])}, "
            f"no de {int(anio_men)}."
        )
    if f.get("valor_total_educacion") is not None:
        advertencias.append(AVISO_SECOP)
    if f.get("terminales_historicas"):
        advertencias.append(AVISO_CPE)
    if f.get("n_senales"):
        advertencias.append(AVISO_SENALES)

    cifras = [
        {"etiqueta": "Cobertura neta", "valor": redondear(f.get("cobertura_neta")), "unidad": "%"},
        {"etiqueta": "Deserción", "valor": redondear(f.get("desercion")), "unidad": "%"},
        {"etiqueta": "Aprobación", "valor": redondear(f.get("aprobacion")), "unidad": "%"},
        {"etiqueta": "Población 5 a 16 años", "valor": f.get("poblacion_5_16"), "unidad": ""},
        {"etiqueta": "Sedes evaluadas", "valor": f.get("sedes_evaluadas"), "unidad": ""},
        {"etiqueta": "Con internet en casa", "valor": redondear(f.get("pct_internet")), "unidad": "%"},
        {"etiqueta": "Sedes rurales", "valor": redondear(f.get("pct_sedes_rurales")), "unidad": "%"},
        {"etiqueta": "Km a la capital del departamento",
         "valor": redondear(f.get("km_a_capital")), "unidad": "km"},
    ]

    comparadas = {
        "tipo": VIS_BARRAS_COMP,
        "titulo": f"Saber 11 en {f['municipio']} frente a {f['departamento']}",
        "categorias": ["Matemáticas"],
        "series": [
            {"nombre": f["municipio"], "valores": [redondear(f.get("prom_matematicas"))], "enfasis": True},
            {"nombre": f"Mediana {f['departamento']}", "valores": [redondear(f.get("mediana_depto_matematicas"))]},
        ],
        "nota_fuente": f"ICFES · Saber 11 {f.get('periodo_saber', '')}",
        "advertencias": ["Comparación dentro del departamento, no contra el promedio nacional."],
    }

    # Los poblados del municipio, para el mapa y para dimensionar la dispersión.
    poblados: list[dict[str, Any]] = []
    if existe("geo_cp"):
        pdf = conexion().execute("""
            SELECT lugar, tipo, lat, lon FROM geo_cp
            WHERE cod_municipio = ? ORDER BY tipo, lugar
        """, [f["cod_municipio"]]).fetchdf()
        poblados = [{"lugar": r.lugar, "tipo": r.tipo,
                     "lat": redondear(r.lat, 5), "lon": redondear(r.lon, 5)}
                    for r in pdf.itertuples()]

    ubicacion = {
        "lat": redondear(f.get("lat"), 5),
        "lon": redondear(f.get("lon"), 5),
        "tipo": f.get("tipo_municipio"),
        "capital_del_departamento": f.get("capital_departamento"),
        "km_a_la_capital_departamental": redondear(f.get("km_a_capital")),
        "km_a_bogota": redondear(f.get("km_a_bogota")),
        "poblados_fuera_de_la_cabecera": f.get("poblados_fuera_de_la_cabecera"),
        "sedes_rurales": f.get("sedes_rurales"),
        "sedes_total": f.get("sedes_total"),
        "pct_sedes_rurales": redondear(f.get("pct_sedes_rurales")),
        "advertencia": AVISO_DISTANCIA,
    }

    economia = None
    if f.get("pib_miles_millones") is not None:
        principales = como_lista(f.get("actividades_principales"))
        porcentajes = como_lista(f.get("pct_actividades_principales"))
        economia = {
            "ambito": f"Departamento de {f['departamento']}",
            "anio": int(f["anio_pib"]) if f.get("anio_pib") else None,
            "pib_miles_de_millones_cop": redondear(f.get("pib_miles_millones")),
            "actividades_principales": [
                {"actividad": a, "pct_del_pib": redondear(pc)}
                for a, pc in zip(principales, porcentajes + [None] * len(principales))
            ],
            "sector_dominante": f.get("sector_dominante"),
            "advertencia": AVISO_ECONOMIA,
        }
        advertencias.append(AVISO_ECONOMIA)

    return {
        "encontrado": True,
        "municipio": f["municipio"],
        "departamento": f["departamento"],
        "cod_municipio": f["cod_municipio"],
        "ubicacion": ubicacion,
        "poblados": poblados,
        "economia_del_departamento": economia,
        "indicadores": {
            "anio": int(anio_men) if anio_men else None,
            "cobertura_neta": redondear(f.get("cobertura_neta")),
            "cobertura_bruta": redondear(f.get("cobertura_bruta")),
            "desercion": redondear(f.get("desercion")),
            "anio_desercion": int(f["anio_desercion"]) if f.get("anio_desercion") else None,
            "aprobacion": redondear(f.get("aprobacion")),
            "reprobacion": redondear(f.get("reprobacion")),
            "repitencia": redondear(f.get("repitencia")),
            "poblacion_5_16": f.get("poblacion_5_16"),
        },
        "saber11": {
            "periodo": f.get("periodo_saber"),
            "sedes_evaluadas": f.get("sedes_evaluadas"),
            "evaluados": f.get("evaluados"),
            **{k: redondear(f.get(k)) for k in AREAS},
            "mediana_departamento_matematicas": redondear(f.get("mediana_depto_matematicas")),
            "percentil_en_su_departamento": redondear(f.get("percentil_depto"), 0),
        },
        "brecha_digital": {
            "pct_internet": redondear(f.get("pct_internet")),
            "pct_computador": redondear(f.get("pct_computador")),
            "ninos_por_terminal": redondear(f.get("ninos_por_terminal")),
            "anio_ninos_por_terminal": (
                int(f["anio_ninos_por_terminal"]) if f.get("anio_ninos_por_terminal") else None
            ),
        },
        "computadores_para_educar": {
            "terminales_recibidas_historico": f.get("terminales_historicas"),
            "docentes_formados_historico": f.get("docentes_formados_historico"),
            "inversion_historica_cop": redondear(f.get("inversion_historica"), 0),
            "ultimo_anio_con_entregas": (
                int(f["ultimo_anio_con_entregas"]) if f.get("ultimo_anio_con_entregas") else None
            ),
            "advertencia": AVISO_CPE,
        },
        "contratacion": {
            "contratos_con_objeto_educativo": f.get("n_contratos_educacion"),
            "valor_total_cop": f.get("valor_total_educacion"),
            "advertencia": AVISO_SECOP,
        },
        "senales": explicar(f.get("senales")),
        "fuente": "ICFES, Ministerio de Educación Nacional, MinTIC, Colombia Compra Eficiente",
        "corte": corte(),
        "advertencias": advertencias,
        "vis": [
            {
                "tipo": VIS_CIFRAS,
                "titulo": f"{f['municipio']}, {f['departamento']}",
                "cifras": cifras,
                "nota_fuente": f"MEN {int(anio_men) if anio_men else ''} · ICFES {f.get('periodo_saber','')}",
                "advertencias": advertencias,
            },
            comparadas,
            {
                "tipo": VIS_MAPA,
                "titulo": f"Dónde queda {f['municipio']}",
                "centro": {"lat": ubicacion["lat"], "lon": ubicacion["lon"]},
                "zoom": 10,
                "puntos": (
                    [{"nombre": f["municipio"], "etiqueta": f["departamento"],
                      "tipo": "municipio", "lat": ubicacion["lat"],
                      "lon": ubicacion["lon"], "destacado": True}]
                    + [{"nombre": p["lugar"], "etiqueta": p["tipo"], "tipo": p["tipo"],
                        "lat": p["lat"], "lon": p["lon"], "destacado": False}
                       for p in poblados if p["lat"] is not None
                       and p["tipo"] != "cabecera municipal"]
                ) if ubicacion["lat"] is not None else [],
                "nota_fuente": "DANE — DIVIPOLA",
                "advertencias": [AVISO_MAPA, AVISO_DISTANCIA],
            },
            {
                "tipo": VIS_SENALES,
                "titulo": "Qué conviene revisar",
                "items": explicar(f.get("senales")),
                "advertencias": [AVISO_SENALES],
            },
        ],
    }


@mcp.tool()
def colegios_del_municipio(municipio: str, departamento: str = "", orden: str = "resultado") -> dict[str, Any]:
    """
    Las sedes de un municipio con su último resultado de Saber 11.

    `orden`: "resultado" (de mayor a menor), "senales" (primero las que tienen más
    puntos por revisar) o "nombre". Las sedes con menos de diez evaluados aparecen
    sin promedio, no se ocultan: que un colegio sea pequeño es información.
    """
    if not existe("fichas_sede"):
        return vacio("Las fichas por sede no están cargadas.")
    if not municipio:
        return FALTA_AMBITO

    f = resolver_municipio(municipio, departamento)
    if f is None:
        return vacio(f"No encontré el municipio «{municipio}».")
    if f.get("ambiguo"):
        return {"encontrado": False, "ambiguo": True, "opciones": f["opciones"],
                "mensaje": "Pregúntale al usuario en qué departamento."}

    criterio = {
        "resultado": "prom_matematicas DESC NULLS LAST",
        "senales": "n_senales DESC, prom_matematicas ASC NULLS LAST",
        "nombre": "nombre_sede",
    }.get(orden, "prom_matematicas DESC NULLS LAST")

    df = conexion().execute(f"""
        SELECT cod_dane_sede, nombre_sede, naturaleza, zona, evaluados,
               muestra_suficiente, prom_matematicas, prom_lectura, pct_internet,
               dif_vs_depto, cambio_matematicas, n_senales, senales
        FROM fichas_sede WHERE cod_municipio = ?
        ORDER BY {criterio} LIMIT {MAX_LISTA}
    """, [f["cod_municipio"]]).fetchdf()

    if df.empty:
        return vacio(
            f"No hay sedes con resultados de Saber 11 registradas en {f['municipio']}.",
            "Puede que el municipio no tenga educación media oficial reportada.",
        )

    sedes = []
    for r in df.itertuples():
        suficiente = bool(r.muestra_suficiente)
        sedes.append({
            "cod_dane_sede": r.cod_dane_sede,
            "nombre": r.nombre_sede,
            "naturaleza": r.naturaleza,
            "zona": r.zona,
            "evaluados": int(r.evaluados) if r.evaluados == r.evaluados else None,
            # Un promedio sobre menos de diez estudiantes no se publica. Se dice por qué.
            "prom_matematicas": redondear(r.prom_matematicas) if suficiente else None,
            "prom_lectura": redondear(r.prom_lectura) if suficiente else None,
            "nota": None if suficiente else f"Menos de {MUESTRA_MINIMA} evaluados: no se publica promedio.",
            "diferencia_vs_departamento": redondear(r.dif_vs_depto) if suficiente else None,
            "cambio_en_tres_periodos": redondear(r.cambio_matematicas) if suficiente else None,
            "senales": explicar(r.senales),
        })

    publicables = [s for s in sedes if s["prom_matematicas"] is not None]
    return {
        "encontrado": True,
        "municipio": f["municipio"],
        "departamento": f["departamento"],
        "total_sedes": len(sedes),
        "sedes": sedes,
        "fuente": "ICFES — Saber 11",
        "corte": corte(),
        "advertencias": [AVISO_SENALES] if any(s["senales"] for s in sedes) else [],
        "vis": {
            "tipo": VIS_TABLA,
            "titulo": f"Sedes de {f['municipio']}",
            "columnas": ["Sede", "Evaluados", "Matemáticas", "Frente al depto.", "Señales"],
            "filas": [
                [s["nombre"], s["evaluados"], s["prom_matematicas"],
                 s["diferencia_vs_departamento"], len(s["senales"])]
                for s in sedes
            ],
            "nota_fuente": "ICFES — Saber 11",
            "advertencias": (
                [f"{len(sedes) - len(publicables)} sedes sin promedio por tener menos "
                 f"de {MUESTRA_MINIMA} evaluados."] if len(publicables) < len(sedes) else []
            ),
        },
    }


@mcp.tool()
def ficha_colegio(cod_dane_sede: str) -> dict[str, Any]:
    """
    Una sede en detalle: su último resultado, su tendencia, cómo está frente a su
    municipio y frente a su departamento.

    El código DANE ya identifica el territorio, así que no hace falta volver a
    fijarlo. Obtén el código con colegios_del_municipio o buscar_colegio.
    """
    if not existe("fichas_sede"):
        return vacio("Las fichas por sede no están cargadas.")

    df = conexion().execute(
        "SELECT * FROM fichas_sede WHERE cod_dane_sede = ?", [str(cod_dane_sede).strip()]
    ).fetchdf()
    if df.empty:
        return vacio(
            f"No tengo resultados para el código DANE {cod_dane_sede}.",
            "Verifica el código con buscar_colegio.",
        )
    s = df.iloc[0].to_dict()

    if not bool(s.get("muestra_suficiente")):
        return {
            "encontrado": True,
            "publicable": False,
            "nombre": s["nombre_sede"],
            "municipio": s["municipio"],
            "departamento": s["departamento"],
            "evaluados": int(s["evaluados"]),
            "mensaje": (
                f"Esta sede presentó {int(s['evaluados'])} estudiantes en {s['periodo_saber']}. "
                f"Con menos de {MUESTRA_MINIMA} no se publica promedio: la cifra sería "
                "estadísticamente inestable y podría identificar a estudiantes."
            ),
            "fuente": "ICFES — Saber 11",
            "corte": corte(),
        }

    advertencias = [
        f"Resultados del periodo {s['periodo_saber']}.",
    ]
    if s.get("n_senales"):
        advertencias.append(AVISO_SENALES)

    return {
        "encontrado": True,
        "publicable": True,
        "cod_dane_sede": s["cod_dane_sede"],
        "nombre": s["nombre_sede"],
        "municipio": s["municipio"],
        "departamento": s["departamento"],
        "naturaleza": s["naturaleza"],
        "zona": s["zona"],
        "periodo": s["periodo_saber"],
        "evaluados": int(s["evaluados"]),
        "promedios": {v: redondear(s.get(k)) for k, v in AREAS.items()},
        "comparacion": {
            "mediana_municipio_matematicas": redondear(s.get("mediana_municipio_matematicas")),
            "mediana_departamento_matematicas": redondear(s.get("mediana_depto_matematicas")),
            "diferencia_vs_municipio": redondear(s.get("dif_vs_municipio")),
            "diferencia_vs_departamento": redondear(s.get("dif_vs_depto")),
        },
        "tendencia": {
            "desde": s.get("p_ini"),
            "hasta": s.get("p_fin"),
            "periodos": int(s["periodos_tendencia"]) if s.get("periodos_tendencia") else 0,
            "cambio_matematicas": redondear(s.get("cambio_matematicas")),
        },
        "brecha_digital": {
            "pct_internet": redondear(s.get("pct_internet")),
            "pct_computador": redondear(s.get("pct_computador")),
        },
        "senales": explicar(s.get("senales")),
        "fuente": "ICFES — Saber 11",
        "corte": corte(),
        "advertencias": advertencias,
        "vis": [
            {
                "tipo": VIS_BARRAS_COMP,
                "titulo": f"{s['nombre_sede']} frente a su entorno",
                "categorias": ["Matemáticas"],
                "series": [
                    {"nombre": "Esta sede", "valores": [redondear(s.get("prom_matematicas"))], "enfasis": True},
                    {"nombre": f"Mediana {s['municipio']}", "valores": [redondear(s.get("mediana_municipio_matematicas"))]},
                    {"nombre": f"Mediana {s['departamento']}", "valores": [redondear(s.get("mediana_depto_matematicas"))]},
                ],
                "nota_fuente": f"ICFES · Saber 11 {s['periodo_saber']}",
                "advertencias": ["Comparación dentro del departamento, no contra el promedio nacional."],
            },
            {
                "tipo": VIS_MEDIDOR,
                "titulo": "Estudiantes con internet en casa",
                "valor": redondear(s.get("pct_internet")),
                "referencia": 100,
                "unidad": "%",
                "nota_fuente": "ICFES — declarado por los propios estudiantes",
                "advertencias": ["Dato declarado por el estudiante al inscribirse, no medido."],
            },
        ],
    }


@mcp.tool()
def buscar_colegio(nombre: str, departamento: str, municipio: str = "") -> dict[str, Any]:
    """
    Busca sedes por nombre DENTRO de un departamento.

    El departamento es obligatorio a propósito: «Instituto Técnico Industrial»
    devuelve decenas de coincidencias en todo el país y ninguna sirve. Si el
    usuario no ha dicho dónde, pregúntale antes de llamar a esta herramienta.
    """
    if not existe("fichas_sede"):
        return vacio("Las fichas por sede no están cargadas.")
    if not departamento:
        return FALTA_AMBITO

    filtros = ["strip_accents(upper(nombre_sede)) LIKE ?",
               "strip_accents(upper(departamento)) LIKE ?"]
    params: list[Any] = [f"%{sin_tildes(nombre)}%", f"%{sin_tildes(departamento)}%"]
    if municipio:
        filtros.append("strip_accents(upper(municipio)) LIKE ?")
        params.append(f"%{sin_tildes(municipio)}%")

    filas = conexion().execute(f"""
        SELECT cod_dane_sede, nombre_sede, municipio, departamento, naturaleza,
               evaluados, periodo_saber
        FROM fichas_sede WHERE {' AND '.join(filtros)}
        ORDER BY evaluados DESC LIMIT {MAX_RESULTADOS}
    """, params).fetchall()

    if not filas:
        return vacio(
            f"No encontré ninguna sede que coincida con «{nombre}» en {departamento}.",
            "Prueba con menos palabras del nombre, o revisa el departamento.",
        )
    return {
        "encontrado": True,
        "coincidencias": [
            {"cod_dane_sede": f[0], "nombre": f[1], "municipio": f[2],
             "departamento": f[3], "naturaleza": f[4], "evaluados": f[5], "periodo": f[6]}
            for f in filas
        ],
        "corte": corte(),
        "nota": "Si hay más de una coincidencia, pregunta al usuario cuál antes de continuar.",
    }


@mcp.tool()
def evolucion_municipio(municipio: str, departamento: str = "", indicador: str = "desercion") -> dict[str, Any]:
    """
    Serie histórica de un indicador del Ministerio para un municipio.

    `indicador`: desercion, cobertura_neta, cobertura_bruta, aprobacion,
    reprobacion, repitencia o tasa_matriculacion.

    Los años sin reporte salen como null, no como cero. Un hueco en la serie es un
    dato en sí mismo: dice que el municipio dejó de reportar.
    """
    permitidos = {"desercion", "cobertura_neta", "cobertura_bruta", "aprobacion",
                  "reprobacion", "repitencia", "tasa_matriculacion"}
    if indicador not in permitidos:
        return vacio(f"Indicador no reconocido: «{indicador}».",
                     "Válidos: " + ", ".join(sorted(permitidos)))
    if not existe("men"):
        return vacio("Los indicadores del Ministerio no están cargados.")
    if not municipio:
        return FALTA_AMBITO

    f = resolver_municipio(municipio, departamento)
    if f is None:
        return vacio(f"No encontré el municipio «{municipio}».")
    if f.get("ambiguo"):
        return {"encontrado": False, "ambiguo": True, "opciones": f["opciones"],
                "mensaje": "Pregúntale al usuario en qué departamento."}

    filas = conexion().execute(f"""
        SELECT anio, {indicador} FROM men
        WHERE cod_municipio = ? ORDER BY anio
    """, [f["cod_municipio"]]).fetchall()
    if not filas:
        return vacio(f"No hay serie histórica para {f['municipio']}.")

    anios = [int(r[0]) for r in filas]
    valores = [redondear(r[1]) for r in filas]
    huecos = [a for a, v in zip(anios, valores) if v is None]

    advertencias = []
    if huecos:
        advertencias.append(
            f"Sin reporte en {', '.join(str(h) for h in huecos)}. "
            "Un año sin dato no es un cero: es un año que el municipio no reportó."
        )

    return {
        "encontrado": True,
        "municipio": f["municipio"],
        "departamento": f["departamento"],
        "indicador": indicador,
        "anios": anios,
        "valores": valores,
        "anios_sin_reporte": huecos,
        "fuente": "Ministerio de Educación Nacional",
        "corte": corte(),
        "advertencias": advertencias,
        "vis": {
            "tipo": VIS_LINEA,
            "titulo": f"{indicador.replace('_', ' ').capitalize()} en {f['municipio']}",
            "categorias": [str(a) for a in anios],
            "series": [{"nombre": f["municipio"], "valores": valores, "enfasis": True}],
            "nota_fuente": "Ministerio de Educación Nacional",
            "advertencias": advertencias,
        },
    }


@mcp.tool()
def senales_departamento(departamento: str, senal: str = "") -> dict[str, Any]:
    """
    Los municipios de un departamento que tienen puntos por revisar, ordenados por
    cuántos.

    Es la herramienta de focalización: sirve para decidir dónde vale la pena
    empezar un diagnóstico, no para concluir nada sobre nadie. Con `senal` se
    filtra a un tipo concreto (por ejemplo "desercion_cero_con_cobertura_baja").
    """
    if not existe("fichas_mun"):
        return vacio("Las fichas no están cargadas.")
    if not departamento:
        return FALTA_AMBITO

    filtros = ["strip_accents(upper(departamento)) LIKE ?", "n_senales > 0"]
    params: list[Any] = [f"%{sin_tildes(departamento)}%"]
    if senal:
        filtros.append("list_contains(senales, ?)")
        params.append(senal)

    df = conexion().execute(f"""
        SELECT cod_municipio, municipio, departamento, cobertura_neta, desercion,
               prom_matematicas, percentil_depto, n_senales, senales
        FROM fichas_mun WHERE {' AND '.join(filtros)}
        ORDER BY n_senales DESC, municipio LIMIT {MAX_LISTA}
    """, params).fetchdf()

    if df.empty:
        return vacio(
            f"No hay municipios con señales en {departamento}"
            + (f" para «{senal}»." if senal else "."),
            "Eso puede significar que reportan bien, o que no hay dato suficiente para marcar nada.",
        )

    municipios = [{
        "cod_municipio": r.cod_municipio,
        "municipio": r.municipio,
        "cobertura_neta": redondear(r.cobertura_neta),
        "desercion": redondear(r.desercion),
        "percentil_en_su_departamento": redondear(r.percentil_depto, 0),
        "senales": explicar(r.senales),
    } for r in df.itertuples()]

    return {
        "encontrado": True,
        "departamento": df.iloc[0]["departamento"],
        "municipios": municipios,
        "glosario": GLOSARIO_SENALES,
        "fuente": "ICFES, Ministerio de Educación Nacional, MinTIC, Colombia Compra Eficiente",
        "corte": corte(),
        "advertencias": [AVISO_SENALES, AVISO_SECOP],
        "vis": {
            "tipo": VIS_SENALES,
            "titulo": f"Municipios por revisar en {df.iloc[0]['departamento']}",
            "items": [
                {"clave": m["municipio"],
                 "explicacion": "; ".join(s["explicacion"] for s in m["senales"])}
                for m in municipios
            ],
            "advertencias": [AVISO_SENALES],
        },
    }


@mcp.tool()
def contratos_municipio(municipio: str, departamento: str = "") -> dict[str, Any]:
    """
    Los contratos más grandes con objeto educativo de las entidades con sede en un
    municipio: objeto, contratista, valor y enlace al expediente en SECOP.

    Sirve para pasar de "aquí hay algo que revisar" a un documento concreto que se
    puede pedir. NO es inversión educativa del territorio: SECOP registra dónde
    está la entidad que contrata, no dónde se ejecutó el gasto.
    """
    if not existe("secop_top"):
        return vacio("Los datos de contratación no están cargados.")
    if not municipio:
        return FALTA_AMBITO

    f = resolver_municipio(municipio, departamento)
    if f is None:
        return vacio(f"No encontré el municipio «{municipio}».")
    if f.get("ambiguo"):
        return {"encontrado": False, "ambiguo": True, "opciones": f["opciones"],
                "mensaje": "Pregúntale al usuario en qué departamento."}

    df = conexion().execute("""
        SELECT objeto_a_contratar, valor_contrato, nom_raz_social_contratista,
               nombre_de_la_entidad, fecha_de_firma_del_contrato, url_contrato,
               modalidad_de_contrataci_n, estado_del_proceso
        FROM secop_top WHERE cod_municipio = ? OR upper(municipio) = upper(?)
        ORDER BY valor_contrato DESC
    """, [f["cod_municipio"], f["municipio"]]).fetchdf()

    if df.empty:
        return vacio(
            f"No tengo contratos con objeto educativo registrados para {f['municipio']}.",
            "Puede que no los haya, o que el nombre del municipio difiera entre SECOP y el MEN.",
        )

    contratos = [{
        "objeto": r.objeto_a_contratar,
        "valor_cop": redondear(r.valor_contrato, 0),
        "contratista": r.nom_raz_social_contratista,
        "entidad": r.nombre_de_la_entidad,
        "fecha": str(r.fecha_de_firma_del_contrato),
        "modalidad": r.modalidad_de_contrataci_n,
        "estado": r.estado_del_proceso,
        "expediente": r.url_contrato,
    } for r in df.itertuples()]

    return {
        "encontrado": True,
        "municipio": f["municipio"],
        "departamento": f["departamento"],
        "total_contratos_educacion": f.get("n_contratos_educacion"),
        "valor_total_cop": f.get("valor_total_educacion"),
        "mayores": contratos,
        "fuente": "Colombia Compra Eficiente — SECOP Integrado",
        "corte": corte(),
        "advertencias": [AVISO_SECOP,
                         "Cada contrato trae su enlace al expediente público: verifícalo antes de afirmar nada."],
        "vis": {
            "tipo": VIS_TABLA,
            "titulo": f"Mayores contratos con objeto educativo — {f['municipio']}",
            "columnas": ["Objeto", "Valor (COP)", "Contratista", "Entidad", "Fecha"],
            "filas": [[c["objeto"], c["valor_cop"], c["contratista"], c["entidad"], c["fecha"]]
                      for c in contratos],
            "nota_fuente": "Colombia Compra Eficiente — SECOP Integrado",
            "advertencias": [AVISO_SECOP],
        },
    }


# --------------------------------------------------------------------------- #
# Herramientas nacionales
# --------------------------------------------------------------------------- #

def token_valido(token: str) -> bool:
    """
    Comparación en tiempo constante. Sin token configurado, nadie entra: es
    preferible que una herramienta nacional no funcione a que funcione para todos
    porque alguien olvidó la variable de entorno.
    """
    if not _token_nacional:
        return False
    return secrets.compare_digest(str(token), _token_nacional)


@mcp.tool()
def ranking_nacional(indicador: str, token: str, peores: bool = True, limite: int = 20) -> dict[str, Any]:
    """
    Ordena todos los municipios del país por un indicador. SOLO para organizaciones
    con acceso completo.

    Requiere un token que el proxy inyecta; no lo pidas al usuario ni lo inventes.
    Si no lo tienes, usa senales_departamento dentro del territorio que interese.
    """
    if not token_valido(token):
        return {
            "encontrado": False,
            "requiere_acceso_completo": True,
            "mensaje": (
                "La consulta nacional está reservada a organizaciones con acceso completo. "
                "Ofrece al usuario trabajar por departamento con senales_departamento, y "
                "cuéntale que puede solicitar acceso completo escribiendo a hola@startin.org.co."
            ),
        }
    permitidos = {"desercion", "cobertura_neta", "prom_matematicas", "pct_internet", "n_senales"}
    if indicador not in permitidos:
        return vacio(f"Indicador no válido: «{indicador}».", "Válidos: " + ", ".join(sorted(permitidos)))

    orden = "ASC" if peores else "DESC"
    if indicador in {"desercion", "n_senales"}:
        orden = "DESC" if peores else "ASC"

    df = conexion().execute(f"""
        SELECT municipio, departamento, {indicador} AS valor, n_senales
        FROM fichas_mun WHERE {indicador} IS NOT NULL
        ORDER BY valor {orden} LIMIT {min(int(limite), 100)}
    """).fetchdf()

    return {
        "encontrado": True,
        "indicador": indicador,
        "municipios": [
            {"municipio": r.municipio, "departamento": r.departamento,
             "valor": redondear(r.valor), "senales": int(r.n_senales)}
            for r in df.itertuples()
        ],
        "fuente": "ICFES, Ministerio de Educación Nacional, MinTIC",
        "corte": corte(),
        "advertencias": [
            "Un ranking nacional mezcla contextos muy distintos. Para decidir dónde "
            "intervenir, la comparación dentro del departamento es más informativa.",
            AVISO_SENALES,
        ],
        "vis": {
            "tipo": VIS_TABLA,
            "titulo": f"Municipios por {indicador}",
            "columnas": ["Municipio", "Departamento", indicador, "Señales"],
            "filas": [[r.municipio, r.departamento, redondear(r.valor), int(r.n_senales)]
                      for r in df.itertuples()],
            "nota_fuente": "ICFES · MEN · MinTIC",
            "advertencias": [AVISO_SENALES],
        },
    }


@mcp.tool()
def comparar_ocde(dominio: str = "lectura") -> dict[str, Any]:
    """
    Colombia frente al promedio de la OCDE en PISA. `dominio`: lectura, matematicas
    o ciencias.

    No requiere territorio porque PISA es una prueba muestral del país: no dice
    nada de un municipio ni de un colegio, y mezclarla con resultados
    institucionales sería inventar.
    """
    ruta = _datos / "pisa_colombia.json"
    if not ruta.exists():
        return vacio(
            "La tabla de PISA no está cargada en este servidor.",
            "La OCDE no publica PISA por API; la tabla se carga a mano desde el informe oficial.",
        )
    tabla = json.loads(ruta.read_text(encoding="utf-8"))
    serie = tabla.get(dominio)
    if not serie:
        return vacio(f"No tengo la serie de «{dominio}».", "Disponibles: " + ", ".join(tabla))

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


@mcp.tool()
def estado_de_los_datos() -> dict[str, Any]:
    """
    Qué fuentes están cargadas, con qué corte y con qué umbrales se marcaron las
    señales.

    Úsala cuando el usuario pregunte de dónde salen las cifras, hasta cuándo llegan
    o por qué falta algo. Responder eso con precisión es parte del servicio.
    """
    conexion()
    disponibles = {v: existe(v) for v, _ in VISTAS}
    return {
        "encontrado": True,
        "vistas_cargadas": disponibles,
        "metadatos_fichas": _meta,
        "consulta_nacional_habilitada": bool(_token_nacional),
        "limitaciones_conocidas": [
            "No existe un dataset nacional abierto de planta docente de básica y media.",
            "Los microdatos con internet y computador en casa terminan en 2022.",
            "Saber 11 solo es comparable desde 2014-2: antes era otra prueba, con otras áreas y otra escala.",
            AVISO_CPE,
            "Ocho archivos agregados del ICFES están truncados en el servidor de origen.",
            "La cobertura viene por nivel educativo, no grado por grado.",
            AVISO_SECOP,
        ],
        "fuente": "Fundación Startin — Brújula Educativa",
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

    global _datos, _token_nacional
    _datos = args.datos
    _token_nacional = os.environ.get("BRUJULA_TOKEN_NACIONAL", "")
    if not _token_nacional:
        LOG.warning("Sin BRUJULA_TOKEN_NACIONAL: las consultas nacionales quedan cerradas.")

    conexion()  # falla temprano si faltan los parquet, no en la primera pregunta

    # HTTP+SSE, no stdio: stdio solo sirve para desarrollo local.
    mcp.settings.port = args.puerto
    mcp.settings.host = "0.0.0.0"
    LOG.info("Brújula MCP escuchando en el puerto %s", args.puerto)
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
