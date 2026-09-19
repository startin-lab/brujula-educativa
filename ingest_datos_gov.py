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

# Medido el 19/09/2026 contra la API real: una consulta departamental que va a
# funcionar responde entre 0,4 s (caché caliente) y 12 s (frío). Si pasa de un
# minuto no va a terminar nunca — Socrata simplemente deja la conexión abierta.
# Un timeout de 240 s con 3 reintentos son DOCE MINUTOS perdidos en un solo
# departamento antes de subdividirlo. Fallar rápido es lo que hace viable la
# subdivisión adaptativa.
TIMEOUT = 60

DATASET_MEN = "nudc-7mev"
DATASET_CPE = "pyqj-s96k"
DATASET_SABER = "kgxf-xxbe"

# Periodos de Saber 11 con cobertura nacional (calendario A). Los de primer
# semestre traen ~15.000 estudiantes de calendario B y se incluyen aparte.
# Periodos de Saber 11 que SÍ son comparables entre sí.
#
# EL CORTE DE 2014 NO ES UN DETALLE TÉCNICO: ES UN EXAMEN DISTINTO.
#
# Verificado el 19/09/2026 consultando kgxf-xxbe periodo por periodo. El dataset
# trae 23 periodos desde 2010, pero antes de 2014-2 la prueba tenía otra
# estructura y otras áreas:
#
#   periodo   lectura   matemáticas   c. naturales   inglés   global
#   20111     —         60.14         —              79.55    —
#   20122     —         45            —              48       —
#   20141     —         55            —              75       —
#   20142     35        45            43             42       212
#   20194     69        66            65             71       339
#
# Hasta 2014-1 no existen `punt_lectura_critica` ni `punt_global`: vienen nulos,
# porque esas áreas no se evaluaban así. Y los puntajes de 2010-2011 son
# decimales sobre otra escala —de ahí los "35,2" que revientan el cast a
# número—, no enteros comparables con los de hoy.
#
# Ingerir esos periodos no produciría un error: produciría una SERIE FALSA. Un
# municipio mostraría una "evolución" de 2010 a 2022 cuyo primer tramo mide otra
# cosa. Para una herramienta cuyo propósito es sustentar diagnósticos, inventar
# una tendencia es peor que no tenerla.
#
# Por eso la serie arranca en 2014-2 y ahí se queda.
PERIODOS_SABER = ["20194", "20201", "20211", "20221", "20224"]

# POR QUÉ CINCO PERIODOS Y NO ONCE
#
# Los microdatos son la fuente más lenta del proyecto: cada periodo se trocea en
# 33 departamentos y varios se subdividen por municipio. Once periodos son horas
# de ingesta. La pregunta correcta no es cuántos caben, sino qué aporta cada uno
# que no esté ya en otra parte.
#
#   · El resultado por sede de los años recientes YA VIENE en los archivos
#     agregados del ICFES (2021-1 a 2025-2, seis periodos usables). Reconstruirlo
#     desde los microdatos es repetir trabajo con más latencia y más riesgo.
#
#   · Lo que SOLO está en los microdatos es la conectividad declarada del hogar
#     —internet y computador—, que es la medida de brecha digital que ninguna
#     otra fuente tiene a este detalle. Y eso se detiene en 2022.
#
# Quedan los dos grandes censos nacionales (20194 con 1.096.524 registros y
# 20224 con 1.065.888) más los intermedios, que dan el antes y el después de la
# pandemia sobre la brecha digital. 20194 es de 2019 y se sale del corte de
# cinco años a propósito: sin él no hay con qué comparar 2022.

# Los que existen pero NO se ingieren, con su razón. Se dejan escritos para que
# nadie los "recupere" dentro de seis meses creyendo que fue un olvido.
PERIODOS_EXCLUIDOS = {
    "20101": "prueba anterior a la reforma de 2014",
    "20102": "prueba anterior a la reforma de 2014",
    "20111": "prueba anterior a la reforma de 2014; puntajes decimales en otra escala",
    "20112": "prueba anterior a la reforma de 2014; puntajes decimales en otra escala",
    "20121": "prueba anterior a la reforma de 2014",
    "20122": "prueba anterior a la reforma de 2014",
    "20131": "prueba anterior a la reforma de 2014",
    "20132": "prueba anterior a la reforma de 2014",
    "20141": "prueba anterior a la reforma de 2014",
    "20151": "calendario B, ~26.000 evaluados: no da cobertura nacional",
    "20161": "calendario B",
    "20171": "calendario B",
    # Fuera del alcance por decisión, no por defecto de la fuente: el resultado
    # por sede de estos años lo cubren mejor los archivos agregados del ICFES.
    "20142": "cubierto por los agregados del ICFES; fuera del corte de 5 años",
    "20152": "cubierto por los agregados del ICFES; fuera del corte de 5 años",
    "20162": "cubierto por los agregados del ICFES; fuera del corte de 5 años",
    "20172": "cubierto por los agregados del ICFES; fuera del corte de 5 años",
    "20181": "cubierto por los agregados del ICFES; fuera del corte de 5 años",
    "20191": "calendario B, 12.561 evaluados",
}

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
            resp = requests.get(url, params=params, timeout=TIMEOUT)
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
    """
    Computadores Para Educar: qué recibió cada municipio del programa nacional.

    Es el registro de «esto ya te lo dieron», que es justo lo que hace falta
    antes de prometerle equipos a una institución. Pero el dataset tiene tres
    trampas que lo vuelven peligroso si se lee de frente. Verificadas el
    19/09/2026 contando valores distintos por año sobre los 1.121 municipios:

    1. TRES COLUMNAS SON CIFRAS NACIONALES REPETIDAS EN CADA FILA.
       `meta_terminales_entregadas`, `meta_docentes_formados` y
       —la más traicionera— `sedes_beneficiadas` tienen UN SOLO valor distinto
       en todo el país para cada año. Un municipio de Santander aparece en 2012
       con 155 computadores entregados y, al lado, «3.889 sedes beneficiadas» y
       una meta de 79.899 terminales. No son suyas: son del programa entero.
       Publicarlas por municipio diría que cada uno de los 1.121 recibió lo
       mismo que el país. Se descartan.

    2. `ni_os_por_terminal` ES DEPARTAMENTAL, NO MUNICIPAL.
       Tiene entre 33 y 35 valores distintos por año: uno por departamento. El
       municipal es `ni_os_por_terminal_municipal`. Los nombres invitan al error
       exacto —usar el departamental como si fuera del municipio—, así que aquí
       se renombran para que el nombre diga lo que el dato es.

    3. DESDE 2020 HAY UNA FILA POR MES, NO POR AÑO.
       Hasta 2019 son 1.121 filas anuales, una por municipio. En 2020 son
       11.210, en 2021 son 14.581: cortes mensuales acumulados dentro de cada
       vigencia, más una fila basura con fecha_corte 1900-01-01. Sumarlas
       multiplicaría lo entregado por doce. Se conserva el último corte de cada
       municipio-año, que es el acumulado de esa vigencia.

    Lo que queda es historia, no presente: el programa entrega a 832-1.106
    municipios al año entre 2010 y 2015, baja a 354 en 2017, a 65 en 2019, y el
    dataset deja de actualizarse en febrero de 2023.
    """
    LOG.info("CPE (%s): descargando...", DATASET_CPE)
    df = paginar(DATASET_CPE, {"$order": "anio,coddane"})

    # Cifras del programa nacional repetidas en cada fila municipal. Fuera.
    NACIONALES = [
        "meta_terminales_entregadas", "meta_docentes_formados",
        "meta_padres_capacitados", "meta_retoma_de_pc", "meta_demanufactura",
        "meta_docentes_acompa_ados", "sedes_beneficiadas",
    ]
    sobran = [c for c in NACIONALES if c in df.columns]
    df = df.drop(columns=sobran)
    LOG.info("CPE: descartadas %s columnas con cifras nacionales repetidas", len(sobran))

    renombres = {
        "coddane": "cod_municipio",
        # El nombre corto es el departamental: se explicita para que nadie lo
        # confunda con el del municipio.
        "ni_os_por_terminal": "ninos_por_terminal_departamental",
        "ni_os_por_terminal_municipal": "ninos_por_terminal",
        "inversi_n": "inversion",
        "docentes_acompa_ados": "docentes_acompanados",
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

    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")

    # Fila centinela del propio dataset. No es un corte real.
    antes = len(df)
    df = df[df["fecha_corte"].dt.year.ne(1900) | df["fecha_corte"].isna()]
    if antes != len(df):
        LOG.info("CPE: descartadas %s filas con fecha_corte 1900-01-01", antes - len(df))

    # Un solo registro por municipio-año: el último corte de la vigencia.
    antes = len(df)
    df = (df.sort_values(["cod_municipio", "anio", "fecha_corte"])
            .drop_duplicates(subset=["cod_municipio", "anio"], keep="last"))
    if antes != len(df):
        LOG.info("CPE: %s filas mensuales colapsadas a %s municipio-año", antes, len(df))

    # Todo lo entregado en el año, venga de MinTIC o de la entidad territorial.
    entregas = ["pc_entregados_mintic", "tabletas_mintic_estudiantes",
                "tabletas_mintic_docentes", "pc_mintic_docentes",
                "pc_aportados_por_et", "tabletas_aportadas_por_et"]
    presentes = [c for c in entregas if c in df.columns]
    df["terminales_entregadas"] = df[presentes].fillna(0).sum(axis=1) if presentes else 0

    activos = int((df["terminales_entregadas"] > 0).sum())
    LOG.info("CPE: %s municipio-año (%s con entregas), %s-%s",
             len(df), activos, int(df["anio"].min()), int(df["anio"].max()))
    return df


# --------------------------------------------------------------------------- #
# ICFES — agregado por colegio desde los microdatos
# --------------------------------------------------------------------------- #

def codigos_departamento() -> list[str]:
    """
    Códigos DANE de departamento, sacados del dataset del MEN.

    Pedirlos al dataset de microdatos de Saber 11 (un DISTINCT sobre 8,2 millones
    de filas sin filtro) devuelve HTTP 500 — verificado el 19/09/2026. El del MEN
    tiene 15.707 filas y responde al instante.

    Se trocea por CÓDIGO y no por nombre a propósito: los nombres difieren entre
    fuentes por tildes y variantes ("BOGOTÁ" vs "Bogotá, D.C."), y una diferencia
    de escritura se traduce en un departamento entero que falta sin que nadie lo note.
    """
    datos = consultar(DATASET_MEN, {"$select": "c_digo_departamento", "$group": "c_digo_departamento"})
    crudos = {str(d["c_digo_departamento"]).strip().zfill(2) for d in datos if d.get("c_digo_departamento")}
    # El MEN trae códigos espurios como "0" y "00" que no corresponden a ningún
    # departamento. Consultarlos cuesta 17 s y devuelve cero filas.
    codigos = sorted(c for c in crudos if c not in {"00", "0"} and c.isdigit())
    LOG.info("Saber 11: se trocea en %s departamentos", len(codigos))
    return codigos


def municipios_de(cod_departamento: str) -> list[str]:
    """Códigos de municipio de un departamento, para subdividir cuando haga falta."""
    datos = consultar(DATASET_MEN, {
        "$select": "c_digo_municipio",
        "$where": f"c_digo_departamento='{cod_departamento}' OR c_digo_departamento='{cod_departamento.lstrip('0')}'",
        "$group": "c_digo_municipio",
    })
    return sorted({str(d["c_digo_municipio"]).strip() for d in datos if d.get("c_digo_municipio")})


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
    # Una serie que mezcle pruebas distintas es peor que una serie corta: la
    # segunda se nota, la primera no. Si alguien pide un periodo excluido, se
    # rechaza con el motivo, no se ingiere en silencio.
    invalidos = [p for p in periodos if p in PERIODOS_EXCLUIDOS]
    if invalidos:
        for p_ in invalidos:
            LOG.error("Periodo %s excluido: %s", p_, PERIODOS_EXCLUIDOS[p_])
        raise SystemExit(
            "Los periodos anteriores a 2014-2 miden otra prueba y no son comparables. "
            "Si de verdad los necesitas para un análisis aparte, sácalos en otra corrida "
            "y no los mezcles con la serie."
        )

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

    def consultar_territorio(periodo: str, campo: str, codigo: str, sel: str, grp: str) -> list[dict]:
        """
        Consulta acotada a un territorio. El MEN guarda los códigos con cero a la
        izquierda ("05") y Saber 11 no siempre; se piden las dos formas, porque
        asumir una sola hace desaparecer un territorio entero sin error visible.
        """
        variantes = sorted({codigo, codigo.lstrip("0"), codigo.zfill(2 if campo.endswith("depto_ubicacion") else 5)} - {""})
        lista = ", ".join(f"'{v}'" for v in variantes)
        return consultar(DATASET_SABER, {
            "$select": sel,
            "$where": f"periodo='{periodo}' AND {campo} IN ({lista})",
            "$group": grp,
            "$limit": PAGINA,
        })

    deptos = codigos_departamento()
    partes: list[pd.DataFrame] = []

    for periodo in periodos:
        del_periodo: list[pd.DataFrame] = []
        fallidos: list[str] = []
        t_periodo = time.time()

        for i, depto in enumerate(deptos, 1):
            # Se informa cada departamento: una corrida de varios minutos sin una
            # sola línea de salida es indistinguible de un proceso colgado, y lo
            # primero que hace quien la ve es matarla.
            t0 = time.time()
            try:
                datos = consultar_territorio(periodo, "cole_cod_depto_ubicacion", depto, seleccion, agrupacion)
                LOG.info("  [%s/%s] %s depto %s — %s colegios en %.1fs",
                         i, len(deptos), periodo, depto, len(datos), time.time() - t0)
            except Exception as exc:  # noqa: BLE001
                # Los departamentos grandes (Antioquia, Valle) tienen tantos
                # estudiantes que el motor de Socrata también se rinde con ellos.
                # Verificado el 19/09/2026: Atlántico y Bogotá responden en menos
                # de 3 s, Antioquia devuelve HTTP 500. En vez de subdividir todo
                # el país por municipio —1.122 consultas por periodo— solo se
                # subdivide el departamento que falla.
                LOG.info("Saber 11 %s / depto %s: no cabe en una consulta (%s). Subdividiendo por municipio.",
                         periodo, depto, str(exc)[:60])
                logrados = 0
                for mcpio in municipios_de(depto):
                    try:
                        d2 = consultar_territorio(periodo, "cole_cod_mcpio_ubicacion", mcpio, seleccion, agrupacion)
                    except Exception:  # noqa: BLE001
                        continue
                    if d2:
                        del_periodo.append(pd.DataFrame(d2))
                        logrados += 1
                if logrados == 0:
                    fallidos.append(depto)
                else:
                    LOG.info("Saber 11 %s / depto %s: recuperado con %s municipios", periodo, depto, logrados)
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

        minutos = (time.time() - t_periodo) / 60
        if fallidos:
            LOG.error(
                "Saber 11 %s: %s colegios en %.1f min, pero FALTAN %s departamentos (%s). "
                "El periodo queda incompleto; vuelve a correrlo antes de publicar.",
                periodo, len(df), minutos, len(fallidos), ", ".join(fallidos[:5]),
            )
        else:
            LOG.info("Saber 11 %s: %s colegios en %s departamentos (%.1f min)",
                     periodo, len(df), len(deptos), minutos)

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
    parser.add_argument("--periodos", nargs="*", default=PERIODOS_SABER,
                        help="Periodos de Saber 11; por defecto solo los comparables (>= 2014-2)")
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
