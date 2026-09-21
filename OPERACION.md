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
| `stbrujulastartin` | Storage Account (Standard_LRS) | Guarda los cortes de datos (blobs) y los contadores del portero (tablas) |
| `stbrujulastartin/datos` | Contenedor de blobs | `cortes/AAAA-MM-DD/` y `actual/` |
| `brujula-ingesta` | Container Instance (y plantilla de Container App **Job** diario en `azure/ingesta_diaria.json`) | Corre la ingesta y se apaga; el script decide solo si toca publicar corte |
| `brujula-entorno` | Container Apps Environment | Red interna donde viven los dos servicios |
| `brujula-mcp` | Container App, **ingreso interno** | El servidor MCP: 13 herramientas sobre DuckDB y el corte en Blob. No es alcanzable desde internet |
| `brujula-orquestador` | Container App, ingreso externo | El agente: FastAPI + modelo de Foundry + portero. Es `acceso.brujula.startinlab.org` |
| `brujula-educativa-startin` | Azure AI Foundry | El modelo (`gpt-5.4-mini`) con identidad administrada |
| Static Web App | Sitio estático | Las páginas de `web/`, publicadas por GitHub Actions. Es `brujula.startinlab.org` |

La cuenta de almacenamiento va con TLS 1.2 mínimo y **acceso público
deshabilitado**: nada de lo que hay ahí se sirve directo a internet. **No hay
claves de Storage en ningún lado**: los tres contenedores entran con su identidad
administrada (lector de blobs el MCP, escritor la ingesta, colaborador de tablas
el orquestador).

Los dominios están en Hostinger: `brujula` → CNAME a la Static Web App;
`acceso.brujula` → CNAME al FQDN del orquestador, con el TXT
`asuid.acceso.brujula` que Container Apps pide para emitir el certificado.

### Cómo se despliega un cambio

Los contenedores **no llevan imagen propia**: al arrancar clonan `main` del
repositorio e instalan dependencias. Así que desplegar es dos pasos:

1. Subir el archivo al repositorio (Freddy lo hace desde GitHub; la carpeta
   `web/` la publica sola la acción `publicar-front.yml`).
2. Para `orquestador.py` o `proxy.py`, forzar una revisión nueva del orquestador
   para que vuelva a clonar:

   ```bash
   az containerapp update -g RG_FMC_BRUJULA -n brujula-orquestador \
     --set-env-vars BRUJULA_DESPLIEGUE=v18        # cualquier valor distinto al anterior
   ```

   Para `server.py`, lo mismo sobre `brujula-mcp`. Las páginas de `web/` no
   necesitan nada: la Static Web App las sirve en cuanto la acción termina.

Comprobar que arrancó bien:

```bash
curl -s https://acceso.brujula.startinlab.org/salud
# {"estado":"bien","herramientas":13,...}
```

Si `herramientas` es 0, el orquestador no alcanzó al MCP: mirar los logs con
`az containerapp logs show -g RG_FMC_BRUJULA -n brujula-orquestador --tail 100`.

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

`azure/ingesta_diaria.json` describe la misma ingesta como **Container App Job**
con disparo diario (`CADA_DIAS` decide cada cuántos días publica corte de
verdad). Si está desplegado, se lanza a mano con
`az containerapp job start -g RG_FMC_BRUJULA -n brujula-ingesta`.

El contenedor clona el repositorio, instala dependencias, corre los pasos
(`territorio`, `poblacion`, `datos_gov`, `matricula`, `icfes`, `secop`), construye
las fichas y publica el corte. Tarda entre una y dos horas; SECOP, los microdatos
del ICFES y la matrícula por sede (33 consultas agregadas a Socrata, una por
departamento) son los tramos largos. `SALTAR=poblacion,matricula` los omite si
hace falta un corte rápido.

Para forzar un corte aunque el vigente sea reciente (por ejemplo, tras agregar
una fuente nueva), se pasa `FORZAR=1` en las variables del contenedor.

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

`actual/` es lo que lee el agente. Se mueve **solo si el corte pasa la
validación** de `subir_blob.py`, que hace dos preguntas distintas:

1. **¿Hay algún archivo vacío?** Pisos absolutos, deliberadamente bajos.
2. **¿Alguna fuente encogió frente al corte anterior?** Más de un 20 % menos que
   lo publicado el mes pasado es una fuente que hoy respondió a medias.

La segunda es la que sirve, y se calibra sola. La primera versión usaba umbrales
escritos a mano —40.000 filas para Saber 11— calculados cuando la ingesta cubría
once periodos; al recortar a cinco el número real pasó a 21.749 y la validación
bloqueó un corte bueno. Un umbral a mano envejece mal: nadie lo mueve cuando
cambia el alcance. Una corrida a medias se guarda con
su fecha —sirve para depurar— pero no se convierte en la versión vigente: mejor
un dato de hace un mes que uno incompleto de hoy.

Cada corte lleva un `manifiesto.json` con la fecha, los archivos, si la
validación pasó y qué falló si no.

### Una carpeta por corrida, no por día

`cortes/` lleva una carpeta por corrida con fecha **y hora**
(`cortes/2026-09-19-2224/`), no solo fecha. La primera versión usaba solo la
fecha y dos corridas del mismo día se pisaban: la segunda sobrescribía los
archivos compartidos y también el manifiesto. El 19/09/2026 quedó así una
carpeta con quince archivos de dos corridas distintas, descrita por un
manifiesto que solo mencionaba dos. Un corte fechado existe para poder
reconstruir de dónde salió un número, y una mezcla de dos corridas no sirve
para eso. `actual/` sigue siendo el puntero al corte vigente.

### Por qué el disparo es diario y el corte mensual

La primera idea era disparar la ingesta una vez al mes. Tiene un defecto que se
vio el 19 de septiembre de 2026: ese día `datos.gov.co` estuvo caído —«Site
Currently Unavailable», el portal entero, no un conjunto— y las siete consultas
a Socrata agotaron sus reintentos en tres minutos. La corrida terminó sin
publicar nada, que es lo correcto, pero con un disparo mensual eso habría
significado **un mes entero con datos viejos y nadie enterado**.

Así que el disparo es diario y la decisión de trabajar la toma el propio script:

1. Pregunta si `datos.gov.co` responde. Si no, sale en segundos (código 3).
2. Pregunta la edad del corte vigente con `subir_blob.py --consultar`. Si tiene
   menos de `CADA_DIAS` (25 por defecto), sale sin hacer nada (código 0).
3. Solo si el portal responde y el corte ya pasó de 25 días, ingiere.

El resultado es un corte mensual que se refresca **el primer día en que las
fuentes estén de pie**, sin reintentos en el programador y sin nadie vigilando.
Un día de no-trabajo cuesta el arranque del contenedor y una lectura de blob:
del orden de un minuto de cómputo, centavos al año.

`FORZAR=1` ignora la edad del corte, para cuando se quiere refrescar a mano.

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

**La validación falla y `actual/` no se mueve.** Suele ser el comportamiento
correcto: mirar el `manifiesto.json` del corte, que dice qué archivo salió corto
y contra qué se comparó. Casi siempre es una fuente que respondió a medias ese
día; volver a correr.

Pero conviene desconfiar cuando fallan *todas* las fuentes a la vez: eso no es
una fuente caída, es la validación rota. Ya pasó una vez —el conteo de filas
devolvía cero para todo— y el síntoma fue exactamente ese. `probar_publicacion.py`
existe para que no vuelva a pasar sin avisar.

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
| Límite por IP y hora | Un script que dispara cientos de consultas |
| Caché | La repetición, que en una herramienta pública es la mayoría del tráfico |
| Presupuesto diario | Si el día se agota, no se llama al modelo (el acceso interno lo salta) |
| Presupuesto mensual | El freno duro. Nadie lo salta |
| Preguntas libres, por navegador y por IP | Cuándo se pide registro |

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
python probar_proxy.py         # incluye la prueba de concurrencia
python probar_publicacion.py   # la validación que decide si se publica
python probar_server.py        # las 13 herramientas del MCP
```

Corre sin credenciales ni SDK de Azure: usa una tabla simulada que reproduce los
conflictos de versión. Un almacén que solo se puede probar contra el servicio
real termina sin probarse.

---

## Ámbitos: país, departamento, municipio, colegio

Los agregados por departamento y país **se calculan al arrancar el servidor
MCP** a partir de `fichas_municipio.parquet` (tablas `fichas_depto` y
`ficha_pais` en DuckDB), no en la ingesta: tardan milisegundos y así un corte
ya publicado gana la vista sin volver a correr una hora de ingesta. Si el log
del MCP dice «No se pudieron construir los agregados», el corte es anterior a
las columnas nuevas (población, matrícula): las herramientas `ficha_departamento`
y `ficha_pais` responden que no están disponibles y el resto sigue.

El orquestador expone `/pais?indicador=` y `/departamento?departamento=&indicador=`
(lectura, caché de 6 h, sin cupo). `ranking_nacional` ya no exige token: la
fundación abrió el país a todo el mundo; el freno es el presupuesto mensual.

## Actualidad: titulares de prensa

El orquestador consulta el RSS de Google Noticias acotado al territorio, a
educación y a los últimos 90 días, y devuelve **titulares con medio, fecha y
enlace**, tal cual (`/actualidad`, y la herramienta local `titulares_recientes`
para el agente). Caché de 6 h en memoria; sin llave ni costo.

La regla, acordada con la fundación el 20/09/2026: Brújula **no lee, no resume
y no interpreta** los artículos. El modelo tiene instrucción explícita de citar
titulares como titulares y de no afirmar corrupción, irregularidad ni mala
gestión a partir de uno. Si Google cambia o bloquea el RSS, el bloque dice «no
se pudo consultar la prensa» y todo lo demás sigue; `BRUJULA_ACTUALIDAD=0` lo
apaga sin tocar código.

## Niveles de acceso

Quien llega tiene uno de tres niveles, y el portero decide con ellos:

| Nivel | Cómo se obtiene | Qué le aplica |
|---|---|---|
| **Libre** | Abrir la página | 10 preguntas por navegador, 25 por conexión (IP) y día, 30 por hora; presupuesto del día |
| **Registrado** | Formulario + enlace que llega al correo | Sin cupo de preguntas; sigue el tope por hora y el presupuesto del día |
| **Interno** | Igual que registrado, pero el correo es de un dominio de `BRUJULA_DOMINIOS_INTERNOS` (hoy `startin.org.co`) | Sin cupo, sin tope por hora, sin presupuesto del día. **Sí** el tope del mes |

El nivel se fija **al hacer clic en el enlace del correo**, no al llenar el
formulario: la única prueba de que alguien es de Startin es que el enlace llegó a
un buzón `@startin.org.co`. La comparación del dominio es exacta
(`startin.org.co`, no «termina en startin»), para que `startin.org.co.evil.com`
no cuele.

El acceso interno existe para las demostraciones: una presentación no puede
morir porque el público gastó la cuota del día o porque diez personas de la sala
salen por la misma IP. Lo que **nadie** se salta es el tope mensual de
`BRUJULA_PRESUPUESTO_MENSUAL_USD` (300): ese es el compromiso con el presupuesto
de la fundación y está probado (`probar_proxy.py`, prueba 18).

Una credencial acreditada antes de que existieran niveles cuenta como
**registrada**. Para que un correo de Startin pase a interno hay que registrarse
otra vez y entrar por el enlace nuevo.

### El panel del equipo

`web/admin.html`, enlazado desde el agente solo para quien entra con nivel
interno. Muestra registros (nombre, organización, correo, propósito, estado),
accesos vigentes por nivel, consultas y gasto de los últimos 14 días y el
presupuesto del mes, y permite revocar un acceso. Lo alimentan dos rutas del
orquestador:

- `GET /admin/resumen` y `POST /admin/revocar {ficha}`, ambas exigen la cabecera
  `X-Brujula-Acceso: <credencial>` con nivel interno; cualquier otra cosa recibe
  403. La credencial **no viaja en la URL**, para que no quede en historiales ni
  en registros de acceso.

No hay una base de «analítica» aparte: el panel lee los mismos contadores que
el portero necesita para funcionar. Una herramienta que promete no rastrear a
nadie no debería tener otra.

### Variables de entorno del orquestador

| Variable | Valor hoy | Para qué |
|---|---|---|
| `BRUJULA_MCP_URL` | `https://brujula-mcp.internal…/sse` | Dónde está el MCP (FQDN interno) |
| `BRUJULA_FOUNDRY_ENDPOINT`, `BRUJULA_MODELO` | Foundry, `gpt-5.4-mini` | El modelo |
| `BRUJULA_SITIO` | `https://brujula.startinlab.org` | La página; adónde vuelve la gente tras entrar |
| `BRUJULA_API` | `https://acceso.brujula.startinlab.org` | Este servicio; sobre él se arman los enlaces del correo |
| `BRUJULA_BUZON` | `brujula@startin.org.co` | Desde dónde salen los correos (Graph, identidad administrada) |
| `BRUJULA_AVISOS` | `portales@startin.org.co` | A dónde llega el aviso de cada registro nuevo, con su enlace de revocación |
| `BRUJULA_DOMINIOS_INTERNOS` | `startin.org.co` | Dominios con acceso interno, separados por coma |
| `BRUJULA_PRESUPUESTO_MENSUAL_USD` | `300` | El tope duro |
| `BRUJULA_PREGUNTAS_LIBRES`, `BRUJULA_PREGUNTAS_LIBRES_IP_DIA`, `BRUJULA_LIMITE_IP_HORA` | `10`, `25`, `30` | Cupos del nivel libre |
| `BRUJULA_VERSION_CACHE` | `5` | Cambiarla invalida la caché entera (hacerlo al publicar un corte nuevo) |
| `BRUJULA_ACTUALIDAD`, `BRUJULA_ACTUALIDAD_DIAS`, `BRUJULA_ACTUALIDAD_MAX` | `1`, `90`, `6` | Titulares de prensa (Google Noticias RSS): apagar con `0`; ventana en días; cuántos se muestran |
| `BRUJULA_DESPLIEGUE` | `vN` | Sin efecto en el código; cambiarla fuerza una revisión nueva |

Y en el MCP: `BRUJULA_MCP_ANFITRIONES`, la lista de nombres de host con los que
acepta peticiones (protección contra *DNS rebinding* del SDK). Si el FQDN interno
cambia, hay que actualizarla o el MCP responde 421.

---

## Lo que falta

- **Confirmar el disparo diario** (`azure/ingesta_diaria.json` desplegado y con
  su primera corrida en verde).
- **Cambiar `BRUJULA_VERSION_CACHE` desde la ingesta** al publicar un corte,
  para que no dependa de que alguien lo recuerde.
- **Docentes por municipio** no tiene fuente nacional abierta (solo por ETC, y
  solo oficiales, ya incorporados). Equipos solo como el acumulado histórico de
  Computadores Para Educar, con su advertencia.
- **TerriData del DNP**, que es descarga de archivo, no API.
- **Apagar el acceso elevado en Entra ID**, que sigue activo desde el
  aprovisionamiento.

---

Fundación Startin — [hola@startin.org.co](mailto:hola@startin.org.co)
