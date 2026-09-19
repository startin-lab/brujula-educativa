#!/usr/bin/env python3
"""
Brújula Educativa — Ingesta de contratación pública (SECOP)
Fundación Startin

Extrae de SECOP Integrado los contratos relacionados con educación, resumidos
por municipio, para responder "¿qué recursos ha recibido este territorio, de
quién y para qué?" en un diagnóstico previo a una intervención.

CONTEXTO VERIFICADO (19/09/2026) en Azure Cloud Shell:

  Dataset rpmr-utcd (SECOP Integrado) · 22.661.020 contratos · actualizado 2026-09
  count(*) nacional responde en 4,6 s
  Filtrado por municipio responde en 1,6 s  (Soacha: 7.790 contratos)

  Es el comportamiento OPUESTO al de los microdatos de Saber 11, que con 8,2
  millones de filas se ahoga en cualquier agrupación nacional. El tamaño no
  predice nada: cada dataset de Socrata está indexado distinto y hay que medirlo.

DOS LÍMITES QUE HAY QUE DECLARAR EN TODA RESPUESTA:

  1. `municipio_entidad` es dónde está la ENTIDAD QUE CONTRATA, no dónde se
     ejecutó el gasto. Un contrato firmado por una entidad nacional con sede en
     Bogotá para obras en Leticia aparece en Bogotá. Para un diagnóstico
     territorial esto distorsiona, y hay que decirlo.

  2. El filtro por objeto es textual y por lo tanto tosco: atrapa "educación" y
     "educativo", pero se le escapa infraestructura escolar que no usa la
     palabra. Es un indicio de magnitud, no una cifra de inversión educativa.

  La cifra correcta se enuncia así: "contratos de entidades con sede en X cuyo
  objeto menciona educación". Nunca "inversión educativa en X".

Uso:
    python ingest_secop.py --salida ./data
    python ingest_secop.py --salida ./data --municipios "Soacha" "Bogotá D.C."
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests

LOG = logging.getLogger("secop")

BASE = "https://www.datos.gov.co/resource"
DATASET_SECOP = "rpmr-utcd"
DATASET_MEN = "nudc-7mev"

TIMEOUT = 60
REINTENTOS = 3
ESPERA = 4
TOP_CONTRATOS = 10       # contratos más grandes que se guardan por municipio

# Términos que marcan un contrato como educativo. Deliberadamente amplios: es
# preferible revisar de más a perder infraestructura escolar por no nombrarla.
TERMINOS = [
    "EDUCA", "ESCOLAR", "COLEGIO", "ESTUDIANTE", "DOCENTE",
    "PEDAGOG", "ESCUELA", "ALIMENTACION ESCOLAR", "PAE",
]


def consultar(dataset: str, params: dict) -> list[dict]:
    url = f"{BASE}/{dataset}.json"
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 400:
                raise RuntimeError(f"SoQL rechazado: {resp.text[:250]}")
            if resp.status_code == 503:
                raise RuntimeError("Socrata saturado (503)")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if intento < REINTENTOS:
                time.sleep(ESPERA * intento * (5 if "503" in str(exc) else 1))
    raise RuntimeError(f"{dataset}: agotados los reintentos") from ultimo


def a_numero(serie: pd.Series) -> pd.Series:
    texto = serie.astype(str).str.strip()
    coma = texto.str.contains(r",\d{1,2}$", regex=True, na=False)
    texto = texto.where(~coma, texto.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    return pd.to_numeric(texto, errors="coerce")


def filtro_educacion() -> str:
    """Cláusula SoQL que marca un contrato como educativo por su objeto."""
    partes = [f"upper(objeto_a_contratar) LIKE '%{t}%'" for t in TERMINOS]
    return "(" + " OR ".join(partes) + ")"


def municipios_men() -> list[tuple[str, str, str]]:
    """
    (código, municipio, departamento) desde el dataset del MEN.

    SECOP guarda el municipio por NOMBRE, no por código DANE, así que hay que
    cruzarlos por texto. Los nombres del MEN vienen con tilde y los de SECOP
    también; se consulta el nombre tal cual y se registra cuando no hay match,
    porque un municipio sin coincidencia es un municipio que desaparece del
    diagnóstico sin que nadie lo note.
    """
    datos = consultar(DATASET_MEN, {
        "$select": "c_digo_municipio, municipio, departamento",
        "$group": "c_digo_municipio, municipio, departamento",
    })
    filas = [
        (str(d["c_digo_municipio"]).strip(), d["municipio"].strip(), d["departamento"].strip())
        for d in datos if d.get("municipio") and d.get("c_digo_municipio")
    ]
    LOG.info("MEN: %s municipios para cruzar contra SECOP", len(filas))
    return sorted(filas, key=lambda x: x[1])


def resumen_municipio(nombre: str) -> dict | None:
    """Conteo y valor de los contratos educativos de un municipio."""
    seguro = nombre.replace("'", "''")
    datos = consultar(DATASET_SECOP, {
        "$select": "count(*) as n, sum(valor_contrato::number) as valor",
        "$where": f"municipio_entidad='{seguro}' AND {filtro_educacion()}",
    })
    if not datos:
        return None
    d = datos[0]
    return {"n_contratos": int(d.get("n") or 0), "valor_total": float(d.get("valor") or 0)}


def mayores_contratos(nombre: str) -> list[dict]:
    """Los contratos educativos más grandes, para que el diagnóstico sea concreto."""
    seguro = nombre.replace("'", "''")
    return consultar(DATASET_SECOP, {
        "$select": ("objeto_a_contratar, valor_contrato, nom_raz_social_contratista, "
                    "nombre_de_la_entidad, fecha_de_firma_del_contrato, url_contrato, "
                    "modalidad_de_contrataci_n, estado_del_proceso"),
        "$where": f"municipio_entidad='{seguro}' AND {filtro_educacion()}",
        "$order": "valor_contrato::number DESC",
        "$limit": TOP_CONTRATOS,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de SECOP para diagnóstico territorial")
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument("--municipios", nargs="*", help="Nombres concretos; por defecto, todos")
    parser.add_argument("--sin-detalle", action="store_true", help="Solo totales, sin los contratos mayores")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )
    args.salida.mkdir(parents=True, exist_ok=True)

    if args.municipios:
        objetivo = [("", m, "") for m in args.municipios]
    else:
        objetivo = municipios_men()

    resumenes: list[dict] = []
    detalles: list[dict] = []
    sin_datos: list[str] = []
    t_total = time.time()

    for i, (cod, nombre, depto) in enumerate(objetivo, 1):
        t0 = time.time()
        try:
            res = resumen_municipio(nombre)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("  [%s/%s] %s — falló: %s", i, len(objetivo), nombre, str(exc)[:90])
            continue

        n = res["n_contratos"] if res else 0
        if n == 0:
            # Puede ser que el municipio no contrate en educación, o que su
            # nombre no coincida entre MEN y SECOP. Se registra para revisarlo:
            # no es lo mismo "no tiene contratos" que "no lo encontramos".
            sin_datos.append(nombre)
            LOG.info("  [%s/%s] %s — sin contratos educativos (%.1fs)", i, len(objetivo), nombre, time.time() - t0)
            continue

        resumenes.append({
            "cod_municipio": cod, "municipio": nombre, "departamento": depto,
            "n_contratos_educacion": n, "valor_total_educacion": res["valor_total"],
        })

        if not args.sin_detalle:
            try:
                for c in mayores_contratos(nombre):
                    c["municipio"] = nombre
                    c["cod_municipio"] = cod
                    detalles.append(c)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("  %s — detalle falló: %s", nombre, str(exc)[:90])

        LOG.info("  [%s/%s] %s — %s contratos, COP %s (%.1fs)",
                 i, len(objetivo), nombre, f"{n:,}".replace(",", "."),
                 f"{res['valor_total']:,.0f}".replace(",", "."), time.time() - t0)

    if resumenes:
        df = pd.DataFrame(resumenes)
        df.to_parquet(args.salida / "secop_municipios.parquet", index=False)
    if detalles:
        dd = pd.DataFrame(detalles)
        dd["valor_contrato"] = a_numero(dd["valor_contrato"])
        dd.to_parquet(args.salida / "secop_contratos_mayores.parquet", index=False)

    print("\n" + "=" * 62)
    print(f"Municipios consultados : {len(objetivo)}")
    print(f"Con contratos          : {len(resumenes)}")
    print(f"Sin coincidencia       : {len(sin_datos)}")
    print(f"Contratos en detalle   : {len(detalles)}")
    print(f"Tiempo total           : {(time.time() - t_total)/60:.1f} min")
    print("=" * 62)

    if sin_datos:
        # Un municipio sin coincidencia puede ser un problema de nombre, no de
        # contratación. Se listan los primeros para poder revisarlo a mano.
        print("Revisar (posible diferencia de nombre entre MEN y SECOP):")
        print("  " + ", ".join(sin_datos[:15]) + (" ..." if len(sin_datos) > 15 else ""))

    return 0 if resumenes else 1


if __name__ == "__main__":
    sys.exit(main())
