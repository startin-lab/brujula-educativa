#!/usr/bin/env bash
#
# Brújula Educativa — Arranque del servidor MCP
# Fundación Startin
#
# Baja el corte vigente desde Blob Storage y levanta el servidor MCP.
#
# POR QUÉ SE BAJA EL CORTE EN VEZ DE LEER BLOB EN CADA CONSULTA
#
#   Las fichas son ~40 MB de parquet y cambian una vez al mes. Leerlas desde
#   Blob en cada pregunta sería pagar latencia de red para datos que no se
#   mueven. Se bajan al arrancar, se leen desde disco con DuckDB, y el servidor
#   se reinicia cuando hay corte nuevo.
#
#   El efecto secundario es bueno: si Blob se cae, el servidor sigue
#   respondiendo con el corte que ya tiene. Un diagnóstico no debería quedarse
#   sin datos porque el almacenamiento tuvo un mal minuto.
#
# ARRANCA SOLO CON DATOS COMPLETOS
#
#   Si falta cualquier archivo obligatorio, el script NO levanta el servidor.
#   Un MCP a medias es peor que uno caído: responde, y responde mal. Container
#   Apps reintentará el arranque, que es el comportamiento correcto.
#
# Variables:
#   AZURE_STORAGE_CONNECTION_STRING   de dónde bajar el corte
#   BRUJULA_TOKEN_NACIONAL            habilita las consultas de país entero
#   CONTENEDOR                        (opcional) por defecto "datos"
#   PUERTO                            (opcional) por defecto 8080

set -uo pipefail

DATOS="${DATOS:-/app/data}"
CONTENEDOR="${CONTENEDOR:-datos}"
PUERTO="${PUERTO:-8080}"

OBLIGATORIOS=(
  fichas_municipio.parquet
  fichas_sede.parquet
  fichas_lugar.parquet
  men_municipios.parquet
  saber11_colegios.parquet
)

echo "================================================================"
echo " Brújula Educativa — servidor MCP $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"

mkdir -p "$DATOS"

if [[ -z "${AZURE_STORAGE_CONNECTION_STRING:-}" ]]; then
  echo "Sin AZURE_STORAGE_CONNECTION_STRING: se usará lo que ya haya en $DATOS"
else
  echo ">>> Bajando el corte vigente de $CONTENEDOR/actual/"
  python3 - <<'PY'
import os, sys
from pathlib import Path
from azure.storage.blob import BlobServiceClient

destino = Path(os.environ.get("DATOS", "/app/data"))
contenedor = os.environ.get("CONTENEDOR", "datos")
cliente = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
cc = cliente.get_container_client(contenedor)

bajados = 0
for blob in cc.list_blobs(name_starts_with="actual/"):
    nombre = blob.name.split("/", 1)[1]
    if not nombre:
        continue
    ruta = destino / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("wb") as f:
        f.write(cc.download_blob(blob.name).readall())
    print(f"    {nombre}  ({blob.size/1e6:.1f} MB)")
    bajados += 1
print(f"    {bajados} archivos")
if not bajados:
    # Sin corte no hay nada que servir. Se sale con error para que el
    # orquestador reintente en vez de dejar un servidor vacío en pie.
    sys.exit("No hay nada en actual/: ¿ya corrió la ingesta?")
PY
  if [[ $? -ne 0 ]]; then
    echo "FALLÓ la descarga del corte. No se levanta el servidor."
    exit 1
  fi
fi

# --- Verificación antes de levantar ------------------------------------- #
faltan=()
for archivo in "${OBLIGATORIOS[@]}"; do
  [[ -s "$DATOS/$archivo" ]] || faltan+=("$archivo")
done

if (( ${#faltan[@]} )); then
  echo ""
  echo "NO SE LEVANTA EL SERVIDOR. Faltan archivos del corte:"
  printf '   · %s\n' "${faltan[@]}"
  echo "Un servidor a medias responde, y responde mal."
  exit 1
fi

if [[ -f "$DATOS/manifiesto.json" ]]; then
  echo ""
  echo ">>> Corte cargado:"
  python3 -c "
import json,sys
m = json.load(open('$DATOS/manifiesto.json'))
print(f\"    fecha      : {m.get('corte')}\")
print(f\"    validación : {'OK' if m.get('validacion_ok') else 'CON PROBLEMAS'}\")
print(f\"    archivos   : {len(m.get('archivos', []))}\")
for p in m.get('problemas', []): print('    ·', p)
"
fi

if [[ -z "${BRUJULA_TOKEN_NACIONAL:-}" ]]; then
  echo ""
  echo "AVISO: sin BRUJULA_TOKEN_NACIONAL las consultas de país entero quedan"
  echo "       cerradas para todos. Es lo correcto si nadie tiene acceso completo."
fi

echo ""
echo ">>> Sirviendo en el puerto $PUERTO"
exec python3 server.py --datos "$DATOS" --puerto "$PUERTO"
