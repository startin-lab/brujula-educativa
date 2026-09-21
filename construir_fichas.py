#!/usr/bin/env python3
"""
Brújula Educativa — Construcción de fichas precalculadas
Fundación Startin

Convierte los parquet crudos de la ingesta en dos tablas que ya traen la
respuesta hecha: una ficha por municipio y una ficha por sede.

POR QUÉ FICHAS Y NO CONSULTAS EN VIVO

  El agente no debe agregar millones de filas cada vez que alguien pregunta por
  Soacha. Con fichas, responder es una búsqueda por clave: costo casi nulo,
  latencia de milisegundos y —lo que más importa— la misma cifra para la misma
  pregunta, hoy y en tres meses. Un diagnóstico que cambia de número entre dos
  reuniones no sirve para sustentar nada.

  La ingesta se corre una vez al mes. Las fichas se reconstruyen después. Entre
  corridas, todo lo que el agente dice es reproducible y tiene fecha de corte.

QUÉ ES UNA FICHA

  No es un volcado de indicadores. Es el material de un diagnóstico previo:
  cobertura, deserción, resultados, brecha digital, contratación, y encima
  SEÑALES — puntos donde el dato público y lo que una institución suele afirmar
  no coinciden. Las señales no acusan a nadie; marcan dónde mirar.

CÓMO SE COMPARA

  Siempre dentro del departamento, nunca contra el país. Comparar un colegio de
  Guainía con el promedio nacional no informa: lo compara contra Bogotá. La
  posición relativa que sirve para focalizar esfuerzos es la que se mide entre
  pares del mismo territorio.

Uso:
    python construir_fichas.py --datos ./data --salida ./data
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

LOG = logging.getLogger("fichas")

# Una sede con pocos evaluados no admite promedio publicable ni comparación.
MUESTRA_MINIMA = 10

# Periodos que se miran para juzgar una tendencia. Menos de tres es ruido.
PERIODOS_TENDENCIA = 3

# Umbrales de señal. Son convenciones de trabajo de la fundación, no normas
# oficiales; van declaradas en los metadatos para que nadie los lea como ley.
UMBRAL_COBERTURA_BAJA = 80.0      # cobertura neta, %
UMBRAL_DESERCION_ALTA = 5.0       # deserción, %
UMBRAL_INTERNET_BAJO = 40.0       # hogares con internet, %
UMBRAL_CAIDA_PUNTOS = 5.0         # puntos de caída sostenida en Saber 11

ARCHIVOS = {
    "men": "men_municipios.parquet",
    "cpe": "computadores_educar.parquet",
    "saber11": "saber11_colegios.parquet",
    "icfes": "saber11_agregado.parquet",
    "secop": "secop_municipios.parquet",
    "geo_mun": "territorio_municipios.parquet",
    "geo_cp": "territorio_centros_poblados.parquet",
    "economia": "economia_resumen.parquet",
    "poblacion": "poblacion_municipios.parquet",
    "matricula": "matricula_municipios.parquet",
    "matricula_sede": "matricula_sedes.parquet",
    "docentes": "docentes_etc.parquet",
}


def registrar(con: duckdb.DuckDBPyConnection, datos: Path) -> dict[str, bool]:
    """
    Crea una vista por archivo disponible. Las fuentes que falten no tumban la
    corrida: una ficha con menos fuentes sigue sirviendo, una corrida abortada
    no sirve de nada. Lo que falta queda anotado en los metadatos.
    """
    presentes: dict[str, bool] = {}
    for clave, archivo in ARCHIVOS.items():
        ruta = datos / archivo
        presentes[clave] = ruta.exists()
        if ruta.exists():
            con.execute(f"CREATE OR REPLACE VIEW {clave} AS SELECT * FROM read_parquet('{ruta}')")
            # Cinturón además de tirantes: la ingesta ya rellena los códigos,
            # pero las fichas se construyen sobre parquet que pueden venir de
            # otra corrida o de otra mano. Un código sin cero a la izquierda no
            # cruza con nada y no lanza ningún error: parte el municipio en dos.
            columnas = {c[0] for c in con.execute(f"DESCRIBE {clave}").fetchall()}
            anchos = {"cod_municipio": 5, "cod_departamento": 2}
            a_rellenar = {c: n for c, n in anchos.items() if c in columnas}
            if a_rellenar:
                rellenos = ", ".join(
                    f"lpad(CAST({c} AS VARCHAR), {n}, '0') AS {c}"
                    for c, n in a_rellenar.items()
                )
                con.execute(
                    f"CREATE OR REPLACE VIEW {clave} AS "
                    f"SELECT * EXCLUDE ({', '.join(a_rellenar)}), {rellenos} "
                    f"FROM read_parquet('{ruta}')"
                )
            n = con.execute(f"SELECT count(*) FROM {clave}").fetchone()[0]
            LOG.info("  %-8s %8s filas  (%s)", clave, f"{n:,}".replace(",", "."), archivo)
        else:
            LOG.warning("  %-8s AUSENTE            (%s)", clave, archivo)
    return presentes


# --------------------------------------------------------------------------- #
# Ficha municipal
# --------------------------------------------------------------------------- #

SQL_MEN_ULTIMO = """
-- Último año con dato POR INDICADOR, no por municipio. El MEN publica filas
-- incompletas: un municipio puede tener cobertura 2024 y deserción solo hasta
-- 2022. Tomar "la fila más reciente" devolvería nulos donde sí hay dato.
CREATE OR REPLACE TABLE men_ultimo AS
WITH base AS (
    SELECT cod_municipio, municipio, departamento, cod_departamento, anio, etc,
           cobertura_neta, cobertura_bruta, desercion, aprobacion, reprobacion,
           repitencia, tasa_matriculacion, poblacion_5_16, desercion_sospechosa
    FROM men
    WHERE cod_municipio IS NOT NULL
      -- La fila «NACIONAL» (código de departamento 00) no es un municipio:
      -- trae la población del país entero y arruina cualquier ponderación.
      AND COALESCE(cod_departamento, '') <> '00'
      AND strip_accents(upper(COALESCE(municipio, ''))) <> 'NACIONAL'
)
SELECT
    cod_municipio,
    any_value(municipio      ORDER BY anio DESC)      AS municipio,
    any_value(departamento   ORDER BY anio DESC)      AS departamento,
    any_value(cod_departamento ORDER BY anio DESC)    AS cod_departamento,
    -- La Entidad Territorial Certificada que administra la educación del
    -- municipio: la llave para cruzar docentes, que solo existen a ese nivel.
    any_value(etc ORDER BY anio DESC) FILTER (etc IS NOT NULL) AS etc,
    max(anio)                                          AS anio_men,
    arg_max(cobertura_neta,      anio) FILTER (cobertura_neta      IS NOT NULL) AS cobertura_neta,
    arg_max(cobertura_bruta,     anio) FILTER (cobertura_bruta     IS NOT NULL) AS cobertura_bruta,
    arg_max(desercion,           anio) FILTER (desercion           IS NOT NULL) AS desercion,
    arg_max(aprobacion,          anio) FILTER (aprobacion          IS NOT NULL) AS aprobacion,
    arg_max(reprobacion,         anio) FILTER (reprobacion         IS NOT NULL) AS reprobacion,
    arg_max(repitencia,          anio) FILTER (repitencia          IS NOT NULL) AS repitencia,
    arg_max(tasa_matriculacion,  anio) FILTER (tasa_matriculacion  IS NOT NULL) AS tasa_matriculacion,
    arg_max(poblacion_5_16,      anio) FILTER (poblacion_5_16      IS NOT NULL) AS poblacion_5_16,
    arg_max(anio, anio) FILTER (desercion IS NOT NULL)              AS anio_desercion,
    -- Cuántos años ha reportado 0 % de deserción teniendo cobertura baja.
    -- Un año puede ser un traspié de reporte; cinco seguidos es un patrón.
    count(*) FILTER (WHERE desercion_sospechosa)                    AS anios_desercion_cero
FROM base
GROUP BY cod_municipio
"""

SQL_POBLACION_ACTUAL = """
-- La proyección del año en curso. Si el corte se construye en un año que el
-- archivo aún no cubre (no debería: llega a 2042), se toma el último disponible.
CREATE OR REPLACE TABLE poblacion_actual AS
WITH objetivo AS (
    SELECT CASE WHEN max(anio) >= {anio} THEN {anio} ELSE max(anio) END AS a FROM poblacion
)
SELECT p.cod_municipio, p.anio AS anio_poblacion, p.poblacion_total, p.poblacion_cabecera,
       p.poblacion_rural, p.poblacion_5_16_dane, p.poblacion_5_18_dane, p.actualizacion_dane
FROM poblacion p, objetivo WHERE p.anio = objetivo.a
"""


def normalizar_etc(nombre) -> str:
    """«Boyacá (ETC)», «Boyacá» y «BOYACA» son la misma entidad."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", str(nombre or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    t = re.sub(r"\(\s*ETC\s*\)", "", t)
    # «Bogotá, D.C.» en el MEN municipal es «Bogotá» en la base de docentes.
    t = re.sub(r"\bD\.?\s*C\.?", "", t)
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


SQL_SABER_MUNICIPIO = """
-- Resultados agregados al municipio desde los microdatos por colegio. Se
-- ponderan por evaluados: el promedio de promedios le daría el mismo peso a un
-- colegio de 12 estudiantes que a uno de 600.
CREATE OR REPLACE TABLE saber_municipio AS
WITH ultimo AS (SELECT max(periodo) AS p FROM saber11)
SELECT
    s.cod_municipio,
    (SELECT p FROM ultimo)                                            AS periodo_saber,
    count(*)                                                          AS sedes_evaluadas,
    sum(s.evaluados)                                                  AS evaluados,
    round(sum(s.prom_lectura     * s.evaluados) / sum(s.evaluados), 1) AS prom_lectura,
    round(sum(s.prom_matematicas * s.evaluados) / sum(s.evaluados), 1) AS prom_matematicas,
    round(sum(s.prom_naturales   * s.evaluados) / sum(s.evaluados), 1) AS prom_naturales,
    round(sum(s.prom_sociales    * s.evaluados) / sum(s.evaluados), 1) AS prom_sociales,
    round(sum(s.prom_ingles      * s.evaluados) / sum(s.evaluados), 1) AS prom_ingles,
    round(sum(s.con_internet)    * 100.0 / sum(s.evaluados), 1)        AS pct_internet,
    round(sum(s.con_computador)  * 100.0 / sum(s.evaluados), 1)        AS pct_computador
FROM saber11 s, ultimo
WHERE s.periodo = ultimo.p AND s.evaluados > 0
GROUP BY s.cod_municipio
"""

SQL_CPE_ULTIMO = """
-- Computadores Para Educar. Sumar aquí es correcto SOLO porque la ingesta ya
-- dejó un registro por municipio-año; antes había un corte mensual desde 2020 y
-- sumarlos multiplicaba lo entregado por doce.
--
-- Se guardan dos cosas distintas y se nombran distinto: el ACUMULADO histórico
-- (qué recibió este municipio en toda la vida del programa, que es lo que sirve
-- para no volver a prometer lo ya entregado) y el ÚLTIMO AÑO CON ACTIVIDAD, que
-- casi nunca es reciente: el programa se apaga después de 2019.
CREATE OR REPLACE TABLE cpe_ultimo AS
SELECT
    cod_municipio,
    max(anio)                                                   AS anio_cpe,
    max(anio) FILTER (terminales_entregadas > 0)                AS ultimo_anio_con_entregas,
    sum(COALESCE(terminales_entregadas, 0))                     AS terminales_historicas,
    sum(COALESCE(docentes_formados, 0))                         AS docentes_formados_historico,
    sum(COALESCE(inversion, 0))                                 AS inversion_historica,
    arg_max(ninos_por_terminal, anio) FILTER (ninos_por_terminal IS NOT NULL)
                                                                AS ninos_por_terminal,
    arg_max(anio, anio) FILTER (ninos_por_terminal IS NOT NULL)  AS anio_ninos_por_terminal
FROM cpe
WHERE cod_municipio IS NOT NULL
GROUP BY cod_municipio
"""


def columnas_de(con: duckdb.DuckDBPyConnection, vista: str) -> set[str]:
    return {r[0] for r in con.execute(f"DESCRIBE {vista}").fetchall()}


def construir_municipios(con: duckdb.DuckDBPyConnection, hay: dict[str, bool]) -> pd.DataFrame:
    if not hay["men"]:
        raise SystemExit("men_municipios.parquet es obligatorio: define el universo de municipios.")

    # El MEN cambia columnas entre vigencias. Se rellena lo ausente con NULL en
    # vez de fallar: perder un indicador es aceptable, perder la corrida no.
    presentes = columnas_de(con, "men")
    esperadas = ["cobertura_neta", "cobertura_bruta", "desercion", "aprobacion", "reprobacion",
                 "repitencia", "tasa_matriculacion", "poblacion_5_16", "cod_departamento",
                 "desercion_sospechosa", "etc"]
    faltantes = [c for c in esperadas if c not in presentes]
    if faltantes:
        LOG.warning("MEN sin columnas %s — se rellenan con NULL", ", ".join(faltantes))
        tipo = {"desercion_sospechosa": "BOOLEAN", "cod_departamento": "VARCHAR", "etc": "VARCHAR"}
        extra = ", ".join(f"CAST(NULL AS {tipo.get(c, 'DOUBLE')}) AS {c}" for c in faltantes)
        con.execute(f"CREATE OR REPLACE VIEW men AS SELECT *, {extra} FROM men")

    con.execute(SQL_MEN_ULTIMO)
    if hay["saber11"]:
        con.execute(SQL_SABER_MUNICIPIO)
    if hay["cpe"]:
        con.execute(SQL_CPE_ULTIMO)

    joins, campos = [], []
    if hay["saber11"]:
        joins.append("LEFT JOIN saber_municipio s USING (cod_municipio)")
        campos.append("s.periodo_saber, s.sedes_evaluadas, s.evaluados, s.prom_lectura, "
                      "s.prom_matematicas, s.prom_naturales, s.prom_sociales, s.prom_ingles, "
                      "s.pct_internet, s.pct_computador")
    if hay["cpe"]:
        joins.append("LEFT JOIN cpe_ultimo c USING (cod_municipio)")
        campos.append("c.anio_cpe, c.ultimo_anio_con_entregas, c.ninos_por_terminal, "
                      "c.anio_ninos_por_terminal, c.terminales_historicas, "
                      "c.docentes_formados_historico, c.inversion_historica")
    if hay["secop"]:
        joins.append("LEFT JOIN secop k USING (cod_municipio)")
        campos.append("k.n_contratos_educacion, k.valor_total_educacion")
    if hay["geo_mun"]:
        # Dónde queda, a qué distancia de su capital y de Bogotá, y si es
        # municipio propiamente dicho o área no municipalizada.
        joins.append("LEFT JOIN geo_mun g USING (cod_municipio)")
        campos.append("g.lat, g.lon, g.km_a_capital, g.km_a_bogota, "
                      "g.capital_departamento, g.tipo_municipio")
    if hay["poblacion"]:
        # Habitantes del año en curso según la proyección DANE vigente. Es la
        # cifra que le da escala a todo lo demás: 45.000 menores en edad
        # escolar no significan lo mismo en un municipio de 60.000 habitantes
        # que en uno de 800.000.
        con.execute(SQL_POBLACION_ACTUAL.format(anio=date.today().year))
        joins.append("LEFT JOIN poblacion_actual p USING (cod_municipio)")
        campos.append("p.anio_poblacion, p.poblacion_total, p.poblacion_cabecera, "
                      "p.poblacion_rural, p.poblacion_5_16_dane, p.poblacion_5_18_dane, "
                      "p.actualizacion_dane")
    if hay["matricula"]:
        joins.append("LEFT JOIN matricula t USING (cod_municipio)")
        campos.append("t.anio_matricula, t.matricula_total, t.matricula_oficial, "
                      "t.matricula_no_oficial, t.matricula_rural")

    extra = (", " + ", ".join(campos)) if campos else ""
    df = con.execute(f"SELECT m.* {extra} FROM men_ultimo m {' '.join(joins)}").fetchdf()

    # ------------------------------------------------------------------ #
    # Docentes — por Entidad Territorial Certificada, nunca inventados por
    # municipio. El MEN solo publica docentes oficiales por ETC. Si el
    # municipio ES una ETC (Soacha, Tumaco, las capitales), la cifra es suya;
    # si no, es la del departamento entero y se marca como tal para que la
    # ficha lo diga en vez de dejar que parezca un dato local.
    # ------------------------------------------------------------------ #
    if hay["docentes"] and "etc" in df.columns:
        doc = con.execute("SELECT etc_norm, etc AS etc_docentes, docentes_oficiales, "
                          "docentes_rurales, anio_docentes FROM docentes").fetchdf()
        df["etc_norm"] = df["etc"].map(normalizar_etc)
        df = df.merge(doc, on="etc_norm", how="left")
        df["docentes_de_este_municipio"] = df["etc_norm"] == df["municipio"].map(normalizar_etc)
        # Cuántos municipios comparten la ETC: sin esto, «8.045 docentes» en la
        # ficha de un pueblo de Boyacá parece un dato del pueblo.
        df["municipios_en_la_etc"] = df.groupby("etc_norm")["cod_municipio"].transform("count")
        df = df.drop(columns=["etc_norm"])
        LOG.info("Docentes: %s municipios con ETC cruzada, %s son ETC propia",
                 int(df["docentes_oficiales"].notna().sum()),
                 int(df["docentes_de_este_municipio"].sum()))

    # ------------------------------------------------------------------ #
    # Contexto territorial
    # ------------------------------------------------------------------ #
    if hay["geo_cp"]:
        # Poblados con nombre propio APARTE de la cabecera: una primera medida
        # de qué tan disperso es el territorio que habría que cubrir. La cabecera
        # se excluye porque es el municipio mismo; contarla inflaría a todos por
        # igual y no distinguiría un municipio compacto de uno regado.
        cp = con.execute("""
            SELECT cod_municipio, count(*) AS poblados_fuera_de_la_cabecera
            FROM geo_cp WHERE tipo <> 'cabecera municipal'
            GROUP BY cod_municipio
        """).fetchdf()
        df = df.merge(cp, on="cod_municipio", how="left")
        df["poblados_fuera_de_la_cabecera"] = (
            df["poblados_fuera_de_la_cabecera"].fillna(0).astype(int)
        )

    if hay["saber11"]:
        # Sedes rurales sobre el total: es la cifra que cambia de raíz lo que
        # significa "cobertura" en un municipio.
        zonas = con.execute("""
            WITH ultimo AS (SELECT max(periodo) AS p FROM saber11)
            SELECT cod_municipio,
                   count(*) AS sedes_total,
                   count(*) FILTER (WHERE upper(zona) LIKE 'RURAL%') AS sedes_rurales
            FROM saber11, ultimo
            WHERE periodo = ultimo.p AND cod_municipio IS NOT NULL
            GROUP BY cod_municipio
        """).fetchdf()
        df = df.merge(zonas, on="cod_municipio", how="left")
        df["pct_sedes_rurales"] = (
            df["sedes_rurales"] / df["sedes_total"].where(df["sedes_total"] > 0) * 100
        ).round(1)

    # ------------------------------------------------------------------ #
    # Contexto económico — SIEMPRE del departamento, nunca del municipio
    # ------------------------------------------------------------------ #
    if hay["economia"]:
        # El PIB solo se publica por departamento. Atribuirlo a un municipio
        # sería inventar, así que viaja con el nombre del departamento pegado y
        # la ficha lo presenta como contexto regional, no como cifra local.
        eco = con.execute("""
            SELECT cod_departamento, anio_pib, pib_miles_millones,
                   actividades_principales, pct_actividades_principales,
                   sector_dominante
            FROM economia
        """).fetchdf()
        df = df.merge(eco, on="cod_departamento", how="left")

    # ------------------------------------------------------------------ #
    # Posición relativa dentro del departamento
    # ------------------------------------------------------------------ #
    if "prom_matematicas" in df.columns:
        grupo = df.groupby("departamento")["prom_matematicas"]
        df["mediana_depto_matematicas"] = grupo.transform("median").round(1)
        # Percentil dentro del departamento. Con menos de cinco municipios con
        # dato el percentil es aritmética sin significado: se deja nulo.
        con_dato = grupo.transform("count")
        df["percentil_depto"] = (grupo.rank(pct=True) * 100).round(0)
        df.loc[con_dato < 5, "percentil_depto"] = pd.NA

    # ------------------------------------------------------------------ #
    # Contratación por habitante en edad escolar
    # ------------------------------------------------------------------ #
    if "valor_total_educacion" in df.columns and "poblacion_5_16" in df.columns:
        # NO es inversión educativa per cápita. SECOP registra el municipio de la
        # ENTIDAD que contrata, no dónde se ejecutó el gasto: los contratos de
        # entidades nacionales con sede en Bogotá caen todos en Bogotá. Sirve
        # como orden de magnitud para abrir una conversación, nunca como cifra.
        df["contratacion_por_menor"] = (
            df["valor_total_educacion"] / df["poblacion_5_16"].where(df["poblacion_5_16"] > 0)
        ).round(0)
        # Referencia departamental. Sin ella el "contraste" solo miraría un lado.
        df["mediana_depto_contratacion"] = (
            df.groupby("departamento")["contratacion_por_menor"].transform("median").round(0)
        )

    # ------------------------------------------------------------------ #
    # Escala: qué parte del municipio está en edad escolar y cuántos de esos
    # están matriculados. Solo se calcula con las dos cifras del mismo origen.
    # ------------------------------------------------------------------ #
    if "poblacion_total" in df.columns and "poblacion_5_18_dane" in df.columns:
        df["pct_poblacion_5_18"] = (
            df["poblacion_5_18_dane"] / df["poblacion_total"].where(df["poblacion_total"] > 0) * 100
        ).round(1)
    if "matricula_total" in df.columns and "docentes_de_este_municipio" in df.columns:
        # Estudiantes oficiales por docente oficial, y SOLO donde el municipio
        # es su propia ETC: en los demás el numerador es del pueblo y el
        # denominador del departamento, y dividirlos no significa nada.
        propio = df["docentes_de_este_municipio"] & (df["docentes_oficiales"] > 0)
        df["estudiantes_por_docente_oficial"] = pd.NA
        df.loc[propio, "estudiantes_por_docente_oficial"] = (
            df.loc[propio, "matricula_oficial"] / df.loc[propio, "docentes_oficiales"]
        ).round(1)

    # ------------------------------------------------------------------ #
    # Señales — dónde mirar, no qué concluir
    # ------------------------------------------------------------------ #
    senales: list[list[str]] = []
    hoy = date.today().year
    for fila in df.itertuples():
        s: list[str] = []
        # Se juzga por el dato vigente, no por cualquier año del histórico:
        # un municipio que corrigió su reporte no debe cargar la marca para
        # siempre. El histórico queda aparte, en anios_desercion_cero.
        if pd.notna(getattr(fila, "desercion", None)) and fila.desercion == 0 \
                and pd.notna(getattr(fila, "cobertura_neta", None)) \
                and fila.cobertura_neta < UMBRAL_COBERTURA_BAJA:
            s.append("desercion_cero_con_cobertura_baja")
        anio_men = getattr(fila, "anio_men", None)
        if pd.notna(anio_men) and hoy - int(anio_men) >= 3:
            s.append("sin_reporte_men_reciente")
        cob = getattr(fila, "cobertura_neta", None)
        if pd.notna(cob) and cob < UMBRAL_COBERTURA_BAJA:
            s.append("cobertura_neta_baja")
        des = getattr(fila, "desercion", None)
        if pd.notna(des) and des > UMBRAL_DESERCION_ALTA:
            s.append("desercion_alta")
        net = getattr(fila, "pct_internet", None)
        if pd.notna(net) and net < UMBRAL_INTERNET_BAJO:
            s.append("brecha_digital_alta")
        # Contraste: contratación por menor por ENCIMA de la mediana de su
        # departamento y resultados en el cuartil más bajo del mismo
        # departamento. No prueba desvío de nada —SECOP ni siquiera dice dónde se
        # ejecutó el gasto—; marca el municipio como candidato a revisión
        # documental, que es exactamente para lo que existe la señal.
        pct = getattr(fila, "percentil_depto", None)
        cpm = getattr(fila, "contratacion_por_menor", None)
        med = getattr(fila, "mediana_depto_contratacion", None)
        if pd.notna(pct) and pd.notna(cpm) and pd.notna(med) and pct <= 25 and cpm > med:
            s.append("revisar_contraste_recursos_resultados")
        senales.append(s)
    df["senales"] = senales
    df["n_senales"] = [len(s) for s in senales]

    df["corte"] = date.today().isoformat()
    LOG.info("Fichas municipales: %s", f"{len(df):,}".replace(",", "."))
    return df


# --------------------------------------------------------------------------- #
# Ficha por sede
# --------------------------------------------------------------------------- #

SQL_SEDES = """
CREATE OR REPLACE TABLE sede_base AS
WITH ordenado AS (
    SELECT *,
           row_number() OVER (PARTITION BY cod_dane_sede ORDER BY periodo DESC) AS rn,
           count(*)     OVER (PARTITION BY cod_dane_sede)                       AS periodos_con_dato
    FROM saber11
    WHERE cod_dane_sede IS NOT NULL AND evaluados > 0
),
ultimo AS (SELECT * FROM ordenado WHERE rn = 1),
-- Pendiente simple entre el periodo más viejo y el más nuevo de la ventana de
-- tendencia. Una regresión sobre tres puntos no es más honesta que una resta,
-- y la resta se puede explicar en una frase a un rector.
ventana AS (
    SELECT cod_dane_sede,
           arg_max(prom_matematicas, periodo) AS mat_fin,
           arg_min(prom_matematicas, periodo) AS mat_ini,
           max(periodo) AS p_fin, min(periodo) AS p_ini,
           count(*) AS n
    FROM ordenado WHERE rn <= {ventana}
    GROUP BY cod_dane_sede
)
SELECT u.cod_dane_sede, u.nombre_sede, u.cod_municipio, u.municipio, u.departamento,
       u.naturaleza, u.zona, u.periodo AS periodo_saber, u.periodos_con_dato,
       u.evaluados, u.muestra_suficiente,
       u.prom_lectura, u.prom_matematicas, u.prom_naturales, u.prom_sociales, u.prom_ingles,
       u.pct_internet, u.pct_computador,
       v.mat_ini, v.mat_fin, v.p_ini, v.p_fin, v.n AS periodos_tendencia
FROM ultimo u LEFT JOIN ventana v USING (cod_dane_sede)
"""


def construir_sedes(con: duckdb.DuckDBPyConnection, hay: dict[str, bool]) -> pd.DataFrame:
    if not hay["saber11"]:
        LOG.warning("Sin saber11_colegios.parquet no hay fichas por sede.")
        return pd.DataFrame()

    con.execute(SQL_SEDES.format(ventana=PERIODOS_TENDENCIA))
    df = con.execute("SELECT * FROM sede_base").fetchdf()

    if hay["matricula_sede"]:
        # Cuántos estudiantes tiene la sede, todos los grados. Saber 11 solo ve
        # a los de once; la matrícula dice el tamaño real del colegio.
        ms = con.execute("SELECT cod_dane_sede, matricula AS matricula_sede, "
                         "anio_matricula FROM matricula_sede").fetchdf()
        df = df.merge(ms, on="cod_dane_sede", how="left")
        LOG.info("Matrícula por sede: %s de %s sedes con dato",
                 int(df["matricula_sede"].notna().sum()), len(df))

    # Comparación contra el municipio y contra el departamento. Un colegio se
    # juzga frente a sus pares, no frente a una media nacional que mezcla
    # contextos incomparables.
    for ambito, clave in (("municipio", "cod_municipio"), ("depto", "departamento")):
        g = df[df["muestra_suficiente"] == True].groupby(clave)["prom_matematicas"]  # noqa: E712
        df[f"mediana_{ambito}_matematicas"] = df[clave].map(g.median()).round(1)

    df["dif_vs_municipio"] = (df["prom_matematicas"] - df["mediana_municipio_matematicas"]).round(1)
    df["dif_vs_depto"] = (df["prom_matematicas"] - df["mediana_depto_matematicas"]).round(1)
    df["cambio_matematicas"] = (df["mat_fin"] - df["mat_ini"]).round(1)

    senales: list[list[str]] = []
    for fila in df.itertuples():
        s: list[str] = []
        if not getattr(fila, "muestra_suficiente", True):
            s.append("muestra_insuficiente")
        camb = getattr(fila, "cambio_matematicas", None)
        per = getattr(fila, "periodos_tendencia", 0) or 0
        if pd.notna(camb) and per >= PERIODOS_TENDENCIA and camb <= -UMBRAL_CAIDA_PUNTOS:
            s.append("caida_sostenida")
        dif = getattr(fila, "dif_vs_depto", None)
        if pd.notna(dif) and dif <= -UMBRAL_CAIDA_PUNTOS:
            s.append("por_debajo_de_su_departamento")
        net = getattr(fila, "pct_internet", None)
        if pd.notna(net) and net < UMBRAL_INTERNET_BAJO:
            s.append("brecha_digital_alta")
        senales.append(s)
    df["senales"] = senales
    df["n_senales"] = [len(s) for s in senales]

    df["corte"] = date.today().isoformat()
    LOG.info("Fichas por sede: %s", f"{len(df):,}".replace(",", "."))
    return df



# --------------------------------------------------------------------------- #
# Gacetero: de un nombre a un lugar en el mapa
# --------------------------------------------------------------------------- #

def normalizar(serie: pd.Series) -> pd.Series:
    """Sin tildes y en mayúsculas, para que «Monteria» encuentre «MONTERÍA»."""
    return (
        serie.astype("string")
        .str.normalize("NFKD")
        .str.encode("ascii", "ignore")
        .str.decode("ascii")
        .str.upper()
        .str.strip()
    )


def construir_lugares(con: duckdb.DuckDBPyConnection, hay: dict[str, bool]) -> pd.DataFrame:
    """
    Un índice de todo lugar con nombre propio del país: los 1.122 municipios más
    los 8.161 centros poblados, cada uno con sus coordenadas.

    Existe para responder «¿dónde queda Tibacuy?» a quien no conoce Colombia.
    Y existe con una advertencia calculada encima: los nombres se repiten
    muchísimo —«Pueblo Nuevo» aparece en 41 municipios de 19 departamentos—, así
    que cada fila lleva cuántos homónimos tiene. Con más de uno, el agente está
    obligado a preguntar en vez de escoger el primero: mandar a alguien a
    diagnosticar el municipio equivocado es peor que no responder.
    """
    if not hay["geo_mun"]:
        LOG.warning("Sin territorio_municipios.parquet no se puede armar el gacetero.")
        return pd.DataFrame()

    municipios = con.execute("""
        SELECT cod_municipio AS cod_lugar, municipio AS lugar, 'municipio' AS tipo,
               cod_municipio, municipio, cod_departamento, departamento, lat, lon
        FROM geo_mun
    """).fetchdf()

    partes = [municipios]
    if hay["geo_cp"]:
        centros = con.execute("""
            SELECT cod_lugar, lugar, tipo, cod_municipio, municipio,
                   cod_departamento, departamento, lat, lon
            FROM geo_cp
            -- La cabecera municipal duplica al municipio: el mismo punto con el
            -- mismo nombre aparecería dos veces en cualquier búsqueda.
            WHERE tipo <> 'cabecera municipal'
        """).fetchdf()
        partes.append(centros)

    df = pd.concat(partes, ignore_index=True)
    df["busqueda"] = normalizar(df["lugar"])

    # Homónimos: cuántos lugares comparten exactamente este nombre en el país.
    df["homonimos"] = df.groupby("busqueda")["busqueda"].transform("count")

    sin_coordenada = int(df["lat"].isna().sum())
    if sin_coordenada:
        LOG.warning("%s lugares sin coordenada: no se podrán ubicar en el mapa", sin_coordenada)

    ambiguos = int((df["homonimos"] > 1).sum())
    LOG.info("Gacetero: %s lugares (%s con nombre repetido en el país)",
             f"{len(df):,}".replace(",", "."), f"{ambiguos:,}".replace(",", "."))
    return df


# --------------------------------------------------------------------------- #

def escribir_metadatos(salida: Path, hay: dict[str, bool], n_mun: int, n_sede: int,
                       n_lugar: int = 0) -> None:
    """
    Los umbrales quedan escritos junto a los datos. Quien lea una ficha dentro de
    un año tiene que poder saber con qué regla se marcó una señal, sin abrir el
    código.
    """
    meta = {
        "generado": date.today().isoformat(),
        "fichas_municipio": n_mun,
        "fichas_sede": n_sede,
        "lugares_en_el_gacetero": n_lugar,
        "fuentes_presentes": {k: v for k, v in hay.items()},
        "umbrales": {
            "muestra_minima": MUESTRA_MINIMA,
            "periodos_tendencia": PERIODOS_TENDENCIA,
            "cobertura_neta_baja_pct": UMBRAL_COBERTURA_BAJA,
            "desercion_alta_pct": UMBRAL_DESERCION_ALTA,
            "internet_bajo_pct": UMBRAL_INTERNET_BAJO,
            "caida_sostenida_puntos": UMBRAL_CAIDA_PUNTOS,
        },
        "advertencias": [
            "Los umbrales son convenciones de trabajo de Fundación Startin, no normas oficiales.",
            "Las señales indican dónde revisar; no constituyen hallazgo ni acusación.",
            "SECOP registra el municipio de la entidad contratante, no el lugar de ejecución del gasto.",
            "Las comparaciones son dentro del departamento, nunca contra el promedio nacional.",
            "El PIB solo existe por departamento: es contexto regional, no cifra del municipio.",
            "Las distancias son en línea recta, no por carretera.",
            "La población son proyecciones DANE (CNPV 2018) del año en curso; el DANE las revisa.",
            "La matrícula es la reportada al SIMAT; el sector privado reporta menos y peor que el oficial.",
            "Los docentes son solo del sector oficial y solo por Entidad Territorial Certificada: para un municipio no certificado la cifra es la del departamento.",
            "No hay capa nacional de veredas ni polígonos municipales: el mapa es de puntos.",
        ],
    }
    (salida / "fichas_metadatos.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Construye las fichas precalculadas de Brújula")
    parser.add_argument("--datos", type=Path, default=Path("./data"))
    parser.add_argument("--salida", type=Path, default=Path("./data"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )
    args.salida.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    LOG.info("Fuentes en %s:", args.datos)
    hay = registrar(con, args.datos)

    municipios = construir_municipios(con, hay)
    municipios.to_parquet(args.salida / "fichas_municipio.parquet", index=False)

    sedes = construir_sedes(con, hay)
    if not sedes.empty:
        sedes.to_parquet(args.salida / "fichas_sede.parquet", index=False)

    lugares = construir_lugares(con, hay)
    if not lugares.empty:
        lugares.to_parquet(args.salida / "fichas_lugar.parquet", index=False)

    escribir_metadatos(args.salida, hay, len(municipios), len(sedes), len(lugares))

    print("\n" + "=" * 62)
    print(f"Fichas municipales : {len(municipios):,}".replace(",", "."))
    print(f"Fichas por sede    : {len(sedes):,}".replace(",", "."))
    if "n_senales" in municipios:
        con_señal = int((municipios["n_senales"] > 0).sum())
        print(f"Municipios con señal: {con_señal:,}".replace(",", "."))
    if not sedes.empty:
        print(f"Sedes con señal    : {int((sedes['n_senales'] > 0).sum()):,}".replace(",", "."))
    if not lugares.empty:
        print(f"Lugares ubicables  : {len(lugares):,}".replace(",", "."))
        print(f"  con nombre repetido: {int((lugares['homonimos'] > 1).sum()):,}".replace(",", "."))
    print("=" * 62)
    print("Las señales marcan dónde revisar. No son hallazgos.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
