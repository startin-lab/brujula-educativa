#!/usr/bin/env python3
"""
Brújula Educativa — Publicación de un corte fechado en Blob Storage
Fundación Startin

Sube los parquet de una corrida a Azure Blob Storage bajo un prefijo con fecha,
y actualiza un puntero `actual/` que apunta al último corte bueno.

POR QUÉ CORTES FECHADOS Y NO SOBRESCRIBIR

  Las fuentes cambian sin avisar. El ICFES corrige un archivo, el MEN republica
  una vigencia, SECOP reclasifica un contrato. Si cada ingesta pisara la
  anterior, una cifra citada en un diagnóstico de marzo sería inverificable en
  julio: nadie podría reconstruir de dónde salió.

  Con cortes fechados, cada respuesta del agente puede decir «datos del corte
  2026-09-19» y ese corte sigue ahí. Es la diferencia entre un dato citable y un
  número que hay que creer.

  `actual/` se actualiza SOLO si la corrida pasó las validaciones. Una ingesta a
  medias no debe convertirse en la versión vigente por el simple hecho de haber
  terminado.

Uso:
    export AZURE_STORAGE_CONNECTION_STRING="..."
    python subir_blob.py --datos ./data --contenedor datos
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("blob")

# Pisos absolutos: solo detectan el archivo vacío o ridículamente corto.
#
# POR QUÉ TAN BAJOS, Y POR QUÉ NO SON LA VALIDACIÓN PRINCIPAL
#
#   La primera versión traía umbrales "razonables" inventados a mano —40.000
#   filas para Saber 11, por ejemplo— calculados cuando la ingesta cubría once
#   periodos. Al recortar el alcance a cinco, el número real pasó a 21.749 y la
#   validación bloqueó un corte perfectamente bueno.
#
#   Un umbral escrito a mano envejece mal: nadie se acuerda de moverlo cuando
#   cambia el alcance, y termina rechazando datos correctos o —peor— dejando
#   pasar datos malos porque se puso demasiado bajo para que dejara de molestar.
#
#   Por eso el piso absoluto solo pregunta "¿esto está vacío?", y la validación
#   de verdad compara contra el ÚLTIMO CORTE PUBLICADO: si una fuente cae más
#   de lo tolerado frente a lo que ya había, algo pasó hoy. Esa regla se calibra
#   sola y no hay que mantenerla.
PISOS = {
    "territorio_municipios.parquet": 1000,
    "territorio_centros_poblados.parquet": 5000,
    "men_municipios.parquet": 5000,
    "saber11_colegios.parquet": 5000,
    "fichas_municipio.parquet": 1000,
    "fichas_lugar.parquet": 5000,
}

# Cuánto puede encoger una fuente respecto al corte anterior antes de sospechar.
# Un 20 % cubre la variación normal entre vigencias; una caída mayor es una
# fuente que respondió a medias.
CAIDA_TOLERADA = 0.20


def contar_filas(datos: Path) -> dict[str, int]:
    """
    Filas de cada parquet, leyendo SOLO el pie del archivo.

    No usar `len(pd.read_parquet(ruta, columns=[]))`. En pandas 3 eso devuelve
    CERO para cualquier archivo, porque un DataFrame sin columnas no tiene
    filas. La versión anterior de este archivo lo hacía así, y el efecto fue que
    todas las fuentes se contaron como vacías: la validación rechazó un corte
    perfectamente bueno y `actual/` no se movió.

    Lo peor de ese error es que fallaba hacia el lado "seguro" —no publicar—,
    así que no rompía nada de forma visible. Habría bloqueado todos los cortes,
    todos los meses, sin que nada explicara por qué.

    El metadato de Parquet trae el conteo exacto y no lee ni una fila de datos.
    """
    import pyarrow.parquet as pq

    conteos: dict[str, int] = {}
    for ruta in sorted(datos.glob("*.parquet")):
        try:
            conteos[ruta.name] = pq.ParquetFile(ruta).metadata.num_rows
        except Exception as exc:  # noqa: BLE001
            LOG.warning("No se pudo leer %s: %s", ruta.name, exc)
            conteos[ruta.name] = -1
    return conteos


def manifiesto_anterior(contenedor) -> dict:  # noqa: ANN001
    """El manifiesto del corte vigente, para comparar contra él."""
    try:
        crudo = contenedor.download_blob("actual/manifiesto.json").readall()
        return json.loads(crudo)
    except Exception:  # noqa: BLE001
        return {}


def validar(datos: Path, previo: dict) -> tuple[bool, list[str]]:
    """
    Dos preguntas distintas, y las dos importan:

      1. ¿Hay algún archivo vacío o absurdamente corto?  (piso absoluto)
      2. ¿Alguna fuente encogió mucho frente al corte anterior?  (comparación)

    La segunda es la que atrapa el caso real: una fuente que hoy respondió a
    medias y devolvió la mitad de lo de siempre. Un umbral fijo no lo ve si está
    puesto por debajo; la comparación con lo que ya había, sí.
    """
    problemas: list[str] = []
    conteos = contar_filas(datos)

    for archivo, piso in PISOS.items():
        n = conteos.get(archivo)
        if n is None:
            problemas.append(f"falta {archivo}")
        elif n < piso:
            problemas.append(f"{archivo}: {n} filas, por debajo del piso de {piso}")

    antes = previo.get("filas", {})
    for archivo, n in conteos.items():
        anterior = antes.get(archivo)
        if not anterior or n < 0:
            continue
        if n < anterior * (1 - CAIDA_TOLERADA):
            caida = (1 - n / anterior) * 100
            problemas.append(
                f"{archivo}: {n} filas frente a {anterior} del corte anterior "
                f"({caida:.0f} % menos)"
            )

    return (not problemas), problemas


def main() -> int:
    parser = argparse.ArgumentParser(description="Sube un corte fechado a Blob Storage")
    parser.add_argument("--datos", type=Path, default=Path("./data"))
    parser.add_argument("--contenedor", default="datos")
    parser.add_argument("--forzar", action="store_true",
                        help="Actualiza 'actual/' aunque falle la validación")
    parser.add_argument("--consultar", action="store_true",
                        help="No sube nada: imprime la fecha y la edad en días "
                             "del corte vigente, y termina")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    # El SDK de Azure registra cada petición HTTP completa, con cabeceras. En una
    # corrida con veinte archivos eso son cientos de líneas que entierran el
    # único mensaje que importa: si la validación pasó o no.
    for ruidoso in ("azure", "azure.core.pipeline.policies.http_logging_policy",
                    "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        LOG.error("Falta azure-storage-blob: pip install azure-storage-blob")
        return 1

    conexion = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conexion:
        LOG.error("Sin AZURE_STORAGE_CONNECTION_STRING no hay dónde subir.")
        return 1

    archivos = sorted(args.datos.glob("*.parquet")) + sorted(args.datos.glob("*.json"))
    if not archivos and not args.consultar:
        LOG.error("No hay nada que subir en %s", args.datos)
        return 1

    corte = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cliente = BlobServiceClient.from_connection_string(conexion)
    contenedor = cliente.get_container_client(args.contenedor)
    try:
        contenedor.create_container()
        LOG.info("Contenedor %s creado", args.contenedor)
    except Exception:  # noqa: BLE001
        pass  # ya existía

    if args.consultar:
        # Lo usa correr_ingesta.sh para decidir si hay algo que hacer hoy. Se
        # imprime en una sola línea legible por `read` en bash.
        vigente = manifiesto_anterior(contenedor)
        fecha = vigente.get("corte", "")
        if fecha:
            try:
                dias = (datetime.now(timezone.utc).date()
                        - datetime.strptime(fecha, "%Y-%m-%d").date()).days
            except ValueError:
                fecha, dias = "ilegible", 9999
        else:
            fecha, dias = "ninguno", 9999
        print(f"corte={fecha} dias={dias}")
        return 0

    previo = manifiesto_anterior(contenedor)
    if previo:
        LOG.info("Comparando contra el corte %s", previo.get("corte", "?"))
    else:
        LOG.info("No hay corte anterior: primera publicación")
    ok, problemas = validar(args.datos, previo)

    # El corte fechado se sube SIEMPRE, incluso si falló la validación: una
    # corrida mala es evidencia de qué pasó ese día y sirve para depurar.
    subidos = 0
    for archivo in archivos:
        destino = f"cortes/{corte}/{archivo.name}"
        with archivo.open("rb") as f:
            contenedor.upload_blob(name=destino, data=f, overwrite=True)
        subidos += 1
        LOG.info("  %s  (%.1f MB)", destino, archivo.stat().st_size / 1e6)

    manifiesto = {
        "corte": corte,
        "generado": datetime.now(timezone.utc).isoformat(),
        "archivos": [a.name for a in archivos],
        # Los conteos viajan en el manifiesto porque son la referencia contra la
        # que se validará el corte del mes que viene.
        "filas": contar_filas(args.datos),
        "validacion_ok": ok,
        "problemas": problemas,
    }
    contenedor.upload_blob(
        name=f"cortes/{corte}/manifiesto.json",
        data=json.dumps(manifiesto, ensure_ascii=False, indent=2).encode(),
        overwrite=True,
    )

    if ok or args.forzar:
        for archivo in archivos:
            with archivo.open("rb") as f:
                contenedor.upload_blob(name=f"actual/{archivo.name}", data=f, overwrite=True)
        contenedor.upload_blob(
            name="actual/manifiesto.json",
            data=json.dumps(manifiesto, ensure_ascii=False, indent=2).encode(),
            overwrite=True,
        )
        LOG.info("'actual/' ahora apunta al corte %s", corte)
    else:
        # Se deja el corte anterior como vigente. Es lo correcto: mejor un dato
        # de hace un mes que uno incompleto de hoy.
        LOG.warning("Validación FALLIDA — 'actual/' sigue apuntando al corte anterior:")
        for p in problemas:
            LOG.warning("   · %s", p)

    print("\n" + "=" * 56)
    print(f"Corte           : {corte}")
    print(f"Archivos        : {subidos}")
    print(f"Validación      : {'OK' if ok else 'FALLIDA'}")
    print(f"'actual/'       : {'actualizado' if (ok or args.forzar) else 'SIN CAMBIOS'}")
    print("=" * 56)

    return 0 if (ok or args.forzar) else 2


if __name__ == "__main__":
    sys.exit(main())
