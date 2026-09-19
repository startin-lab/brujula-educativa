#!/usr/bin/env bash
#
# Brújula Educativa — Corrida completa de ingesta
# Fundación Startin
#
# Lo que ejecuta el contenedor: baja el código, trae los datos de las cinco
# fuentes, construye las fichas y publica un corte fechado en Blob Storage.
#
# Está pensado para correr SOLO, sin nadie mirando: una vez ahora y una vez al
# mes. Por eso cada paso registra cuánto tardó y en qué terminó, y por eso una
# fuente caída no aborta la corrida.
#
# POR QUÉ NO SE DETIENE EN EL PRIMER ERROR
#
#   Las fuentes públicas colombianas se caen por turnos. Si el ICFES está abajo
#   un martes, eso no es razón para quedarnos también sin los datos del MEN, de
#   SECOP y del territorio. Cada paso se intenta, se registra su resultado, y al
#   final `subir_blob.py` decide si lo conseguido alcanza para ser el corte
#   vigente. Una corrida parcial se guarda con su fecha, pero no reemplaza a la
#   anterior.
#
# Variables que espera:
#   AZURE_STORAGE_CONNECTION_STRING   dónde publicar el corte
#   REPO                              (opcional) repositorio de origen
#   SALTAR                            (opcional) pasos a omitir, separados por coma
#                                     ej: SALTAR=icfes,saber11

set -u  # variable sin definir es error; pero NO set -e: los pasos fallan solos

REPO="${REPO:-https://github.com/startin-lab/brujula-educativa.git}"
TRABAJO="${TRABAJO:-/tmp/brujula}"
DATOS="$TRABAJO/data"
SALTAR="${SALTAR:-}"

declare -A RESULTADO
declare -A SEGUNDOS

omitido() { [[ ",$SALTAR," == *",$1,"* ]]; }

paso() {
  local nombre="$1"; shift
  if omitido "$nombre"; then
    RESULTADO[$nombre]="OMITIDO"; SEGUNDOS[$nombre]=0
    echo ">>> $nombre — omitido por SALTAR"
    return 0
  fi
  echo ""
  echo ">>> $nombre — $(date -u +%H:%M:%S)"
  local t0; t0=$(date +%s)
  if "$@"; then RESULTADO[$nombre]="OK"; else RESULTADO[$nombre]="FALLÓ"; fi
  SEGUNDOS[$nombre]=$(( $(date +%s) - t0 ))
  echo ">>> $nombre — ${RESULTADO[$nombre]} en ${SEGUNDOS[$nombre]}s"
}

echo "================================================================"
echo " Brújula Educativa — ingesta $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"

rm -rf "$TRABAJO"
git clone -q "$REPO" "$TRABAJO" || { echo "No se pudo clonar $REPO"; exit 1; }
cd "$TRABAJO" || exit 1
mkdir -p "$DATOS"
echo "Commit: $(git log -1 --format='%h %s')"

pip install --quiet --no-cache-dir \
  pandas requests pyarrow duckdb openpyxl xlrd azure-storage-blob \
  || { echo "Falló la instalación de dependencias"; exit 1; }

# --- Fuentes. El orden importa poco salvo por una cosa: territorio va primero
# --- porque es el más rápido, y si falla algo del entorno se ve en 5 segundos
# --- en vez de a la media hora.
paso territorio  python3 ingest_territorio.py --salida "$DATOS"
paso datos_gov   python3 ingest_datos_gov.py  --salida "$DATOS"
paso icfes       python3 ingest_icfes.py      --salida "$DATOS"
paso secop       python3 ingest_secop.py      --salida "$DATOS"

# --- Fichas: solo tiene sentido si al menos el MEN llegó, que es quien define
# --- el universo de municipios.
if [[ -f "$DATOS/men_municipios.parquet" ]]; then
  paso fichas python3 construir_fichas.py --datos "$DATOS" --salida "$DATOS"
else
  RESULTADO[fichas]="SIN INSUMO"; SEGUNDOS[fichas]=0
  echo ">>> fichas — no se construyen: falta men_municipios.parquet"
fi

paso publicar python3 subir_blob.py --datos "$DATOS" --contenedor "${CONTENEDOR:-datos}"

echo ""
echo "================================================================"
printf "%-14s %-12s %10s\n" "PASO" "ESTADO" "SEGUNDOS"
echo "----------------------------------------------------------------"
for p in territorio datos_gov icfes secop fichas publicar; do
  printf "%-14s %-12s %10s\n" "$p" "${RESULTADO[$p]:-?}" "${SEGUNDOS[$p]:-0}"
done
echo "================================================================"
ls -lh "$DATOS" 2>/dev/null | tail -n +2

# El código de salida refleja si la publicación salió bien, que es lo único que
# determina si el agente verá datos nuevos.
[[ "${RESULTADO[publicar]:-}" == "OK" ]] && exit 0 || exit 1
