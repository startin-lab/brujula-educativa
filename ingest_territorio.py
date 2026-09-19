#!/usr/bin/env python3
"""
Brújula Educativa — Ingesta de contexto territorial
Fundación Startin

Responde a una pregunta que el resto del proyecto daba por resuelta: ¿dónde
queda esto? Todo lo demás asume que quien consulta ya conoce el país. Alguien
que llega de afuera —un donante, un aliado internacional, un funcionario nuevo—
lee «Tibacuy» y no sabe si es un barrio, un pueblo o una región.

QUÉ SE INGESTA (verificado el 19/09/2026 contra datos.gov.co)

  gdxc-w37w  DIVIPOLA municipios ........ 1.122 con código DANE y coordenadas
  xaxy-8nri  DIVIPOLA centros poblados ... 8.161 con coordenadas
  t7kp-7a7c  DIVIPOLA departamentos ......... 33 con punto de referencia
  kgyi-qc7j  PIB departamental ........... 16.302 filas, 2005-2023, 13 actividades

LO QUE NO EXISTE, Y HAY QUE DECIRLO

  · VEREDAS. En datos.gov.co hay 249 resultados con esa palabra y todos son de un
    municipio suelto («Veredas de Chía», «Barrios y Veredas de Tauramena»). La
    capa nacional (~32.000 veredas) vive en el IGAC como archivo geográfico, no
    como API. Los centros poblados son lo más fino con cobertura nacional.

  · SEDES GEORREFERENCIADAS. No hay dataset nacional. El que se llama
    «Coordenadas - Sedes Educativas» (bt4f-xk4p) son 26 filas de un solo
    municipio, con las coordenadas escritas como texto: 4º30'19.06"N.

  · POLÍGONOS. No hay límites municipales en el portal. El mapa es de puntos.

  · ECONOMÍA MUNICIPAL. El PIB solo está por departamento. «Valor agregado
    municipal» devuelve un único resultado, y es solo de Caldas. Lo municipal
    está en TerriData del DNP, que es descarga de archivo.

TRES TRAMPAS DE ESTOS DATASETS

  1. Las coordenadas de DIVIPOLA vienen con COMA decimal: "-75,581775".
  2. El PIB trae los departamentos en Mayúscula Inicial («Cundinamarca») mientras
     el MEN los trae en mayúscula sostenida («CUNDINAMARCA»), y el LIKE de Socrata
     distingue mayúsculas: filtrar por nombre devuelve cero filas sin error.
  3. Los códigos de departamento van sin cero a la izquierda ("5", no "05").

Uso:
    python ingest_territorio.py --salida ./data
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests

LOG = logging.getLogger("territorio")

BASE = "https://www.datos.gov.co/resource"
TIMEOUT = 60
REINTENTOS = 3
ESPERA = 4
PAGINA = 20000

DS_MUNICIPIOS = "gdxc-w37w"
DS_CENTROS = "xaxy-8nri"
DS_DEPARTAMENTOS = "t7kp-7a7c"
DS_PIB = "kgyi-qc7j"

# La capital de un departamento es el municipio cuyo código termina en 001.
# Se cumple en los 33, con UNA excepción: Cundinamarca, cuyo 25001 es Agua de
# Dios porque su capital es Bogotá, que es un departamento aparte (11001).
CAPITAL_DE = {"25": "11001"}

RADIO_TIERRA_KM = 6371.0


def consultar(dataset: str, params: dict) -> list[dict]:
    url = f"{BASE}/{dataset}.json"
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 400:
                raise RuntimeError(f"SoQL rechazado: {resp.text[:250]}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if intento < REINTENTOS:
                time.sleep(ESPERA * intento)
    raise RuntimeError(f"{dataset}: agotados los reintentos") from ultimo


def paginar(dataset: str, params: dict) -> pd.DataFrame:
    partes, offset = [], 0
    while True:
        lote = consultar(dataset, {**params, "$limit": PAGINA, "$offset": offset})
        if not lote:
            break
        partes.append(pd.DataFrame(lote))
        if len(lote) < PAGINA:
            break
        offset += PAGINA
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def a_coordenada(serie: pd.Series) -> pd.Series:
    """
    DIVIPOLA escribe las coordenadas con coma decimal: "-75,581775". Leídas como
    número directo quedan nulas y el punto desaparece del mapa sin avisar.
    """
    return pd.to_numeric(
        serie.astype(str).str.strip().str.replace(",", ".", regex=False),
        errors="coerce",
    )


def sin_tildes(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def distancia_km(lat1, lon1, lat2, lon2) -> float | None:
    """
    Haversine. Es distancia en línea recta, no por carretera: en Colombia, con
    tres cordilleras de por medio, la diferencia puede ser del triple. Se reporta
    como «distancia en línea recta» y nunca como tiempo de viaje.
    """
    if any(pd.isna(v) for v in (lat1, lon1, lat2, lon2)):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * RADIO_TIERRA_KM * math.asin(math.sqrt(a)), 1)


# --------------------------------------------------------------------------- #

def bajar_municipios() -> pd.DataFrame:
    LOG.info("DIVIPOLA municipios (%s)...", DS_MUNICIPIOS)
    df = paginar(DS_MUNICIPIOS, {"$order": "cod_mpio"})
    df = df.rename(columns={
        "cod_dpto": "cod_departamento", "dpto": "departamento",
        "cod_mpio": "cod_municipio", "nom_mpio": "municipio",
    })
    for col in ("cod_departamento", "cod_municipio"):
        df[col] = df[col].astype("string").str.strip()
    df["lat"] = a_coordenada(df["latitud"])
    df["lon"] = a_coordenada(df["longitud"])
    df = df.drop(columns=["latitud", "longitud"])

    perdidas = int(df["lat"].isna().sum())
    if perdidas:
        LOG.warning("%s municipios sin coordenada legible", perdidas)

    # Distancia en línea recta a la capital de su departamento: es el indicador
    # más honesto de aislamiento que se puede calcular sin datos de vías.
    capitales = {}
    for cod_dep in df["cod_departamento"].dropna().unique():
        cod_cap = CAPITAL_DE.get(cod_dep, f"{cod_dep}001")
        fila = df[df["cod_municipio"] == cod_cap]
        if not fila.empty:
            capitales[cod_dep] = (fila.iloc[0]["lat"], fila.iloc[0]["lon"], fila.iloc[0]["municipio"])
        else:
            LOG.warning("Sin capital identificable para el departamento %s", cod_dep)

    df["capital_departamento"] = df["cod_departamento"].map(lambda c: capitales.get(c, (None,) * 3)[2])
    df["km_a_capital"] = [
        distancia_km(r.lat, r.lon, *capitales.get(r.cod_departamento, (None, None, None))[:2])
        for r in df.itertuples()
    ]
    # Bogotá como segunda referencia: para quien no conoce el país, «a 180 km de
    # Bogotá» dice muchísimo más que el nombre de una capital departamental.
    bog = df[df["cod_municipio"] == "11001"]
    if not bog.empty:
        blat, blon = bog.iloc[0]["lat"], bog.iloc[0]["lon"]
        df["km_a_bogota"] = [distancia_km(r.lat, r.lon, blat, blon) for r in df.itertuples()]

    LOG.info("Municipios: %s (%s con distancia calculada)", len(df), int(df["km_a_capital"].notna().sum()))
    return df


def bajar_centros_poblados() -> pd.DataFrame:
    LOG.info("DIVIPOLA centros poblados (%s)...", DS_CENTROS)
    df = paginar(DS_CENTROS, {"$order": "codigo_centro_poblado"})
    df = df.rename(columns={
        "codigo_departamento": "cod_departamento", "nombre_departamento": "departamento",
        "codigo_municipio": "cod_municipio", "nombre_municipio": "municipio",
        "codigo_centro_poblado": "cod_lugar", "nombre_centro_poblado": "lugar",
        "tipo_centro_poblado": "tipo",
    })
    for col in ("cod_departamento", "cod_municipio", "cod_lugar"):
        df[col] = df[col].astype("string").str.strip()
    df["lat"] = a_coordenada(df["latitud"])
    df["lon"] = a_coordenada(df["longitud"])
    df = df.drop(columns=["latitud", "longitud"])
    df["tipo"] = df["tipo"].map({"CM": "cabecera municipal", "CP": "centro poblado"}).fillna(df["tipo"])
    LOG.info("Centros poblados: %s en %s municipios", len(df), df["cod_municipio"].nunique())
    return df


def bajar_departamentos() -> pd.DataFrame:
    LOG.info("DIVIPOLA departamentos (%s)...", DS_DEPARTAMENTOS)
    df = pd.DataFrame(consultar(DS_DEPARTAMENTOS, {"$limit": 100}))
    df = df.rename(columns={"cod_dpto": "cod_departamento", "nom_dpto": "departamento"})
    # Aquí los códigos vienen SIN cero a la izquierda ("5"). Sin rellenar, no
    # cruzan con los del MEN y el departamento entero se queda sin contexto.
    df["cod_departamento"] = df["cod_departamento"].astype("string").str.strip().str.zfill(2)
    df["lat"] = pd.to_numeric(df["latitud"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitud"], errors="coerce")
    df = df[["cod_departamento", "departamento", "lat", "lon"]]
    LOG.info("Departamentos: %s", len(df))
    return df


def bajar_pib() -> pd.DataFrame:
    """
    PIB por departamento y actividad económica. Responde «¿de qué vive esta
    región?», que es la primera pregunta de cualquiera que llega de afuera.

    Solo existe a nivel departamental. Atribuirlo a un municipio sería inventar,
    y por eso la ficha lo presenta siempre como contexto del departamento.
    """
    LOG.info("PIB departamental (%s)...", DS_PIB)
    df = paginar(DS_PIB, {"$where": "tipo_de_precios='PIB a precios corrientes'",
                          "$order": "a_o, c_digo_departamento_divipola"})
    df = df.rename(columns={
        "a_o": "anio", "c_digo_departamento_divipola": "cod_departamento",
        "valor_miles_de_millones_de": "valor_miles_millones",
    })
    df["cod_departamento"] = df["cod_departamento"].astype("string").str.strip().str.zfill(2)
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    df["valor_miles_millones"] = pd.to_numeric(df["valor_miles_millones"], errors="coerce")
    # El nombre viene en Mayúscula Inicial mientras el MEN lo trae en mayúscula
    # sostenida. Se normaliza para que el cruce por nombre no falle en silencio.
    df["departamento_norm"] = df["departamento"].map(sin_tildes)

    ultimo = int(df["anio"].max())
    LOG.info("PIB: %s filas, %s-%s, %s actividades (último: %s)",
             len(df), int(df["anio"].min()), ultimo, df["actividad"].nunique(), ultimo)
    return df


def resumen_economico(pib: pd.DataFrame) -> pd.DataFrame:
    """Las tres actividades principales de cada departamento en el último año."""
    ultimo = int(pib["anio"].max())
    reciente = pib[pib["anio"] == ultimo]
    filas = []
    for cod, g in reciente.groupby("cod_departamento"):
        total = g["valor_miles_millones"].sum()
        top = g.nlargest(3, "valor_miles_millones")
        filas.append({
            "cod_departamento": cod,
            "departamento": g.iloc[0]["departamento"],
            "anio_pib": ultimo,
            "pib_miles_millones": round(float(total), 1),
            "actividades_principales": list(top["actividad"]),
            "pct_actividades_principales": [
                round(float(v) / total * 100, 1) if total else None
                for v in top["valor_miles_millones"]
            ],
            "sector_dominante": top.iloc[0]["sector"] if not top.empty else None,
        })
    df = pd.DataFrame(filas)
    LOG.info("Resumen económico: %s departamentos, año %s", len(df), ultimo)
    return df


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de contexto territorial")
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument("--saltar", nargs="*", default=[],
                        choices=["municipios", "centros", "departamentos", "pib"])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-8s %(message)s")
    args.salida.mkdir(parents=True, exist_ok=True)

    resumen: list[tuple[str, str, int]] = []
    pib: pd.DataFrame | None = None

    tareas = [
        ("municipios", "territorio_municipios.parquet", bajar_municipios),
        ("centros", "territorio_centros_poblados.parquet", bajar_centros_poblados),
        ("departamentos", "territorio_departamentos.parquet", bajar_departamentos),
        ("pib", "economia_departamental.parquet", bajar_pib),
    ]
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
            if clave == "pib":
                pib = df
        except Exception as exc:  # noqa: BLE001
            LOG.error("%s: %s", clave, exc)
            resumen.append((clave, "ERROR", 0))

    if pib is not None and not pib.empty:
        try:
            res = resumen_economico(pib)
            res.to_parquet(args.salida / "economia_resumen.parquet", index=False)
            resumen.append(("resumen_econ", "OK", len(res)))
        except Exception as exc:  # noqa: BLE001
            LOG.error("resumen económico: %s", exc)

    print("\n" + "=" * 52)
    print(f"{'FUENTE':<16}{'ESTADO':<12}{'FILAS':>12}")
    print("-" * 52)
    for clave, estado, filas in resumen:
        print(f"{clave:<16}{estado:<12}{filas:>12,}".replace(",", "."))
    print("=" * 52)
    print("Veredas y polígonos municipales: no hay fuente nacional por API.")
    print("Economía municipal: solo departamental; lo municipal está en TerriData.")

    return 0 if any(e == "OK" for _, e, _ in resumen) else 1


if __name__ == "__main__":
    sys.exit(main())
