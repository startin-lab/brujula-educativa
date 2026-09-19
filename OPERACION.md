# Operación

Cómo se corre Brújula, qué hay montado en Azure y qué hacer cuando algo falla.

El `README.md` documenta **los datos**: de dónde salen, qué trampas tienen y qué
decisiones se tomaron. Este documento es lo otro: **la infraestructura**. Existe
porque el código explica qué hace pero no dónde vive, y esa parte se olvida en
tres meses.

---

## Qué hay montado

Todo vive en la suscripción **Startin - Azure - Mcsft Non Profit**
(`b37ec3b1-bd89-4cb9-a2fc-bccfbd6c485a`), grupo de recursos **RG_FMC_BRUJULA**,
región **East US 2**.

| Recurso | Tipo | Para qué |
|---|---|---|
| `stbrujulastartin` | Storage Account (Standard_LRS) | Guarda los cortes de datos |
| `stbrujulastartin/datos` | Contenedor de blobs | `cortes/AAAA-MM-DD/` y `actual/` |
| `brujula-ingesta` | Container Instance | Corre la ingesta y se apaga |

La cuenta de almacenamiento va con TLS 1.2 mínimo y **acceso público
deshabilitado**: nada de lo que hay ahí se sirve directo a internet.

### Qué cuesta

El contenedor **se paga por segundo mientras corre**. Con 2 vCPU y 4 GB, una
corrida de una hora cuesta del orden de cinco centavos de dólar. Apagado no
cuesta nada. Lo que se paga siempre es el almacenamiento, y con estos volúmenes
son centavos al mes.

Una corrida mensual, entonces, cuesta menos de un dólar al año en cómputo. El
tope de gasto configurado para el proyecto es de USD 300/mes y esto no se le
acerca.

---

## Correr la ingesta

```bash
az container start -g RG_FMC_BRUJULA -n brujula-ingesta
```

El contenedor clona el repositorio, instala dependencias, corre las cuatro
fuentes, construye las fichas y publica el corte. Tarda alrededor de una hora;
SECOP y los microdatos son los tramos largos.

**Siempre toma la última versión de `main`.** No hay que reconstruir imagen ni
volver a crear el contenedor: basta con subir el código al repositorio y
arrancarlo. Si se cambió el código mientras corría, hay que pararlo y arrancarlo
de nuevo para que tome lo nuevo.

### Ver cómo va

```bash
az container logs -g RG_FMC_BRUJULA -n brujula-ingesta | tail -40
az container show  -g RG_FMC_BRUJULA -n brujula-ingesta \
  --query '{estado:instanceView.state, salida:containers[0].instanceView.currentState.exitCode}'
```

También desde el portal, en el contenedor → **Configuración → Contenedores →
Registros**. Esa vista es la más cómoda cuando Cloud Shell se cae, que pasa.

### Pararla

```bash
az container stop -g RG_FMC_BRUJULA -n brujula-ingesta
```

Parar a mitad no corrompe nada: el corte solo se publica al final, y `actual/`
solo se mueve si pasa las validaciones.

---

## Qué queda en Blob

```
datos/
  cortes/2026-09-19/     ← todo lo de esa corrida, con su manifiesto
  cortes/2026-10-19/
  actual/                ← el último corte que pasó las validaciones
```

**Los cortes fechados no se borran.** Una cifra citada en un diagnóstico de marzo
tiene que seguir siendo verificable en julio; si cada ingesta pisara a la
anterior, nadie podría reconstruir de dónde salió un número.

`actual/` es lo que lee el agente. Se mueve **solo si la corrida pasa las cinco
validaciones de volumen** de `subir_blob.py`. Una corrida a medias se guarda con
su fecha —sirve para depurar— pero no se convierte en la versión vigente: mejor
un dato de hace un mes que uno incompleto de hoy.

Cada corte lleva un `manifiesto.json` con la fecha, los archivos, si la
validación pasó y qué falló si no.

### Revisar un corte

```bash
az storage blob list --account-name stbrujulastartin -c datos \
  --prefix cortes/ --auth-mode login -o table

az storage blob download --account-name stbrujulastartin -c datos \
  -n actual/manifiesto.json -f /tmp/manifiesto.json --auth-mode login && cat /tmp/manifiesto.json
```

---

## Cuando algo falla

**El contenedor no arranca: `RegistryErrorResponse` de `index.docker.io`.**
Docker Hub limita las descargas anónimas desde Azure. Por eso la imagen es
`mcr.microsoft.com/devcontainers/python:3.12`, del registro de Microsoft, que
además ya trae git. No volver a Docker Hub.

**La validación falla y `actual/` no se mueve.** Es el comportamiento correcto,
no un error. Mirar el `manifiesto.json` del corte: dice qué archivo salió corto.
Casi siempre es una fuente que respondió a medias ese día. Volver a correr.

**Una fuente falla pero la corrida sigue.** También es a propósito. Las fuentes
públicas colombianas se caen por turnos; que el ICFES esté abajo un martes no es
razón para quedarnos sin MEN, SECOP y territorio. El resumen final dice qué pasó
con cada paso.

**Cloud Shell se desconecta.** Pasa seguido en sesiones largas. Los comandos
`az` se pueden correr desde cualquier terminal con la CLI instalada, y el estado
del contenedor se ve completo desde el portal. La ingesta no depende de Cloud
Shell: corre sola en el contenedor.

**Saltarse pasos en una corrida de prueba:**

```bash
# vía variable de entorno del contenedor, o localmente:
SALTAR=icfes,secop bash correr_ingesta.sh
```

---

## Correr todo a mano

Sin contenedor, en cualquier máquina con Python 3.12:

```bash
git clone https://github.com/startin-lab/brujula-educativa.git && cd brujula-educativa
pip install pandas requests pyarrow duckdb openpyxl xlrd azure-storage-blob
export AZURE_STORAGE_CONNECTION_STRING="..."
bash correr_ingesta.sh
```

Y para probar el servidor MCP sin tocar la red ni los datos reales:

```bash
python probar_server.py
```

Genera un territorio sintético, construye las fichas y ejerce las trece
herramientas. Devuelve 0 si todo pasa. **Correrlo antes de subir cualquier
cambio.**

---

## El proxy de costo

`proxy.py` se para entre el front y el agente. Azure **no tiene un freno duro por
dólares** —los presupuestos de Cost Management avisan, no detienen— así que el
tope real se arma aquí, en cuatro capas:

| Capa | Qué ataja |
|---|---|
| Límite por IP | Un script que dispara cientos de consultas |
| Caché | La repetición, que en una herramienta pública es la mayoría del tráfico |
| Presupuesto diario | El freno duro. Si el día se agota, no se llama al modelo |
| Preguntas libres | Cuándo se pide registro |

El orden importa. El límite por IP va primero porque un script vacía el
presupuesto antes de que la caché se caliente. La caché va antes que la cuota
porque una respuesta guardada no cuesta nada y no tiene por qué gastarle a nadie
una de sus preguntas libres.

### Cuánto puede gastar como máximo

De los **USD 300** mensuales reparte **8,50 al día** y solo usa el **85 %**: el
techo real son **USD 255 en 30 días**. El margen que queda permite que las
respuestas en caché y los mensajes de error sigan funcionando cuando el día se
agota.

```bash
export BRUJULA_PRESUPUESTO_MENSUAL_USD=300
export BRUJULA_USD_POR_MILLON_ENTRADA=3
export BRUJULA_USD_POR_MILLON_SALIDA=15
export BRUJULA_PREGUNTAS_LIBRES=10
export BRUJULA_LIMITE_IP_HORA=30
export BRUJULA_CACHE_DIAS=7
```

**Las tarifas por millón de tokens son el único punto donde el proxy traduce
consumo a dinero.** Si el modelo cambia y esas cifras se quedan viejas, el tope
queda mal calculado sin que nada lo avise. Revisarlas al cambiar de modelo.

### Contadores compartidos

Con `AZURE_STORAGE_CONNECTION_STRING` definida, los contadores van a Table
Storage en `stbrujulastartin`. Sin ella, quedan en memoria y el proxy **lo avisa
en el log**: con varias réplicas, cada una llevaría su propio tope de 8,50
diarios y el límite se multiplicaría en silencio.

El contador del día está **repartido en 16 filas**, y no por gusto. Cada petición
suma al gasto del mismo día, así que con una sola fila todas las réplicas se
pisan entre sí. Medido con ocho hilos y 200 sumas: **478 conflictos de versión, y
se perdía el 28 % del gasto**. Repartido en fragmentos bajó a 22 conflictos y el
total quedó exacto. Un contador que pierde gasto no frena nada.

### Falla cerrado

Si Table Storage no responde, el proxy **no deja pasar la petición**. Un contador
caído significa que no sabemos cuánto llevamos gastado, y gastar sin saber es
justo lo que este componente existe para impedir. Está probado.

```bash
python probar_proxy.py    # incluye la prueba de concurrencia
```

Corre sin credenciales ni SDK de Azure: usa una tabla simulada que reproduce los
conflictos de versión. Un almacén que solo se puede probar contra el servicio
real termina sin probarse.

---

## Lo que falta

- **Desplegar el servidor MCP.** `server.py` está probado pero todavía no está
  en Container Apps: hoy solo corre local.
- **Desplegar el proxy.** El código está probado pero todavía no está en pie.
- **El front y el dominio** `brujula.startinlab.org` (DNS en Hostinger).
- **La programación mensual.** Hoy la ingesta se arranca a mano.
- **TerriData del DNP**, que es descarga de archivo, no API.
- **Apagar el acceso elevado en Entra ID**, que sigue activo desde el
  aprovisionamiento.

---

Fundación Startin — [hola@startin.org.co](mailto:hola@startin.org.co)
