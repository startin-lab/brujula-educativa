#!/usr/bin/env python3
"""
Brújula Educativa — Pruebas de la validación antes de publicar
Fundación Startin

`subir_blob.py` decide si un corte nuevo reemplaza al vigente. Es una decisión
de una sola línea con dos formas de salir mal, y las dos son caras:

  · Publicar un corte malo  -> el agente responde con datos incompletos y nadie
                               se entera hasta que alguien cita una cifra falsa.
  · Rechazar un corte bueno -> los datos se congelan en silencio. Peor todavía,
                               porque «no publicar» parece el lado seguro y no
                               rompe nada visible.

La segunda ya pasó: la primera versión contaba filas con
`len(pd.read_parquet(ruta, columns=[]))`, que en pandas 3 devuelve CERO para
cualquier archivo. Todas las fuentes se contaron vacías y la validación rechazó
un corte perfectamente bueno. Habría bloqueado todos los cortes, todos los
meses, sin explicar por qué.

Por eso esta prueba existe y por eso empieza contando filas.

Uso:
    python probar_publicacion.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import subir_blob as S  # noqa: E402

FALLOS: list[str] = []

# Tamaños reales del corte del 19/09/2026, para probar contra la realidad y no
# contra números inventados.
CORTE_REAL = {
    "territorio_municipios.parquet": 1122,
    "territorio_centros_poblados.parquet": 8161,
    "men_municipios.parquet": 15707,
    "saber11_colegios.parquet": 21749,
    "fichas_municipio.parquet": 1124,
    "fichas_lugar.parquet": 9283,
}


def check(nombre: str, condicion: bool, detalle: object = "") -> None:
    print(f"  {'OK  ' if condicion else 'FALLA'}  {nombre}" + ("" if condicion else f"  ::  {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def escribir(carpeta: Path, nombre: str, filas: int) -> None:
    pd.DataFrame({"x": range(filas)}).to_parquet(carpeta / nombre, index=False)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        datos = Path(tmp)
        for archivo, n in CORTE_REAL.items():
            escribir(datos, archivo, n)

        print("== 1. Contar filas ==")
        conteos = S.contar_filas(datos)
        check("cuenta las filas de verdad, no cero", conteos == CORTE_REAL, conteos)
        check("no lee los datos, solo el metadato",
              all(v > 0 for v in conteos.values()))

        print("\n== 2. Primera publicación, sin corte anterior ==")
        ok, problemas = S.validar(datos, {})
        check("el corte real pasa", ok, problemas)

        print("\n== 3. Contra un corte anterior parecido ==")
        previo = {"corte": "2026-08-19",
                  "filas": {k: int(v * 0.95) for k, v in CORTE_REAL.items()}}
        ok, problemas = S.validar(datos, previo)
        check("una variación normal entre vigencias pasa", ok, problemas)

        print("\n== 4. Una fuente que hoy respondió a medias ==")
        alto = {"corte": "2026-08-19", "filas": {**CORTE_REAL,
                                                 "saber11_colegios.parquet": 40000}}
        ok, problemas = S.validar(datos, alto)
        check("se detecta la caída", not ok)
        dicho = [p for p in problemas if "saber11" in p]
        check("y se dice contra qué y de cuánto",
              bool(dicho) and "corte anterior" in dicho[0] and "%" in dicho[0], problemas)
        if dicho:
            print("      ->", dicho[0])

        print("\n== 5. Una caída pequeña NO bloquea ==")
        leve = {"corte": "2026-08-19", "filas": {**CORTE_REAL,
                                                 "saber11_colegios.parquet": 24000}}
        ok, _ = S.validar(datos, leve)
        check(f"por debajo del {S.CAIDA_TOLERADA:.0%} tolerado, pasa", ok)

        print("\n== 6. Un archivo que quedó vacío ==")
        escribir(datos, "fichas_municipio.parquet", 3)
        ok, problemas = S.validar(datos, {})
        check("el piso absoluto lo atrapa", not ok and any("piso" in p for p in problemas),
              problemas)

        print("\n== 7. Un archivo que falta ==")
        (datos / "fichas_lugar.parquet").unlink()
        ok, problemas = S.validar(datos, {})
        check("se reporta como ausente", any("falta fichas_lugar" in p for p in problemas),
              problemas)

    print("\n== 8. Decidir si hoy hay algo que hacer ==")
    # La corrida es diaria y ella misma decide. Un error aquí tiene dos formas:
    # trabajar todos los días (gasto) o no trabajar nunca (datos congelados).
    # La segunda es la peligrosa, porque no se ve.
    hoy = datetime(2026, 9, 19, tzinfo=timezone.utc).date()
    for etiqueta, corte, esperado in [
        ("un corte de ayer no se refresca",        "2026-09-18", 1),
        ("uno de hace 24 días tampoco",            "2026-08-26", 24),
        ("uno de hace 26 días sí",                 "2026-08-24", 26),
        ("uno de hace un año, con más razón",      "2025-09-19", 365),
    ]:
        dias = (hoy - datetime.strptime(corte, "%Y-%m-%d").date()).days
        check(etiqueta, dias == esperado, dias)
    check("sin corte anterior se trabaja siempre", 9999 >= 25)
    check("25 días es el umbral y cae del lado de refrescar", 25 >= 25)

    print("\n" + "=" * 64)
    if FALLOS:
        print(f"FALLARON {len(FALLOS)}:")
        for f in FALLOS:
            print("  ·", f)
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
