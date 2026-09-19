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

  La agrupación nacional por municipio SÍ responde, pero tarda 122 s (medido).
  Por eso esa consulta —y solo esa— usa un timeout aparte: con los 60 s del
  resto, fallaría siempre.

EL CRUCE DE NOMBRES, QUE ES DONDE ESTABA EL PROBLEMA

  SECOP no guarda el código DANE: guarda el municipio por nombre. Y lo escribe
  distinto que el MEN. Medido contra los 1.274 municipios del MEN:

    · 86 municipios se escriben diferente en las dos fuentes. «Santa Rosa de
      Osos» en el MEN es «Santa Rosa De Osos» en SECOP; «Villa de Leyva» es
      «Villa De Leyva»; «Paez» es «Páez». Con igualdad exacta, esos 86 —el 7 %
      del país— habrían devuelto cero contratos sin un solo error visible.

    · Bogotá aparece con DOS grafías dentro del propio SECOP: «Bogotá» (con
      departamento «Distrito Capital de Bogotá») y «Bogotá D.C.». El MEN la
      llama «Bogotá, D.C.», con coma. Ninguna de las tres coincide.

  La solución no es adivinar la ortografía del otro: es pedirle a SECOP su
  propio vocabulario una vez al inicio, normalizarlo sin tildes ni mayúsculas, y
  buscar ahí. Cobertura resultante: 1.257 de 1.274 (98,7 %).

  Los 17 que no cruzan son, casi todos, áreas no municipalizadas de Guainía,
  Vaupés, Amazonas y Chocó, que no contratan. Se listan al final de la corrida
  para poder mirarlos, en vez de desaparecer.

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
TIMEOUT_VOCABULARIO = 240   # la agrupación nacional tarda ~122 s; 60 no alcanzan
LIMITE_SOCRATA = 5000       # sin $limit, Socrata devuelve 1.000 y no avisa
REINTENTOS = 3
ESPERA = 4
TOP_CONTRATOS = 10       # contratos más grandes que se guardan por municipio

# Términos que marcan un contrato como educativo. Deliberadamente amplios: es
# preferible revisar de más a perder infraestructura escolar por no nombrarla.
TERMINOS = [
    "EDUCA", "ESCOLAR", "COLEGIO", "ESTUDIANTE", "DOCENTE",
    "PEDAGOG", "ESCUELA", "ALIMENTACION ESCOLAR", "PAE",
]


def consultar(dataset: str, params: dict, timeout: int = TIMEOUT) -> list[dict]:
    url = f"{BASE}/{dataset}.json"
    ultimo: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
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


def normalizar(nombre: str) -> str:
    """Sin tildes y en mayúsculas: es la llave con la que se cruzan las fuentes."""
    texto = unicodedata.normalize("NFKD", str(nombre or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).upper().strip()


# Bogotá aparece con dos grafías dentro del propio SECOP y con una tercera en el
# MEN. No hay regla que las una: es una excepción y se escribe como excepción.
ALIAS = {
    "BOGOTA, D.C.": ["BOGOTA", "BOGOTA D.C.", "BOGOTA, D.C."],
}


def filtro_educacion() -> str:
    """Cláusula SoQL que marca un contrato como educativo por su objeto."""
    partes = [f"upper(objeto_a_contratar) LIKE '%{t}%'" for t in TERMINOS]
    return "(" + " OR ".join(partes) + ")"


def vocabulario_secop() -> dict[str, list[str]]:
    """
    Cómo escribe SECOP cada municipio, indexado por el nombre normalizado.

    Es UNA consulta nacional al inicio de la corrida (~122 s). Cuesta dos
    minutos y evita el modo de falla más caro de todo el proyecto: un municipio
    que devuelve cero contratos no porque no contrate, sino porque su nombre
    lleva una tilde distinta. Ese error no lanza excepción y no aparece en
    ningún log; simplemente borra un territorio del diagnóstico.

    Devuelve una LISTA de grafías por nombre, no una sola, porque hay municipios
    escritos de varias formas en el mismo dataset.
    """
    LOG.info("SECOP: pidiendo el vocabulario de municipios (tarda ~2 min)...")
    t0 = time.time()
    datos = consultar(DATASET_SECOP, {
        "$select": "municipio_entidad, count(*) as n",
        "$group": "municipio_entidad",
        "$limit": LIMITE_SOCRATA,
    }, timeout=TIMEOUT_VOCABULARIO)

    vocabulario: dict[str, list[str]] = {}
    for fila in datos:
        crudo = (fila.get("municipio_entidad") or "").strip()
        if not crudo:
            continue
        vocabulario.setdefault(normalizar(crudo), []).append(crudo)

    varias = sum(1 for v in vocabulario.values() if len(v) > 1)
    LOG.info("SECOP: %s municipios distintos en %.0f s (%s con varias grafías)",
             len(vocabulario), time.time() - t0, varias)
    return vocabulario


def municipios_men() -> list[tuple[str, str, str]]:
    """
    (código, municipio, departamento) desde el dataset del MEN.

    Tres cuidados que no son opcionales:

      · `$limit`. Sin él Socrata devuelve 1.000 filas y no avisa de que hay más.
        Costó descubrirlo: el conteo daba 1.000 redondo, que es justo el número
        que debería dar sospecha.
      · La fila NACIONAL. El MEN publica un agregado del país como si fuera un
        municipio más. Sin filtrarlo se consultarían en SECOP los contratos de
        un municipio llamado «NACIONAL».
      · Duplicados por código. Un municipio cuyo nombre cambió de grafía entre
        vigencias aparece dos veces. Se queda la grafía más reciente.
    """
    datos = consultar(DATASET_MEN, {
        "$select": "c_digo_municipio, municipio, departamento",
        "$group": "c_digo_municipio, municipio, departamento",
        "$limit": LIMITE_SOCRATA,
    })
    vistos: dict[str, tuple[str, str, str]] = {}
    for d in datos:
        codigo = str(d.get("c_digo_municipio") or "").strip()
        nombre = (d.get("municipio") or "").strip()
        if not codigo or not nombre or normalizar(nombre) == "NACIONAL":
            continue
        vistos[codigo] = (codigo, nombre, (d.get("departamento") or "").strip())

    filas = sorted(vistos.values(), key=lambda x: x[1])
    LOG.info("MEN: %s municipios para cruzar contra SECOP", len(filas))
    return filas


def grafias_secop(nombre: str, vocabulario: dict[str, list[str]]) -> list[str]:
    """
    Cómo llamar a este municipio para que SECOP lo encuentre.

    Devuelve lista vacía cuando no hay coincidencia, y esa lista vacía es
    información: significa «no lo encontramos», que no es lo mismo que «no tiene
    contratos». Las dos cosas se reportan por separado al final de la corrida.
    """
    clave = normalizar(nombre)
    if clave in ALIAS:
        grafias: list[str] = []
        for variante in ALIAS[clave]:
            grafias.extend(vocabulario.get(normalizar(variante), []))
        if grafias:
            return sorted(set(grafias))
    return sorted(set(vocabulario.get(clave, [])))


def clausula_municipio(grafias: list[str]) -> str:
    """`IN (...)` con todas las grafías, porque un municipio puede tener varias."""
    lista = ", ".join("'" + g.replace("'", "''") + "'" for g in grafias)
    return f"municipio_entidad IN ({lista})"


def resumen_municipio(grafias: list[str]) -> dict | None:
    """Conteo y valor de los contratos educativos de un municipio."""
    datos = consultar(DATASET_SECOP, {
        "$select": "count(*) as n, sum(valor_contrato::number) as valor",
        "$where": f"{clausula_municipio(grafias)} AND {filtro_educacion()}",
    })
    if not datos:
        return None
    d = datos[0]
    return {"n_contratos": int(d.get("n") or 0), "valor_total": float(d.get("valor") or 0)}


def mayores_contratos(grafias: list[str]) -> list[dict]:
    """Los contratos educativos más grandes, para que el diagnóstico sea concreto."""
    return consultar(DATASET_SECOP, {
        "$select": ("objeto_a_contratar, valor_contrato, nom_raz_social_contratista, "
                    "nombre_de_la_entidad, fecha_de_firma_del_contrato, url_contrato, "
                    "modalidad_de_contrataci_n, estado_del_proceso"),
        "$where": f"{clausula_municipio(grafias)} AND {filtro_educacion()}",
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

    vocabulario = vocabulario_secop()

    if args.municipios:
        objetivo = [("", m, "") for m in args.municipios]
    else:
        objetivo = municipios_men()

    resumenes: list[dict] = []
    detalles: list[dict] = []
    sin_contratos: list[str] = []   # existe en SECOP, pero sin contratos educativos
    sin_cruce: list[str] = []       # no lo encontramos en el vocabulario de SECOP
    fallidos: list[str] = []
    t_total = time.time()

    for i, (cod, nombre, depto) in enumerate(objetivo, 1):
        grafias = grafias_secop(nombre, vocabulario)
        if not grafias:
            # NO es lo mismo que «no tiene contratos». Es que no lo encontramos,
            # y confundir las dos cosas es exactamente como un territorio
            # desaparece de un diagnóstico sin que nadie lo note.
            sin_cruce.append(f"{nombre} ({depto})")
            LOG.info("  [%s/%s] %s — sin coincidencia de nombre en SECOP", i, len(objetivo), nombre)
            continue

        t0 = time.time()
        try:
            res = resumen_municipio(grafias)
        except Exception as exc:  # noqa: BLE001
            fallidos.append(nombre)
            LOG.warning("  [%s/%s] %s — falló: %s", i, len(objetivo), nombre, str(exc)[:90])
            continue

        n = res["n_contratos"] if res else 0
        if n == 0:
            sin_contratos.append(nombre)
            LOG.info("  [%s/%s] %s — sin contratos educativos (%.1fs)",
                     i, len(objetivo), nombre, time.time() - t0)
            continue

        resumenes.append({
            "cod_municipio": cod, "municipio": nombre, "departamento": depto,
            "grafias_secop": grafias,
            "n_contratos_educacion": n, "valor_total_educacion": res["valor_total"],
        })

        if not args.sin_detalle:
            try:
                for c in mayores_contratos(grafias):
                    c["municipio"] = nombre
                    c["cod_municipio"] = cod
                    detalles.append(c)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("  %s — detalle falló: %s", nombre, str(exc)[:90])

        LOG.info("  [%s/%s] %s — %s contratos, COP %s (%.1fs)",
                 i, len(objetivo), nombre, f"{n:,}".replace(",", "."),
                 f"{res['valor_total']:,.0f}".replace(",", "."), time.time() - t0)

    if resumenes:
        pd.DataFrame(resumenes).to_parquet(args.salida / "secop_municipios.parquet", index=False)
    if detalles:
        dd = pd.DataFrame(detalles)
        dd["valor_contrato"] = a_numero(dd["valor_contrato"])
        dd.to_parquet(args.salida / "secop_contratos_mayores.parquet", index=False)

    consultados = len(objetivo)
    print("\n" + "=" * 62)
    print(f"Municipios del MEN     : {consultados}")
    print(f"Con contratos          : {len(resumenes)}")
    print(f"Sin contratos          : {len(sin_contratos)}")
    print(f"Sin cruce de nombre    : {len(sin_cruce)}")
    print(f"Consultas fallidas     : {len(fallidos)}")
    print(f"Contratos en detalle   : {len(detalles)}")
    if consultados:
        print(f"Cobertura del cruce    : {(consultados - len(sin_cruce)) / consultados * 100:.1f} %")
    print(f"Tiempo total           : {(time.time() - t_total)/60:.1f} min")
    print("=" * 62)

    if sin_cruce:
        # Se listan TODOS, no una muestra: son los territorios que quedarían
        # fuera del diagnóstico, y eso hay que poder revisarlo entero.
        print(f"Sin coincidencia en SECOP ({len(sin_cruce)}):")
        for m in sin_cruce:
            print("   ·", m)
        print("  La mayoría suelen ser áreas no municipalizadas, que no contratan.")
    if fallidos:
        print(f"Fallaron por error de consulta ({len(fallidos)}): " + ", ".join(fallidos[:15]))

    return 0 if resumenes else 1


if __name__ == "__main__":
    sys.exit(main())
