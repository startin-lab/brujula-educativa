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

# Mínimos de cordura. Una corrida que produzca menos que esto está rota, aunque
# haya terminado sin excepciones, y no puede pasar a ser el corte vigente.
MINIMOS = {
    "territorio_municipios.parquet": 1000,
    "territorio_centros_poblados.parquet": 7000,
    "men_municipios.parquet": 10000,
    "fichas_municipio.parquet": 1000,
    "fichas_lugar.parquet": 8000,
}


def validar(datos: Path) -> tuple[bool, list[str]]:
    """
    Comprueba que la corrida tenga sentido antes de declararla vigente.

    No valida contenido, valida volumen: es barato y atrapa el modo de falla más
    común, que es una fuente que respondió a medias y dejó un parquet enano.
    """
    import pandas as pd

    problemas: list[str] = []
    for archivo, minimo in MINIMOS.items():
        ruta = datos / archivo
        if not ruta.exists():
            problemas.append(f"falta {archivo}")
            continue
        n = len(pd.read_parquet(ruta, columns=[]))
        if n < minimo:
            problemas.append(f"{archivo}: {n} filas, se esperaban al menos {minimo}")
    return (not problemas), problemas


def main() -> int:
    parser = argparse.ArgumentParser(description="Sube un corte fechado a Blob Storage")
    parser.add_argument("--datos", type=Path, default=Path("./data"))
    parser.add_argument("--contenedor", default="datos")
    parser.add_argument("--forzar", action="store_true",
                        help="Actualiza 'actual/' aunque falle la validación")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

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
    if not archivos:
        LOG.error("No hay nada que subir en %s", args.datos)
        return 1

    ok, problemas = validar(args.datos)
    corte = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cliente = BlobServiceClient.from_connection_string(conexion)
    contenedor = cliente.get_container_client(args.contenedor)
    try:
        contenedor.create_container()
        LOG.info("Contenedor %s creado", args.contenedor)
    except Exception:  # noqa: BLE001
        pass  # ya existía

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
