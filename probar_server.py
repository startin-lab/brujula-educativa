#!/usr/bin/env python3
"""
Brújula Educativa — Pruebas del servidor MCP
Fundación Startin

Genera un territorio sintético (2 departamentos, 40 municipios, 240 sedes, con
huecos de reporte y sedes demasiado pequeñas a propósito), construye las fichas
con `construir_fichas.py` y ejerce las herramientas del servidor.

NO TOCA LA RED, y es deliberado. Las fuentes públicas colombianas se caen,
tienen latencias de minutos y devuelven 503 sin avisar. Si estas pruebas
dependieran de ellas, una caída del ICFES un martes cualquiera parecería un
error nuestro y nadie podría distinguir un fallo del código de un fallo del
servidor ajeno.

Lo que se verifica no es que el código corra, sino que cumpla lo que promete:
que no se pueda consultar sin elegir territorio, que una sede con menos de diez
evaluados no entregue promedio, que un año sin reporte salga nulo y no cero, que
las comparaciones sean departamentales y no nacionales, que las advertencias
viajen dentro del bloque de visualización, y que la consulta nacional esté
cerrada sin token.

Uso:
    pip install duckdb pandas pyarrow "mcp[cli]"
    python probar_server.py

Devuelve 0 si todo pasa, 1 si algo falla. Sirve tal cual en CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent
PERIODOS = ["2018-2", "2019-2", "2020-2", "2021-2", "2022-2"]


# --------------------------------------------------------------------------- #
# Territorio sintético
# --------------------------------------------------------------------------- #

def generar(destino: Path) -> None:
    """
    Fabrica los parquet crudos con los defectos que trae la realidad: municipios
    que dejan de reportar deserción, municipios que reportan 0 % teniendo
    cobertura baja, sedes con tres evaluados, y un SECOP que solo cubre 30 de los
    40 municipios porque los nombres no siempre cruzan.
    """
    destino.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)

    municipios = (
        [(f"05{i:03d}", f"Muni{i}", "ANTIOQUIA", "05") for i in range(1, 21)]
        + [(f"25{i:03d}", f"Villa{i}", "CUNDINAMARCA", "25") for i in range(1, 21)]
    )

    # --- MEN ---------------------------------------------------------------- #
    filas = []
    for cod, nom, dep, cd in municipios:
        for anio in range(2011, 2025):
            cobertura = float(rng.uniform(60, 98))
            # Un municipio que reporta 0 % de deserción con cobertura baja: el
            # caso que la señal `desercion_cero_con_cobertura_baja` debe atrapar.
            desercion = 0.0 if (cod.endswith("003") and anio > 2020) else float(rng.uniform(0.5, 9))
            # La mitad deja de reportar deserción después de 2022, conservando el
            # resto de indicadores. Es el hueco que no puede convertirse en cero.
            if anio > 2022 and int(cod[-1]) % 2 == 0:
                desercion = np.nan
            filas.append(dict(
                anio=anio, cod_municipio=cod, municipio=nom, departamento=dep,
                cod_departamento=cd, cobertura_neta=cobertura, cobertura_bruta=cobertura + 8,
                desercion=desercion, aprobacion=float(rng.uniform(80, 97)),
                reprobacion=float(rng.uniform(2, 12)), repitencia=float(rng.uniform(1, 6)),
                tasa_matriculacion=float(rng.uniform(70, 99)),
                poblacion_5_16=int(rng.integers(500, 90000)),
            ))
    men = pd.DataFrame(filas)
    men["anio"] = men["anio"].astype("Int64")
    men["cod_municipio"] = men["cod_municipio"].astype("string")
    men["desercion_sospechosa"] = (men["desercion"] == 0) & (men["cobertura_neta"] < 80)
    men.to_parquet(destino / "men_municipios.parquet", index=False)

    # --- Saber 11 por sede -------------------------------------------------- #
    registros = []
    for cod, nom, dep, cd in municipios:
        for j in range(6):
            dane = f"{cod}0000{j}"
            base, deriva = rng.uniform(40, 62), rng.uniform(-4, 2)
            for k, periodo in enumerate(PERIODOS):
                # La sede 5 de cada municipio puede quedar por debajo del mínimo.
                evaluados = int(rng.integers(3, 400)) if j == 5 else int(rng.integers(12, 400))
                punt = base + deriva * k + rng.normal(0, 1.2)
                registros.append(dict(
                    periodo=periodo, anio=int(periodo[:4]), cod_dane_sede=dane,
                    nombre_sede=f"IE {nom} {j}", cod_municipio=cod, municipio=nom,
                    departamento=dep, naturaleza="OFICIAL" if j < 4 else "NO OFICIAL",
                    zona="URBANO" if j < 5 else "RURAL", evaluados=evaluados,
                    prom_lectura=punt + 2, prom_matematicas=punt, prom_naturales=punt + 1,
                    prom_sociales=punt - 1, prom_ingles=punt - 3,
                    con_internet=int(evaluados * rng.uniform(0.2, 0.95)),
                    con_computador=int(evaluados * rng.uniform(0.3, 0.98)),
                ))
    saber = pd.DataFrame(registros)
    saber["pct_internet"] = (saber.con_internet / saber.evaluados * 100).round(1)
    saber["pct_computador"] = (saber.con_computador / saber.evaluados * 100).round(1)
    saber["muestra_suficiente"] = saber.evaluados >= 10
    for col in ("cod_dane_sede", "cod_municipio"):
        saber[col] = saber[col].astype("string")
    saber.to_parquet(destino / "saber11_colegios.parquet", index=False)

    # --- Computadores Para Educar ------------------------------------------- #
    cpe = pd.DataFrame([
        dict(anio=anio, cod_municipio=cod, municipio=nom, departamento=dep,
             ninos_por_terminal=float(rng.uniform(1, 14)),
             terminales=int(rng.integers(0, 900)), docentes_formados=int(rng.integers(0, 200)))
        for cod, nom, dep, cd in municipios for anio in (2019, 2021, 2023)
    ])
    cpe["anio"] = cpe["anio"].astype("Int64")
    cpe["cod_municipio"] = cpe["cod_municipio"].astype("string")
    cpe.to_parquet(destino / "computadores_educar.parquet", index=False)

    # --- SECOP: solo 30 de 40, como pasa de verdad -------------------------- #
    secop = pd.DataFrame([
        dict(cod_municipio=cod, municipio=nom, departamento=dep,
             n_contratos_educacion=int(rng.integers(5, 9000)),
             valor_total_educacion=float(rng.uniform(1e8, 2e12)))
        for cod, nom, dep, cd in municipios[:30]
    ])
    secop["cod_municipio"] = secop["cod_municipio"].astype("string")
    secop.to_parquet(destino / "secop_municipios.parquet", index=False)

    # --- Contexto territorial ------------------------------------------ #
    # Coordenadas plausibles: Antioquia alrededor de (6,5 / -75,6) y
    # Cundinamarca alrededor de (4,6 / -74,1).
    geo = []
    for cod, nom, dep, cd in municipios:
        base_lat, base_lon = (6.5, -75.6) if cd == "05" else (4.6, -74.1)
        geo.append(dict(
            cod_departamento=cd, departamento=dep, cod_municipio=cod, municipio=nom,
            tipo_municipio="Municipio",
            lat=base_lat + float(rng.uniform(-1.2, 1.2)),
            lon=base_lon + float(rng.uniform(-1.2, 1.2)),
        ))
    geo = pd.DataFrame(geo)
    capital = {"05": "Muni1", "25": "Villa1"}
    geo["capital_departamento"] = geo["cod_departamento"].map(capital)
    ref = {cd: geo[geo.municipio == nom].iloc[0] for cd, nom in capital.items()}
    geo["km_a_capital"] = [
        round(float(np.hypot(r.lat - ref[r.cod_departamento].lat,
                             r.lon - ref[r.cod_departamento].lon) * 111), 1)
        for r in geo.itertuples()
    ]
    geo["km_a_bogota"] = [round(float(np.hypot(r.lat - 4.61, r.lon + 74.08) * 111), 1)
                          for r in geo.itertuples()]
    for col in ("cod_departamento", "cod_municipio"):
        geo[col] = geo[col].astype("string")
    geo.to_parquet(destino / "territorio_municipios.parquet", index=False)

    # Centros poblados, con nombres repetidos a propósito: "PUEBLO NUEVO"
    # existe en 41 municipios reales y el gacetero tiene que detectarlo.
    repetidos = ["PUEBLO NUEVO", "SAN ANTONIO", "SANTA ROSA"]
    cps = []
    for i, (cod, nom, dep, cd) in enumerate(municipios):
        fila = geo[geo.cod_municipio == cod].iloc[0]
        cps.append(dict(cod_departamento=cd, departamento=dep, cod_municipio=cod,
                        municipio=nom, cod_lugar=f"{cod}000", lugar=nom,
                        tipo="cabecera municipal", lat=fila.lat, lon=fila.lon))
        for k in range(2):
            nombre = repetidos[k % len(repetidos)] if i % 3 == 0 else f"{nom} Vereda {k}"
            cps.append(dict(cod_departamento=cd, departamento=dep, cod_municipio=cod,
                            municipio=nom, cod_lugar=f"{cod}00{k+1}", lugar=nombre,
                            tipo="centro poblado",
                            lat=fila.lat + float(rng.uniform(-0.2, 0.2)),
                            lon=fila.lon + float(rng.uniform(-0.2, 0.2))))
    cps = pd.DataFrame(cps)
    for col in ("cod_departamento", "cod_municipio", "cod_lugar"):
        cps[col] = cps[col].astype("string")
    cps.to_parquet(destino / "territorio_centros_poblados.parquet", index=False)

    # Economía: solo departamental, como en la realidad.
    eco = pd.DataFrame([
        dict(cod_departamento="05", departamento="Antioquia", anio_pib=2023,
             pib_miles_millones=190000.0,
             actividades_principales=["Industrias manufactureras",
                                      "Comercio y transporte", "Construcción"],
             pct_actividades_principales=[22.4, 18.1, 9.7], sector_dominante="Secundario"),
        dict(cod_departamento="25", departamento="Cundinamarca", anio_pib=2023,
             pib_miles_millones=98000.0,
             actividades_principales=["Industrias manufactureras",
                                      "Agricultura, ganadería, caza, silvicultura y pesca",
                                      "Comercio y transporte"],
             pct_actividades_principales=[19.2, 16.7, 16.5], sector_dominante="Secundario"),
    ])
    eco["cod_departamento"] = eco["cod_departamento"].astype("string")
    eco.to_parquet(destino / "economia_resumen.parquet", index=False)

    detalle = pd.DataFrame([
        dict(objeto_a_contratar=f"PRESTACION DE SERVICIOS EDUCATIVOS {k}",
             valor_contrato=float(rng.integers(int(1e7), int(9e11))),
             nom_raz_social_contratista=f"CONTRATISTA {k}",
             nombre_de_la_entidad=f"ALCALDIA DE {nom}",
             fecha_de_firma_del_contrato=f"2023-04-1{k % 9}",
             url_contrato=f"https://www.secop.gov.co/contrato/{cod}-{k}",
             modalidad_de_contrataci_n="Licitación pública",
             estado_del_proceso="Liquidado", municipio=nom, cod_municipio=cod)
        for cod, nom, dep, cd in municipios[:30] for k in range(10)
    ])
    detalle["cod_municipio"] = detalle["cod_municipio"].astype("string")
    detalle.to_parquet(destino / "secop_contratos_mayores.parquet", index=False)


# --------------------------------------------------------------------------- #
# Andamiaje
# --------------------------------------------------------------------------- #

FALLOS: list[str] = []


def check(nombre: str, condicion: bool, detalle: object = "") -> None:
    print(f"  {'OK  ' if condicion else 'FALLA'}  {nombre}" + ("" if condicion else f"  ::  {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def llamar(herramienta, **kw):
    """FastMCP envuelve la función; se busca la original para probarla directa."""
    return getattr(herramienta, "fn", herramienta)(**kw)


# --------------------------------------------------------------------------- #

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        datos = Path(tmp) / "data"
        print(f"Generando territorio sintético en {datos}")
        generar(datos)

        print("Construyendo fichas...")
        r = subprocess.run(
            [sys.executable, str(RAIZ / "construir_fichas.py"), "--datos", str(datos), "--salida", str(datos)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stdout, r.stderr)
            return 1

        os.environ["BRUJULA_TOKEN_NACIONAL"] = "token-de-prueba"
        sys.path.insert(0, str(RAIZ))
        import server as S  # noqa: PLC0415

        S._datos = datos
        S._token_nacional = "token-de-prueba"
        S.conexion()

        print("\n== 1. El ámbito territorial es obligatorio ==")
        for nombre, fn, kw in [
            ("ficha_municipio", S.ficha_municipio, {"municipio": ""}),
            ("colegios_del_municipio", S.colegios_del_municipio, {"municipio": ""}),
            ("buscar_colegio", S.buscar_colegio, {"nombre": "Instituto", "departamento": ""}),
            ("evolucion_municipio", S.evolucion_municipio, {"municipio": ""}),
            ("senales_departamento", S.senales_departamento, {"departamento": ""}),
            ("contratos_municipio", S.contratos_municipio, {"municipio": ""}),
        ]:
            res = llamar(fn, **kw)
            check(f"{nombre} exige territorio", res.get("requiere_ambito") is True, json.dumps(res)[:120])

        print("\n== 2. Selector territorial ==")
        d = llamar(S.listar_departamentos)
        check("listar_departamentos", d["encontrado"] and len(d["departamentos"]) == 2, d)
        mu = llamar(S.listar_municipios, departamento="antioquia")   # sin tilde, minúscula
        check("listar_municipios normaliza tildes y mayúsculas",
              mu["encontrado"] and len(mu["municipios"]) == 20)
        check("departamento inexistente no inventa",
              llamar(S.listar_municipios, departamento="Atlantico")["encontrado"] is False)

        print("\n== 3. Ficha municipal ==")
        f = llamar(S.ficha_municipio, municipio="Muni4", departamento="ANTIOQUIA")
        check("devuelve indicadores", f["encontrado"] and f["indicadores"]["cobertura_neta"] is not None)
        check("trae fecha de corte", bool(f["corte"]))
        check("trae fuente", bool(f["fuente"]))
        check("cuatro bloques de visualización", len(f["vis"]) == 4, [v["tipo"] for v in f["vis"]])
        check("uno de ellos es el mapa", any(v["tipo"] == "mapa" for v in f["vis"]))
        check("advertencias dentro de cada vis", all("advertencias" in v for v in f["vis"]))
        check("aviso de SECOP presente", any("SECOP" in a for a in f["advertencias"]))
        check("compara contra el DEPARTAMENTO, no contra el país",
              "Mediana ANTIOQUIA" in [s["nombre"] for s in f["vis"][1]["series"]])
        check("las señales vienen explicadas en español",
              all(s["explicacion"] != s["clave"] for s in f["senales"]))
        check("municipio inexistente no inventa",
              llamar(S.ficha_municipio, municipio="Macondo", departamento="ANTIOQUIA")["encontrado"] is False)

        print("\n== 4. Muestra pequeña: no se publica promedio ==")
        sede_peq = pd.read_parquet(datos / "fichas_sede.parquet").query("~muestra_suficiente")
        check("el territorio sintético tiene sedes pequeñas", not sede_peq.empty)
        muni_peq = sede_peq.iloc[0]["municipio"]
        c = llamar(S.colegios_del_municipio, municipio=muni_peq, departamento="ANTIOQUIA")
        peq = [s for s in c["sedes"] if s["nota"]]
        check("sedes pequeñas listadas pero sin promedio",
              bool(peq) and all(s["prom_matematicas"] is None for s in peq), peq[:1])
        fp = llamar(S.ficha_colegio, cod_dane_sede=peq[0]["cod_dane_sede"])
        check("ficha_colegio bloquea el promedio con n<10", fp["publicable"] is False)
        check("y explica por qué", "menos de" in fp["mensaje"].lower())

        print("\n== 5. Ficha de sede publicable ==")
        grande = next(s for s in c["sedes"] if s["prom_matematicas"] is not None)
        fs = llamar(S.ficha_colegio, cod_dane_sede=grande["cod_dane_sede"])
        check("publicable", fs["publicable"] is True)
        check("compara contra su municipio y su departamento",
              fs["comparacion"]["diferencia_vs_municipio"] is not None
              and fs["comparacion"]["diferencia_vs_departamento"] is not None)
        check("trae tendencia de tres periodos", fs["tendencia"]["periodos"] == 3)
        check("dos bloques de visualización", len(fs["vis"]) == 2)

        print("\n== 6. Búsqueda acotada al departamento ==")
        check("encuentra dentro del departamento",
              llamar(S.buscar_colegio, nombre="IE Muni4", departamento="ANTIOQUIA")["encontrado"])
        check("no cruza fronteras departamentales",
              llamar(S.buscar_colegio, nombre="IE Muni4", departamento="CUNDINAMARCA")["encontrado"] is False)

        print("\n== 7. Serie histórica: los huecos no son ceros ==")
        e = llamar(S.evolucion_municipio, municipio="Muni4", departamento="ANTIOQUIA", indicador="desercion")
        check("hay años sin reporte", len(e["anios_sin_reporte"]) == 2, e["anios_sin_reporte"])
        check("esos años salen nulos, no en cero",
              all(e["valores"][e["anios"].index(a)] is None for a in e["anios_sin_reporte"]))
        check("y la respuesta lo advierte", any("no es un cero" in a for a in e["advertencias"]))
        check("indicador inválido rechazado",
              llamar(S.evolucion_municipio, municipio="Muni4", departamento="ANTIOQUIA",
                     indicador="lo_que_sea")["encontrado"] is False)

        print("\n== 8. Focalización por señales ==")
        sd = llamar(S.senales_departamento, departamento="ANTIOQUIA")
        check("lista municipios con señales", sd["encontrado"] and len(sd["municipios"]) > 0)
        check("ordenados de más a menos señales",
              all(len(a["senales"]) >= len(b["senales"]) for a, b in zip(sd["municipios"], sd["municipios"][1:])))
        check("advierte que no son hallazgos", any("no son hallazgos" in a for a in sd["advertencias"]))
        sf = llamar(S.senales_departamento, departamento="ANTIOQUIA",
                    senal="desercion_cero_con_cobertura_baja")
        check("filtra por una señal concreta",
              all(any(s["clave"] == "desercion_cero_con_cobertura_baja" for s in m["senales"])
                  for m in sf["municipios"]))

        print("\n== 9. Contratación ==")
        k = llamar(S.contratos_municipio, municipio="Muni4", departamento="ANTIOQUIA")
        check("cada contrato trae su expediente", k["encontrado"] and all(c_["expediente"] for c_ in k["mayores"]))
        check("ordenados por valor",
              all(a["valor_cop"] >= b["valor_cop"] for a, b in zip(k["mayores"], k["mayores"][1:])))
        check("el aviso de SECOP viaja pegado", any("no dónde se ejecutó" in a for a in k["advertencias"]))

        print("\n== 10. Consulta nacional: cerrada sin token ==")
        check("sin token, no responde",
              llamar(S.ranking_nacional, indicador="desercion", token="").get("requiere_acceso_completo") is True)
        check("token equivocado tampoco",
              llamar(S.ranking_nacional, indicador="desercion", token="adivinado").get("requiere_acceso_completo") is True)
        check("con el token correcto sí",
              llamar(S.ranking_nacional, indicador="desercion", token="token-de-prueba")["encontrado"])
        S._token_nacional = ""
        check("sin token configurado queda cerrada para todos",
              llamar(S.ranking_nacional, indicador="desercion", token="").get("requiere_acceso_completo") is True)
        S._token_nacional = "token-de-prueba"

        print("\n== 11. Transparencia ==")
        ed = llamar(S.estado_de_los_datos)
        check("reporta vistas cargadas y limitaciones",
              ed["encontrado"] and len(ed["limitaciones_conocidas"]) >= 5)
        po = llamar(S.comparar_ocde, dominio="lectura")
        check("PISA ausente: lo dice, no inventa cifras",
              po["encontrado"] is False and "no está cargada" in po["mensaje"])

        print("\n== 12. Contexto territorial: dónde queda y qué es ==")
        check("la ficha dice dónde queda",
              f["ubicacion"]["lat"] is not None and f["ubicacion"]["lon"] is not None)
        check("y a qué distancia de su capital y de Bogotá",
              f["ubicacion"]["km_a_la_capital_departamental"] is not None
              and f["ubicacion"]["km_a_bogota"] is not None)
        check("advierte que la distancia es en línea recta",
              "línea recta" in f["ubicacion"]["advertencia"])
        check("dice qué tan rural es", f["ubicacion"]["pct_sedes_rurales"] is not None)
        check("cuenta los poblados fuera de la cabecera",
              f["ubicacion"]["poblados_fuera_de_la_cabecera"] == 2,
              f["ubicacion"]["poblados_fuera_de_la_cabecera"])

        print("\n== 13. Economía: del departamento, nunca del municipio ==")
        eco = f["economia_del_departamento"]
        check("trae actividades principales", eco and len(eco["actividades_principales"]) == 3)
        check("cada una con su peso en el PIB",
              all(a["pct_del_pib"] is not None for a in eco["actividades_principales"]))
        check("el ámbito dice explícitamente 'Departamento de'", eco["ambito"].startswith("Departamento de"))
        check("y la advertencia viaja en la respuesta",
              any("solo se publica por departamento" in a for a in f["advertencias"]))

        print("\n== 14. Ubicar un lugar ==")
        u = llamar(S.ubicar_lugar, nombre="Muni4")
        check("encuentra el municipio", u["encontrado"] and u["lugares"][0]["tipo"] == "municipio")
        check("devuelve bloque de mapa", u["vis"]["tipo"] == "mapa" and len(u["vis"]["puntos"]) > 0)
        check("advierte que el mapa ubica pero no mide",
              any("no mide" in a for a in u["advertencias"]))

        rep = llamar(S.ubicar_lugar, nombre="Pueblo Nuevo")
        check("detecta el nombre repetido", rep["varios_resultados"] is True)
        check("y obliga a preguntar en vez de escoger",
              rep["nota"] is not None and "preguntar" in rep["nota"])
        check("los lista todos con su municipio",
              len(rep["lugares"]) > 1 and all(l["municipio"] for l in rep["lugares"]))
        check("el mapa se abre lejos para que se vean todos", rep["vis"]["zoom"] <= 6)

        acot = llamar(S.ubicar_lugar, nombre="Pueblo Nuevo", departamento="CUNDINAMARCA")
        check("acotar por departamento reduce el ruido",
              len(acot["lugares"]) < len(rep["lugares"])
              and all(l["departamento"] == "CUNDINAMARCA" for l in acot["lugares"]))

        ver = llamar(S.ubicar_lugar, nombre="La Esperanza de Nadie")
        check("lugar inexistente no inventa", ver["encontrado"] is False)
        check("y explica que las veredas no tienen capa nacional",
              "vereda" in ver["sugerencia"])

        check("sin tildes encuentra igual",
              llamar(S.ubicar_lugar, nombre="muni4")["encontrado"])

        print("\n== 15. Todo lo que sale es JSON serializable ==")
        for nombre, obj in [("ficha_municipio", f), ("ficha_colegio", fs),
                            ("senales_departamento", sd), ("contratos_municipio", k),
                            ("ubicar_lugar", rep)]:
            try:
                json.dumps(obj, ensure_ascii=False, default=str)
                check(nombre, True)
            except Exception as exc:  # noqa: BLE001
                check(nombre, False, str(exc))

    print("\n" + "=" * 64)
    if FALLOS:
        print(f"FALLARON {len(FALLOS)}:")
        for f_ in FALLOS:
            print("  ·", f_)
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
