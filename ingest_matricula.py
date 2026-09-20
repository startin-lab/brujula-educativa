#!/usr/bin/env python3
"""
Brújula Educativa — Ingesta de matrícula y docentes (Ministerio de Educación)
Fundación Startin

Dos preguntas que la ficha no sabía responder: ¿cuántos estudiantes hay
matriculados aquí? y ¿cuántos docentes los atienden?

QUÉ SE INGESTA (verificado el 20/09/2026 contra datos.gov.co)

  ngw5-c5nw  MEN_MATRICULA_EN_EDUCACION_EN_PREESCOLAR, BÁSICA Y MEDIA
             38 millones de filas, 2010-2025: una por sede, grado, jornada,
             edad, género y grupo étnico. NO se descarga: se pide a Socrata que
             sume del lado del servidor, por municipio y por sede. Salen dos
             tablas de miles de filas, no una de millones.

  pgrh-8um9  MEN_DOCENTES_OFICIALES_EPBM
             Docentes del sector oficial, desde 2015. Solo existe por Entidad
             Territorial Certificada (ETC): los 32 departamentos y las ~65
             ciudades que administran su propia educación. Última carga:
             septiembre de 2023, con datos hasta 2022.

LO QUE HAY QUE DECIR SIN RODEOS

  · DOCENTES POR MUNICIPIO NO EXISTE COMO DATO ABIERTO. El MEN tenía una base
    por establecimiento (fjw5-pzau) de la que quedan vistas filtradas sueltas,
    pero el dataset padre responde 403: fue retirado. Para un municipio
    certificado el dato de su ETC ES el dato del municipio; para los demás, el
    número pertenece al departamento entero, y así se presenta.

  · EL AÑO 2022 DE DOCENTES TRAE LA MITAD QUE 2021 (328.745 frente a 659.182).
    Los años 2015-2021 vienen duplicados en la carga del MEN —la cifra
    verosímil de docentes oficiales del país ronda los 330.000—, así que el
    2022 es el bueno y los anteriores se dividen entre dos solo si hace falta
    una serie. Aquí se toma el último año y punto.

  · LA MATRÍCULA ES DEL AÑO ANTERIOR AL DE LA CARGA. El dataset dice «hasta
    2025» pero se cargó en enero de 2025: el último año completo es 2024. El
    script busca hacia atrás desde el año en curso hasta encontrar datos.

  · ES SIMAT, NO CENSO. La matrícula es la que los colegios reportan al
    sistema; los privados reportan menos y peor que los oficiales.

Uso:
    python ingest_matricula.py --salida ./data
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import requests

LOG = logging.getLogger("matricula")

BASE = "https://www.datos.gov.co/resource"
DS_MATRICULA = "ngw5-c5nw"
DS_DOCENTES = "pgrh-8um9"

# Estas consultas AGREGAN sobre 38 millones de filas. Socrata tarda entre 20 s y
# tres minutos según la caché; con menos de cinco minutos de espera la
# primera pasada del día casi siempre se corta a medias.
TIMEOUT = 300
REINTENTOS = 3
ESPERA = 20
PAGINA = 50_000


def consultar(dataset: str, params: dict) -> list[dict]:
    url = f"{BASE}/{dataset}.json"
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 400:
                raise RuntimeError(f"SoQL rechazado: {resp.text[:300]}")
            if resp.status_code == 503:
                raise RuntimeError("Socrata saturado (503)")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            LOG.warning("%s intento %s/%s falló: %s", dataset, intento, REINTENTOS, str(exc)[:160])
            if intento < REINTENTOS:
                time.sleep(ESPERA * intento)
    raise RuntimeError(f"{dataset}: agotados los reintentos") from ultimo


def paginar(dataset: str, params: dict) -> pd.DataFrame:
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


def sin_tildes(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def codigo(serie: pd.Series, ancho: int) -> pd.Series:
    return (serie.astype("string").str.strip()
            .str.replace(r"\.0$", "", regex=True).str.zfill(ancho))


def entero(serie: pd.Series) -> pd.Series:
    return pd.to_numeric(serie, errors="coerce").fillna(0).astype("int64")


# --------------------------------------------------------------------------- #
# Matrícula
# --------------------------------------------------------------------------- #

def ultimo_anio_con_datos(dataset: str, campo: str, desde: int, hasta: int) -> int | None:
    """
    Busca hacia atrás el año más reciente que tenga al menos una fila. Un
    filtro por igualdad sobre un campo indexado responde en segundos aunque
    el dataset tenga 38 millones de filas; un max() no.
    """
    for anio in range(desde, hasta - 1, -1):
        filas = consultar(dataset, {"$select": campo, "$where": f"{campo}='{anio}'", "$limit": 1})
        if filas:
            return anio
        LOG.info("%s: sin filas para %s, se prueba %s", dataset, anio, anio - 1)
    return None


def bajar_matricula_municipios(anio: int) -> pd.DataFrame:
    LOG.info("Matrícula por municipio, %s ...", anio)
    df = paginar(DS_MATRICULA, {
        "$select": "cod_dane_municipio, sector, zona, sum(total_matricula) AS matricula, "
                   "count(distinct codigo_dane_sede) AS sedes",
        "$where": f"anno_inf='{anio}'",
        "$group": "cod_dane_municipio, sector, zona",
    })
    if df.empty:
        return df
    df["cod_municipio"] = codigo(df["cod_dane_municipio"], 5)
    df["matricula"] = entero(df["matricula"])
    df["sedes"] = entero(df["sedes"])
    oficial = df["sector"].map(sin_tildes).str.startswith("OFICIAL")
    rural = df["zona"].map(sin_tildes).str.startswith("RURAL")

    salida = df.groupby("cod_municipio").agg(matricula_total=("matricula", "sum")).reset_index()
    salida["matricula_oficial"] = df[oficial].groupby("cod_municipio")["matricula"].sum()\
        .reindex(salida["cod_municipio"]).fillna(0).astype("int64").values
    salida["matricula_rural"] = df[rural].groupby("cod_municipio")["matricula"].sum()\
        .reindex(salida["cod_municipio"]).fillna(0).astype("int64").values
    salida["matricula_no_oficial"] = salida["matricula_total"] - salida["matricula_oficial"]
    salida["anio_matricula"] = anio
    LOG.info("Matrícula: %s municipios, %s estudiantes", len(salida),
             f"{int(salida['matricula_total'].sum()):,}".replace(",", "."))
    return salida


def departamentos_con_matricula(anio: int) -> list[str]:
    filas = consultar(DS_MATRICULA, {
        "$select": "cod_dane_departamento", "$where": f"anno_inf='{anio}'",
        "$group": "cod_dane_departamento", "$limit": 100,
    })
    return sorted({str(f["cod_dane_departamento"]).strip() for f in filas if f.get("cod_dane_departamento")})


def municipios_con_matricula(anio: int, cod_departamento: str) -> list[str]:
    filas = consultar(DS_MATRICULA, {
        "$select": "cod_dane_municipio",
        "$where": f"anno_inf='{anio}' AND cod_dane_departamento='{cod_departamento}'",
        "$group": "cod_dane_municipio", "$limit": 2000,
    })
    return sorted({str(f["cod_dane_municipio"]).strip() for f in filas if f.get("cod_dane_municipio")})


def _sedes_de(anio: int, filtro: str) -> pd.DataFrame:
    return paginar(DS_MATRICULA, {
        "$select": "codigo_dane_sede, cod_dane_municipio, sum(total_matricula) AS matricula",
        "$where": f"anno_inf='{anio}' AND {filtro}",
        "$group": "codigo_dane_sede, cod_dane_municipio",
    })


def bajar_matricula_sedes(anio: int) -> pd.DataFrame:
    """
    Matrícula total por sede: es lo que permite decir cuántos estudiantes tiene
    el colegio que la persona eligió en el selector.

    Se pide POR DEPARTAMENTO. Agrupar por sede el país entero en una sola
    consulta son 2,3 millones de filas para Socrata y a los siete minutos
    todavía no ha respondido (medido el 20/09/2026). Por departamento son
    treinta y tres consultas de segundos. Si un departamento tampoco responde
    —Bogotá y Antioquia son los candidatos— se baja municipio por municipio.
    """
    LOG.info("Matrícula por sede, %s, por departamento ...", anio)
    partes: list[pd.DataFrame] = []
    for dep in departamentos_con_matricula(anio):
        try:
            parte = _sedes_de(anio, f"cod_dane_departamento='{dep}'")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Departamento %s no respondió (%s); se baja por municipio", dep, str(exc)[:80])
            municipios = municipios_con_matricula(anio, dep)
            parte = pd.concat(
                [_sedes_de(anio, f"cod_dane_municipio='{m}'") for m in municipios], ignore_index=True,
            ) if municipios else pd.DataFrame()
        LOG.info("  %s: %s sedes", dep, len(parte))
        if not parte.empty:
            partes.append(parte)
    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()
    if df.empty:
        return df
    # Una sede con dos códigos de municipio (cambio de DIVIPOLA) se queda con
    # la fila mayor; sumar las dos la contaría doble.
    df["matricula"] = entero(df["matricula"])
    df = df.sort_values("matricula", ascending=False).drop_duplicates("codigo_dane_sede")
    df["cod_dane_sede"] = codigo(df["codigo_dane_sede"], 12)
    df["cod_municipio"] = codigo(df["cod_dane_municipio"], 5)
    df["anio_matricula"] = anio
    salida = df[["cod_dane_sede", "cod_municipio", "matricula", "anio_matricula"]]
    LOG.info("Matrícula: %s sedes", f"{len(salida):,}".replace(",", "."))
    return salida


# --------------------------------------------------------------------------- #
# Docentes
# --------------------------------------------------------------------------- #

def bajar_docentes(anio: int) -> pd.DataFrame:
    """
    Docentes oficiales por ETC en el último año. `codigo_sed` no es un código:
    es el NOMBRE de la entidad («Bogotá», «Boyacá», «Soacha»). Se normaliza
    para cruzarlo con la columna `etc` del MEN municipal, que trae lo mismo
    con el sufijo «(ETC)».
    """
    LOG.info("Docentes oficiales por ETC, %s ...", anio)
    df = paginar(DS_DOCENTES, {
        "$select": "codigo_sed, zona, sum(docentes_n) AS docentes",
        "$where": f"anno_inf='{anio}'",
        "$group": "codigo_sed, zona",
    })
    if df.empty:
        return df
    df["docentes"] = entero(df["docentes"])
    df["etc_norm"] = df["codigo_sed"].map(normalizar_etc)
    rural = df["zona"].map(sin_tildes).str.startswith("RURAL")
    salida = df.groupby("etc_norm").agg(etc=("codigo_sed", "first"),
                                        docentes_oficiales=("docentes", "sum")).reset_index()
    salida["docentes_rurales"] = df[rural].groupby("etc_norm")["docentes"].sum()\
        .reindex(salida["etc_norm"]).fillna(0).astype("int64").values
    salida["anio_docentes"] = anio
    total = int(salida["docentes_oficiales"].sum())
    LOG.info("Docentes: %s ETC, %s docentes oficiales", len(salida), f"{total:,}".replace(",", "."))
    # Los años anteriores a 2022 vienen duplicados en la fuente. Si el total
    # nacional no es verosímil, mejor no publicar que publicar el doble.
    if not (200_000 < total < 500_000):
        raise RuntimeError(f"Total nacional de docentes no verosímil: {total:,}")
    return salida


def normalizar_etc(nombre) -> str:
    """«Boyacá (ETC)» y «Boyacá» → «BOYACA». «Bogotá, D.C.» → «BOGOTA D.C.»."""
    t = sin_tildes(nombre)
    t = re.sub(r"\(\s*ETC\s*\)", "", t)
    # «Bogotá, D.C.» en el MEN municipal es «Bogotá» en la base de docentes.
    t = re.sub(r"\bD\.?\s*C\.?", "", t)
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de matrícula y docentes (MEN)")
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument("--anio", type=int, help="forzar el año de matrícula")
    parser.add_argument("--saltar", nargs="*", default=[], choices=["matricula", "sedes", "docentes"])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-8s %(message)s")
    args.salida.mkdir(parents=True, exist_ok=True)
    hoy = date.today().year
    resumen: list[tuple[str, str, int]] = []

    anio_mat = args.anio
    if not anio_mat and ({"matricula", "sedes"} - set(args.saltar)):
        anio_mat = ultimo_anio_con_datos(DS_MATRICULA, "anno_inf", hoy, hoy - 4)
        if not anio_mat:
            LOG.error("La matrícula no tiene filas en los últimos cinco años")
    if anio_mat:
        LOG.info("Año de matrícula: %s", anio_mat)

    tareas = [
        ("matricula", "matricula_municipios.parquet", lambda: bajar_matricula_municipios(anio_mat)),
        ("sedes", "matricula_sedes.parquet", lambda: bajar_matricula_sedes(anio_mat)),
        ("docentes", "docentes_etc.parquet", lambda: bajar_docentes(
            ultimo_anio_con_datos(DS_DOCENTES, "anno_inf", hoy, hoy - 6) or 0)),
    ]
    for clave, archivo, funcion in tareas:
        if clave in args.saltar or (clave != "docentes" and not anio_mat):
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

    print("\n" + "=" * 52)
    print(f"{'FUENTE':<16}{'ESTADO':<12}{'FILAS':>12}")
    print("-" * 52)
    for clave, estado, filas in resumen:
        print(f"{clave:<16}{estado:<12}{filas:>12,}".replace(",", "."))
    print("=" * 52)
    return 0 if any(e == "OK" for _, e, _ in resumen) else 1


if __name__ == "__main__":
    sys.exit(main())
