#!/usr/bin/env python3
"""
Brújula Educativa — Ingesta de Datos Abiertos Colombia
Fundación Startin

Baja y normaliza las tres fuentes que viven en datos.gov.co:

  nudc-7mev  MEN — Estadísticas en educación por municipio (2011-2024)
  pyqj-s96k  MinTIC — Computadores Para Educar (hasta 2023)
  kgxf-xxbe  ICFES — Microdatos Saber 11 (2010-2022), agregados aquí por colegio

CONTEXTO VERIFICADO (19/09/2026):

1. En kgxf-xxbe TODAS las columnas son de tipo texto, incluidos los puntajes.
   avg(punt_global) falla con query.soql.type-mismatch. Hay que castear:
   avg(punt_global::number). Los filtros por periodo van entre comillas.

2. Los agregados se calculan del lado del servidor con SoQL. Nunca se descargan
   los 8,2 millones de filas: se piden promedios y llegan kilobytes. Agrupar sin
   filtrar por periodo sí desborda el tiempo de respuesta, así que se recorre
   periodo por periodo.

3. En pyqj-s96k los números vienen con coma decimal y como texto (",00", "2,00").
   Sin normalizar, cualquier promedio sale mal.

4. Los códigos DANE se guardan como texto. Leídos como número pierden los ceros
   a la izquierda y dejan de cruzar entre fuentes.

Uso:
    python ingest_datos_gov.py --salida ./data
    python ingest_datos_gov.py --salida ./data --saltar saber11
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import requests

LOG = logging.getLogger("datosgov")

BASE = "https://www.datos.gov.co/resource"
PAGINA = 50_000          # tope por petición que acepta Socrata
REINTENTOS = 3
ESPERA = 4               # segundos entre reintentos

DATASET_MEN = "nudc-7mev"
DATASET_CPE = "pyqj-s96k"
DATASET_SABER = "kgxf-xxbe"

# Periodos de Saber 11 con cobertura nacional (calendario A). Los de primer
# semestre traen ~15.000 estudiantes de calendario B y se incluyen aparte.
PERIODOS_SABER = [
    "20102", "20112", "20122", "20132", "20142", "20152",
    "20162", "20172", "20194", "20224",
]

AREAS = {
    "punt_lectura_critica": "prom_lectura",
    "punt_matematicas": "prom_matematicas",
    "punt_sociales_ciudadanas": "prom_sociales",
    "punt_c_naturales": "prom_naturales",
    "punt_ingles": "prom_ingles",
}


def consultar(dataset: str, params: dict) -> list[dict]:
    """GET contra Socrata con reintentos. Devuelve la lista de registros."""
    url = f"{BASE}/{dataset}.json"
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.get(url, params=params, timeout=240)
            if resp.status_code == 400:
                # Socrata devuelve el detalle del error de SoQL en el cuerpo.
                raise RuntimeError(f"SoQL rechazado: {resp.text[:300]}")
            if resp.status_code == 503:
                # Socrata devuelve 503 en medio segundo cuando su motor está
                # saturado. No es un bloqueo ni un error nuestro: hay que esperar
                # más que en un fallo de red normal y volver a intentar.
                raise RuntimeError("Socrata saturado (503)")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            LOG.warning("%s intento %s/%s falló: %s", dataset, intento, REINTENTOS, str(exc)[:160])
            if intento < REINTENTOS:
                saturado = "503" in str(exc)
                time.sleep(ESPERA * intento * (5 if saturado else 1))
    raise RuntimeError(f"{dataset}: agotados los reintentos") from ultimo


def paginar(dataset: str, params: dict) -> pd.DataFrame:
    """Recorre el dataset por páginas hasta que deje de devolver filas."""
    filas: list[dict] = []
    offset = 0
    while True:
        lote = consultar(dataset, {**params, "$limit": PAGINA, "$offset": offset})
        if not lote:
            break
        filas.extend(lote)
        LOG.debug("%s: %s filas acumuladas", dataset, len(filas))
        if len(lote) < PAGINA:
            break
        offset += PAGINA
    return pd.DataFrame(filas)


def a_numero(serie: pd.Series) -> pd.Series:
    """
    Normaliza número tolerando coma decimal colombiana. Lo que no convierte
    queda nulo, nunca cero: un cero inventado contamina cualquier promedio.
    """
    texto = serie.astype(str).str.strip()
    coma = texto.str.contains(r",\d{1,2}$", regex=True, na=False)
    texto = texto.where(
        ~coma,
        texto.str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
    )
    return pd.to_numeric(texto, errors="coerce")


# --------------------------------------------------------------------------- #
# MEN — indicadores municipales
# --------------------------------------------------------------------------- #

def bajar_men() -> pd.DataFrame:
    LOG.info("MEN (%s): descargando indicadores municipales...", DATASET_MEN)
    df = paginar(DATASET_MEN, {"$order": "a_o,c_digo_municipio"})

    renombres = {
        "a_o": "anio",
        "c_digo_municipio": "cod_municipio",
        "c_digo_departamento": "cod_departamento",
        "poblaci_n_5_16": "poblacion_5_16",
        "tasa_matriculaci_n_5_16": "tasa_matriculacion",
        "deserci_n": "desercion",
        "deserci_n_transici_n": "desercion_transicion",
        "deserci_n_primaria": "desercion_primaria",
        "deserci_n_secundaria": "desercion_secundaria",
        "deserci_n_media": "desercion_media",
        "aprobaci_n": "aprobacion",
        "reprobaci_n": "reprobacion",
        "cobertura_neta_transici_n": "cobertura_neta_transicion",
        "cobertura_bruta_transici_n": "cobertura_bruta_transicion",
    }
    df = df.rename(columns={k: v for k, v in renombres.items() if k in df.columns})

    texto = {"cod_municipio", "cod_departamento", "municipio", "departamento", "etc", "c_digo_etc"}
    for col in df.columns:
        if col in texto:
            df[col] = df[col].astype("string").str.strip()
        elif col != "anio":
            df[col] = a_numero(df[col])
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")

    # Un 0 % de deserción en un municipio con cobertura baja casi siempre es un
    # vacío de reporte, no un logro. Se marca para que el agente lo advierta en
    # vez de presentarlo como dato bueno.
    if "desercion" in df.columns and "cobertura_neta" in df.columns:
        df["desercion_sospechosa"] = (df["desercion"] == 0) & (df["cobertura_neta"] < 80)

    LOG.info("MEN: %s filas, años %s-%s", len(df), df["anio"].min(), df["anio"].max())
    return df


# --------------------------------------------------------------------------- #
# MinTIC — Computadores Para Educar
# --------------------------------------------------------------------------- #

def bajar_cpe() -> pd.DataFrame:
    LOG.info("CPE (%s): descargando...", DATASET_CPE)
    df = paginar(DATASET_CPE, {"$order": "anio,coddane"})

    renombres = {
        "coddane": "cod_municipio",
        "ni_os_por_terminal": "ninos_por_terminal",
        "ni_os_por_terminal_municipal": "ninos_por_terminal_municipal",
        "inversi_n": "inversion",
        "docentes_acompa_ados": "docentes_acompanados",
        "meta_docentes_acompa_ados": "meta_docentes_acompanados",
    }
    df = df.rename(columns={k: v for k, v in renombres.items() if k in df.columns})

    texto = {"cod_municipio", "departamento", "municipio"}
    for col in df.columns:
        if col in texto:
            df[col] = df[col].astype("string").str.strip()
        elif col in {"fecha_corte", "vigencia"}:
            df[col] = pd.to_datetime(df[col], errors="coerce")
        else:
            df[col] = a_numero(df[col])

    if "anio" in df.columns:
        df["anio"] = df["anio"].astype("Int64")

    LOG.info("CPE: %s filas", len(df))
    return df


# --------------------------------------------------------------------------- #
# ICFES — agregado por colegio desde los microdatos
# --------------------------------------------------------------------------- #

def departamentos() -> list[str]:
    """Lista de departamentos presentes en los microdatos, para trocear por ahí."""
    datos = consultar(DATASET_SABER, {"$select": "cole_depto_ubicacion", "$group": "cole_depto_ubicacion"})
    valores = [d.get("cole_depto_ubicacion") for d in datos if d.get("cole_depto_ubicacion")]
    LOG.info("Saber 11: %s departamentos", len(valores))
    return sorted(valores)


def agregar_saber11(periodos: list[str]) -> pd.DataFrame:
    """
    Reconstruye el promedio por colegio para los años que el ICFES no publica
    como archivo agregado usable. La agregación ocurre en el servidor de
    Socrata: se piden promedios, no filas.

    SE TROCEA POR DEPARTAMENTO, y no es un capricho. Verificado el 19/09/2026 en
    Azure Cloud Shell: una sola consulta agrupando un periodo nacional completo
    (~1.000.000 de filas) no responde en cuatro minutos y termina sacando 503 del
    motor de Socrata. La misma consulta acotada a un departamento devuelve en
    0,4 s con caché caliente y 11,7 s en frío. Con 33 departamentos por periodo la
    corrida completa toma minutos, no horas, y cada pieza es reintentable.
    """
    seleccion = ", ".join(
        [
            "cole_cod_dane_establecimiento",
            "cole_nombre_establecimiento",
            "cole_cod_mcpio_ubicacion",
            "cole_mcpio_ubicacion",
            "cole_depto_ubicacion",
            "cole_naturaleza",
            "cole_area_ubicacion",
            "count(*) as evaluados",
        ]
        + [f"avg({origen}::number) as {destino}" for origen, destino in AREAS.items()]
        # Conectividad del hogar declarada por los propios estudiantes: es la
        # medida de brecha digital que ninguna otra fuente tiene a este detalle.
        + [
            "sum(case(fami_tieneinternet='Si',1,true,0)) as con_internet",
            "sum(case(fami_tienecomputador='Si',1,true,0)) as con_computador",
        ]
    )
    agrupacion = (
        "cole_cod_dane_establecimiento, cole_nombre_establecimiento, "
        "cole_cod_mcpio_ubicacion, cole_mcpio_ubicacion, cole_depto_ubicacion, "
        "cole_naturaleza, cole_area_ubicacion"
    )

    deptos = departamentos()
    partes: list[pd.DataFrame] = []

    for periodo in periodos:
        del_periodo: list[pd.DataFrame] = []
        fallidos: list[str] = []

        for depto in deptos:
            # Las comillas simples en el nombre romperían el WHERE; se duplican.
            seguro = depto.replace("'", "''")
            try:
                datos = consultar(
                    DATASET_SABER,
                    {
                        "$select": seleccion,
                        "$where": f"periodo='{periodo}' AND cole_depto_ubicacion='{seguro}'",
                        "$group": agrupacion,
                        "$limit": PAGINA,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                fallidos.append(depto)
                LOG.warning("Saber 11 %s / %s: %s", periodo, depto, str(exc)[:140])
                continue

            if datos:
                del_periodo.append(pd.DataFrame(datos))

        if not del_periodo:
            LOG.warning("Saber 11 %s: sin resultados en ningún departamento", periodo)
            continue

        df = pd.concat(del_periodo, ignore_index=True)
        df["periodo"] = periodo
        df["anio"] = int(periodo[:4])
        partes.append(df)

        if fallidos:
            LOG.error(
                "Saber 11 %s: %s colegios, pero FALTAN %s departamentos (%s). "
                "El periodo queda incompleto; vuelve a correrlo antes de publicar.",
                periodo, len(df), len(fallidos), ", ".join(fallidos[:5]),
            )
        else:
            LOG.info("Saber 11 %s: %s colegios en %s departamentos", periodo, len(df), len(deptos))

    if not partes:
        return pd.DataFrame()

    df = pd.concat(partes, ignore_index=True).rename(
        columns={
            "cole_cod_dane_establecimiento": "cod_dane_sede",
            "cole_nombre_establecimiento": "nombre_sede",
            "cole_cod_mcpio_ubicacion": "cod_municipio",
            "cole_mcpio_ubicacion": "municipio",
            "cole_depto_ubicacion": "departamento",
            "cole_naturaleza": "naturaleza",
            "cole_area_ubicacion": "zona",
        }
    )

    for col in ["cod_dane_sede", "cod_municipio"]:
        df[col] = df[col].astype("string").str.strip()
    for col in ["evaluados", "con_internet", "con_computador", *AREAS.values()]:
        df[col] = a_numero(df[col])

    # Porcentajes, que es como se lee la brecha, no conteos absolutos.
    df["pct_internet"] = (df["con_internet"] / df["evaluados"] * 100).round(1)
    df["pct_computador"] = (df["con_computador"] / df["evaluados"] * 100).round(1)

    # Un colegio con 3 evaluados no admite promedio publicable.
    df["muestra_suficiente"] = df["evaluados"] >= 10

    LOG.info("Saber 11: %s filas en %s periodos", len(df), df["periodo"].nunique())
    return df


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de Datos Abiertos Colombia")
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument(
        "--saltar", nargs="*", default=[], choices=["men", "cpe", "saber11"],
        help="Fuentes a omitir en esta corrida",
    )
    parser.add_argument("--periodos", nargs="*", default=PERIODOS_SABER)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )
    args.salida.mkdir(parents=True, exist_ok=True)

    tareas = [
        ("men", "men_municipios.parquet", bajar_men),
        ("cpe", "computadores_educar.parquet", bajar_cpe),
        ("saber11", "saber11_colegios.parquet", lambda: agregar_saber11(args.periodos)),
    ]

    resumen: list[tuple[str, str, int]] = []
    for clave, archivo, funcion in tareas:
        if clave in args.saltar:
            resumen.append((clave, "OMITIDO", 0))
            continue
        try:
            df = funcion()
            if df.empty:
                resumen.append((clave, "VACIO", 0))
                continue
            df.to_parquet(args.salida / archivo, index=False)
            resumen.append((clave, "OK", len(df)))
        except Exception as exc:  # noqa: BLE001
            LOG.error("%s: %s", clave, exc)
            resumen.append((clave, "ERROR", 0))

    print("\n" + "=" * 48)
    print(f"{'FUENTE':<12}{'ESTADO':<12}{'FILAS':>12}")
    print("-" * 48)
    for clave, estado, n in resumen:
        print(f"{clave:<12}{estado:<12}{n:>12,}".replace(",", "."))
    print("=" * 48)

    return 0 if any(e == "OK" for _, e, _ in resumen) else 1


if __name__ == "__main__":
    sys.exit(main())
