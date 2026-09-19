def x():
    if True:
        return 1
    return 0
#!/usr/bin/env python3
"""
Brújula Educativa — Ingesta de resultados agregados Saber 11 (ICFES)
Fundación Startin

Descarga los archivos de "Resultados agregados por establecimiento" que el ICFES
publica en su portal, los normaliza a un esquema único y los deja en Parquet.

CONTEXTO VERIFICADO (19/09/2026) — leer antes de tocar este archivo:

1. El ICFES publica dos esquemas distintos:
   - MODERNO (2024, 2025): 21 columnas, incluye CODIGODANE_SEDE y una fila por
     SEDE-JORNADA. Cabecera en la fila 1 (la fila 0 es un título).
   - ANTIGUO (2015 y anteriores): 24 columnas, SIN CODIGODANE_SEDE, la columna
     de conteo se llama EVALUADOS (plural) y trae áreas que ya no existen
     (razonamiento cuantitativo, competencias ciudadanas).

2. OCHO archivos están DAÑADOS en el servidor del ICFES: pesan exactamente
   1.048.576 bytes (1 MiB) y no se pueden descomprimir. No es un problema de
   descarga: el servidor reporta ese tamaño como el real. Afecta a
   2012, 2016-2, 2017-2, 2018, 2019-1, 2019-2, 2020-4 y 2021-4 — justamente
   los de calendario A, que son los que traen las ~14.000 sedes del país.
   El script los detecta y los salta con una advertencia en vez de reventar.

3. Para los años sin archivo usable la serie se reconstruye desde los microdatos
   de datos.gov.co. Eso lo hace ingest_datos_gov.py, no este script.

Uso:
    python ingest_icfes.py --salida ./data
    python ingest_icfes.py --salida ./data --solo 2025-2 2024-2
"""

from __future__ import annotations

import argparse
import logging
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

LOG = logging.getLogger("icfes")

BASE = "https://www.icfes.gov.co/wp-content/uploads/"

# Tamaño exacto de los archivos truncados en el servidor del ICFES.
TAMANO_TRUNCADO = 1_048_576

ARCHIVOS: dict[str, str] = {
    "2025-2": "2026/01/Resultados-Agregados-2025-2.xlsx",
    "2025-1": "2026/01/Resultados-Agregados-2025-1.xlsx",
    "2024-2": "2025/02/Resultados-Agregados-2024-2.xlsx",
    "2024-1": "2025/02/Resultados-Agregados-2024-1.xlsx",
    "2022-1": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2022-1-VF.xlsx",
    "2021-4": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2021-4.xlsx",
    "2021-1": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2021-1.xlsx",
    "2020-4": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2020-4.xlsx",
    "2019-2": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2019-2.xlsx",
    "2019-1": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2019-1.xlsx",
    "2018-2": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-20181.xlsx",
    "2017-2": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2017-2.xlsx",
    "2017-1": "2025/02/Resultados-agregados-puntajes-promedio-saber-11-2017-1.xlsx",
    "2016-2": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2016-2.xlsx",
    "2015-1": "2025/02/Resultados-agregados-puntajes-promedio-Saber-11-2015-1-por-institucion-educativa.xls",
    "2012-2": "2025/02/Tabla-resultados-agregados-saber-11-2012-instituciones-educativas.xlsx",
}

COLUMNAS_SALIDA = [
    "periodo", "anio", "semestre",
    "cod_inst", "cod_dane_sede", "nombre_sede",
    "cod_municipio", "municipio", "departamento",
    "calendario", "naturaleza", "jornada",
    "evaluados",
    "prom_lectura", "prom_matematicas", "prom_sociales",
    "prom_naturales", "prom_ingles",
    "desv_lectura", "desv_matematicas", "desv_sociales",
    "desv_naturales", "desv_ingles",
]

# Nombre en el Excel (normalizado) -> nombre de salida. Cubre los dos esquemas.
MAPEO = {
    "CODINST": "cod_inst",
    "CODIGODANE_SEDE": "cod_dane_sede",
    "NOMBREINSTITUCION": "nombre_sede",
    "CODIGOMUNICIPIO": "cod_municipio",
    "NOMBREMUNICIPIO": "municipio",
    "DEPARTAMENTO": "departamento",
    "CALENDARIO": "calendario",
    "NATURALEZA": "naturaleza",
    "JORNADA": "jornada",
    "EVALUADO": "evaluados",
    "EVALUADOS": "evaluados",
    "PROMLECTURACRITICA": "prom_lectura",
    "PROMMATEMATICA": "prom_matematicas",
    "PROMMATEMATICAS": "prom_matematicas",
    "PROMSOCIALESYCIUDADANAS": "prom_sociales",
    "PROMCIENCIASNATURALES": "prom_naturales",
    "PROMINGLES": "prom_ingles",
    "DESVLECTURACRITICA": "desv_lectura",
    "DESVMATEMATICA": "desv_matematicas",
    "DESVMATEMATICAS": "desv_matematicas",
    "DESVSOCIALESYCIUDADANAS": "desv_sociales",
    "DESVCIENCIASNATURALES": "desv_naturales",
    "DESVINGLES": "desv_ingles",
}

NUMERICAS = [c for c in COLUMNAS_SALIDA if c.startswith(("prom_", "desv_"))] + ["evaluados"]
TEXTO_CODIGO = ["cod_inst", "cod_dane_sede", "cod_municipio"]


@dataclass
class Resultado:
    periodo: str
    filas: int
    estado: str
    detalle: str = ""


def normalizar_encabezado(valor: object) -> str:
    """Quita tildes, espacios y signos para comparar nombres de columna."""
    texto = str(valor or "")
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return "".join(c for c in texto.upper() if c.isalnum() or c == "_")


def descargar(periodo: str, ruta: str, cache: Path) -> bytes | None:
    """Descarga el archivo, usando caché local. Devuelve None si está truncado."""
    destino = cache / f"{periodo}{Path(ruta).suffix}"
    if destino.exists():
        datos = destino.read_bytes()
        LOG.debug("%s: leído de caché (%s bytes)", periodo, len(datos))
    else:
        url = BASE + ruta
        LOG.info("%s: descargando...", periodo)
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        datos = resp.content
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(datos)

    if len(datos) == TAMANO_TRUNCADO:
        LOG.warning(
            "%s: ARCHIVO DAÑADO EN EL ORIGEN — %s bytes exactos (1 MiB). "
            "La subida al portal del ICFES quedó truncada. Se omite.",
            periodo, len(datos),
        )
        return None
    return datos


def localizar_encabezado(crudo: pd.DataFrame) -> int:
    """La cabecera real no siempre está en la fila 0: suele haber un título encima."""
    for i in range(min(10, len(crudo))):
        celdas = {normalizar_encabezado(v) for v in crudo.iloc[i].tolist()}
        if "CODINST" in celdas or "NOMBREINSTITUCION" in celdas:
            return i
    raise ValueError("no se encontró la fila de encabezado en las primeras 10 filas")


def a_numero(serie: pd.Series) -> pd.Series:
    """
    Convierte a número tolerando las dos convenciones que aparecen en las
    fuentes colombianas: '1234.5' y '1.234,5'. Lo que no se pueda convertir
    queda como nulo, nunca como cero — un cero inventado es peor que un vacío.
    """
    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_numeric(serie, errors="coerce")
    texto = serie.astype(str).str.strip()
    coma_decimal = texto.str.contains(r",\d{1,2}$", regex=True, na=False)
    texto = texto.where(
        ~coma_decimal,
        texto.str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
    )
    return pd.to_numeric(texto, errors="coerce")


def procesar(periodo: str, datos: bytes, sufijo: str) -> pd.DataFrame:
    motor = "xlrd" if sufijo == ".xls" else "openpyxl"
    crudo = pd.read_excel(pd.io.common.BytesIO(datos), header=None, engine=motor)

    fila = localizar_encabezado(crudo)
    encabezados = [normalizar_encabezado(v) for v in crudo.iloc[fila].tolist()]
    df = crudo.iloc[fila + 1:].copy()
    df.columns = encabezados
    df = df.loc[:, [c for c in df.columns if c in MAPEO]]
    df = df.rename(columns=MAPEO)
    df = df.loc[:, ~df.columns.duplicated()]

    for col in COLUMNAS_SALIDA:
        if col not in df.columns:
            df[col] = pd.NA

    anio, semestre = periodo.split("-")
    df["periodo"] = periodo
    df["anio"] = int(anio)
    df["semestre"] = int(semestre)

    for col in NUMERICAS:
        df[col] = a_numero(df[col])
    for col in TEXTO_CODIGO:
        # Los códigos DANE son identificadores, no cantidades: si se leen como
        # número pierden los ceros a la izquierda y dejan de cruzar con el MEN.
        df[col] = df[col].astype("string").str.strip().str.replace(r"\.0$", "", regex=True)

    df = df[COLUMNAS_SALIDA]
    df = df.dropna(subset=["nombre_sede"])
    df = df[df["evaluados"].notna() & (df["evaluados"] > 0)]
    return df.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de resultados agregados Saber 11 (ICFES)")
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument("--cache", type=Path, default=Path("./.cache_icfes"))
    parser.add_argument("--solo", nargs="*", help="Periodos concretos, p. ej. 2025-2 2024-2")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    periodos = args.solo or list(ARCHIVOS)
    desconocidos = [p for p in periodos if p not in ARCHIVOS]
    if desconocidos:
        LOG.error("Periodos desconocidos: %s", ", ".join(desconocidos))
        return 2

    args.salida.mkdir(parents=True, exist_ok=True)
    partes: list[pd.DataFrame] = []
    reporte: list[Resultado] = []

    for periodo in periodos:
        ruta = ARCHIVOS[periodo]
        try:
            datos = descargar(periodo, ruta, args.cache)
            if datos is None:
                reporte.append(Resultado(periodo, 0, "DAÑADO", "1 MiB exacto en el origen"))
                continue
            df = procesar(periodo, datos, Path(ruta).suffix)
            partes.append(df)
            reporte.append(Resultado(periodo, len(df), "OK"))
            LOG.info("%s: %s sedes", periodo, f"{len(df):,}".replace(",", "."))
        except Exception as exc:  # noqa: BLE001 — un periodo malo no debe tumbar la corrida
            reporte.append(Resultado(periodo, 0, "ERROR", str(exc)[:120]))
            LOG.error("%s: %s", periodo, exc)

    print("\n" + "=" * 64)
    print(f"{'PERIODO':<10}{'ESTADO':<10}{'FILAS':>10}  DETALLE")
    print("-" * 64)
    for r in reporte:
        print(f"{r.periodo:<10}{r.estado:<10}{r.filas:>10,}  {r.detalle}".replace(",", "."))
    print("=" * 64)

    if not partes:
        LOG.error("Ningún periodo produjo datos.")
        return 1

    completo = pd.concat(partes, ignore_index=True)
    destino = args.salida / "saber11_agregado.parquet"
    completo.to_parquet(destino, index=False)

    usables = sum(1 for r in reporte if r.estado == "OK")
    LOG.info(
        "Escrito %s — %s filas, %s periodos usables de %s.",
        destino, f"{len(completo):,}".replace(",", "."), usables, len(periodos),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
