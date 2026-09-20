# Brújula Educativa

Agente público de consulta sobre datos educativos abiertos de Colombia.
Un proyecto de [Fundación Startin](https://startin.org.co).

Los datos de la educación colombiana llevan años publicados. El problema nunca
fue que no existieran: es que están repartidos en cuatro portales, en archivos
de millones de filas, y hay que saber SQL para leerlos. Brújula los responde en
español, citando siempre la fuente y la fecha de corte.

---

## Fuentes conectadas

| Fuente | Entidad | Qué aporta | Corte |
|---|---|---|---|
| Resultados agregados por establecimiento | ICFES | Promedio, desviación y evaluados por área, sede por sede | **2021 – 2025** |
| Resultados únicos Saber 11 (`kgxf-xxbe`) | ICFES | Brecha digital del hogar: internet y computador declarados | **2019 – 2022** |
| Estadísticas en educación por municipio (`nudc-7mev`) | MinEducación | 41 indicadores de cobertura, deserción, aprobación y repitencia por nivel | 2011 – 2024 |
| Computadores Para Educar (`pyqj-s96k`) | MinTIC | Qué recibió ya cada municipio: equipos, docentes formados, inversión | 2010 – 2022 |
| Indicadores de infraestructura (`3ncw-3qwq`) | MinEducación | Aulas nuevas y mejoradas por sede | hasta 2021 |
| SECOP Integrado (`rpmr-utcd`) | Colombia Compra Eficiente | Contratos cuyo objeto menciona educación, por municipio de la entidad | 2026-09 |
| Proyecciones de población municipal 2018-2042 (Excel, CNPV 2018) | DANE | Habitantes de cada municipio en el año en curso, cabecera/rural, y población de 5 a 16 y de 5 a 18 años por edad simple | actualizado 30/07/2025 |
| Matrícula en preescolar, básica y media (`ngw5-c5nw`) | MinEducación | Estudiantes matriculados (SIMAT) por municipio y por sede, oficial y no oficial, urbano y rural | 2010 – 2025 |
| Docentes oficiales EPBM (`pgrh-8um9`) | MinEducación | Docentes del sector oficial por Entidad Territorial Certificada | hasta 2022 |
| DIVIPOLA municipios (`gdxc-w37w`) | DANE | Los 1.122 municipios con código oficial y coordenadas | vigente |
| DIVIPOLA centros poblados (`xaxy-8nri`) | DANE | 8.161 poblados con nombre propio y coordenadas | vigente |
| PIB departamental (`kgyi-qc7j`) | DANE | De qué vive cada departamento: 13 actividades económicas | 2005 – 2023 |
| PISA (`data/pisa_colombia.json`) | OCDE | Colombia frente al promedio OCDE en lectura, matemáticas y ciencias | ciclo 2025, publicado 08/09/2026 |

Todas son fuentes oficiales y públicas. Brújula no produce datos propios ni
estimaciones.

---

## Lo que Brújula no sabe

Una herramienta de datos vale por lo que reconoce que ignora. Estas limitaciones
están documentadas a propósito y se declaran en las respuestas:

- **Docentes por municipio o por colegio.** Lo único abierto son los docentes
  *oficiales* por Entidad Territorial Certificada (los 32 departamentos y unas 65
  ciudades). Si el municipio es una ETC —Soacha, Tumaco, las capitales— la cifra
  es suya; si no, es la del departamento entero y la ficha lo dice así. El MEN
  tenía una base por establecimiento (`fjw5-pzau`) que ya no es pública. Además
  los años 2015-2021 de la base por ETC vienen duplicados; solo 2022 es verosímil.
- **La población es proyección.** El DANE proyecta desde el Censo 2018 y revisa las
  cifras cada tanto: el «2026» de hoy no será el «2026» de dentro de dos años. Por
  eso cada cifra lleva el año y la fecha de actualización del DANE.
- **La matrícula es la reportada al SIMAT**, no un censo. Los colegios privados
  reportan menos y peor que los oficiales.
- **2023.** Los microdatos públicos terminan en 2022 y el archivo agregado de ese
  año está dañado en el servidor del ICFES. No hay resultados por colegio para 2023.
- **Por grado.** La cobertura viene por nivel (transición, primaria, secundaria,
  media), no grado por grado.
- **Brecha digital del hogar.** Los microdatos que indican si el estudiante tiene
  internet y computador en casa se detienen en 2022, aunque el rendimiento por
  colegio llegue hasta 2025.
- **PISA no habla de tu colegio.** Es una prueba muestral que caracteriza al país.
  Brújula no mezcla ese dato con los resultados institucionales.
- **PISA no tiene API.** La tabla se cura a mano desde el informe oficial y se
  actualiza cada tres años. Solo entran los ciclos cuyas cifras de Colombia *y*
  de la OCDE se verificaron contra fuente oficial: hoy son 2022 y 2025. Los
  anteriores quedan fuera en vez de completarse con estimaciones.
- **Nunca por estudiante.** Los microdatos son anónimos y solo se consultan
  agregados.
- **No hay veredas.** En datos.gov.co la palabra devuelve 249 resultados y todos
  son de un municipio suelto («Veredas de Chía», «Barrios y Veredas de Tauramena»).
  La capa nacional —unas 32.000 veredas— vive en el IGAC como archivo geográfico,
  no como API. Lo más fino con cobertura nacional son los centros poblados.
- **Las sedes no están georreferenciadas.** No existe dataset nacional. El que se
  llama «Coordenadas - Sedes Educativas» son 26 filas de un solo municipio, con las
  coordenadas escritas como texto: `4º30'19.06"N`.
- **No hay polígonos municipales** en el portal, así que el mapa es de puntos.
- **La economía municipal tampoco.** El PIB solo se publica por departamento;
  «valor agregado municipal» devuelve un único resultado y es de Caldas. Lo
  municipal está en TerriData del DNP, que es descarga de archivo.
- **SECOP no dice dónde se gastó.** El dataset registra el municipio de la
  *entidad que contrata*, no el lugar de ejecución. Un contrato firmado en Bogotá
  para obras en Leticia aparece en Bogotá. Además el filtro por objeto es
  textual: atrapa "educación" y "escolar", se le escapa infraestructura escolar
  que no use esas palabras. La cifra se enuncia como *"contratos de entidades con
  sede en X cuyo objeto menciona educación"*, nunca como *"inversión educativa en X"*.

---

## Fichas: la capa que responde

`construir_fichas.py` convierte los parquet crudos en dos tablas —una por
municipio, una por sede— que ya traen la respuesta hecha. El agente no agrega
millones de filas cada vez que alguien pregunta por Soacha: busca por clave.

Eso resuelve tres cosas a la vez. El costo por pregunta cae a casi nada, la
latencia a milisegundos, y —lo que más pesa para un diagnóstico— la misma
pregunta da la misma cifra hoy y en tres meses. Un número que cambia entre dos
reuniones no sustenta nada.

```bash
python construir_fichas.py --datos ./data --salida ./data
```

Produce `fichas_municipio.parquet`, `fichas_sede.parquet` y
`fichas_metadatos.json`, este último con la fecha de corte, las fuentes que
estaban presentes en la corrida y los umbrales con los que se marcó cada señal.

### Señales

Una ficha no es un volcado de indicadores: es el material de un diagnóstico
previo. Encima de las cifras lleva **señales**, que marcan dónde el dato público
y lo que suele afirmarse no coinciden.

| Señal | Qué marca |
|---|---|
| `desercion_cero_con_cobertura_baja` | Deserción reportada en 0 % con cobertura neta bajo 80 %. Casi siempre es un vacío de reporte, no un logro |
| `sin_reporte_men_reciente` | El municipio lleva tres años o más sin actualizar sus indicadores |
| `cobertura_neta_baja` · `desercion_alta` | Umbrales de trabajo de la fundación |
| `brecha_digital_alta` | Menos del 40 % de los estudiantes declara internet en casa |
| `revisar_contraste_recursos_resultados` | Contratación por menor de edad escolar por encima de la mediana de su departamento **y** resultados en el cuartil más bajo del mismo departamento |
| `caida_sostenida` (sede) | Cinco puntos o más de caída en matemáticas a lo largo de tres periodos |
| `por_debajo_de_su_departamento` (sede) | Cinco puntos o más bajo la mediana departamental |
| `muestra_insuficiente` (sede) | Menos de diez evaluados: no admite promedio publicable |

**Las señales indican dónde revisar. No son hallazgos y no acusan a nadie.**
`revisar_contraste_recursos_resultados` en particular no prueba desvío de
recursos: SECOP ni siquiera dice dónde se ejecutó el gasto. Dice que ahí vale la
pena pedir documentos.

Los umbrales son convenciones de trabajo, no normas oficiales, y por eso quedan
escritos en `fichas_metadatos.json` junto a los datos: quien lea una ficha dentro
de un año tiene que poder saber con qué regla se marcó, sin abrir el código.

### Cómo se compara

Siempre dentro del departamento, nunca contra el país. Comparar un colegio de
Guainía con el promedio nacional no informa: lo compara contra Bogotá. Y el
percentil departamental se deja nulo cuando hay menos de cinco municipios con
dato, porque con cuatro es aritmética sin significado.

---

## Estado conocido de las fuentes

### Archivos del ICFES dañados en el origen

Ocho de los dieciséis archivos de resultados agregados que publica el ICFES se
descargan con un tamaño de **exactamente 1.048.576 bytes (1 MiB)** y no se pueden
descomprimir. El servidor reporta ese tamaño como el real, de modo que no es un
problema de descarga: la carga del archivo al portal quedó truncada.

Periodos afectados: **2012, 2016-2, 2017-2, 2018, 2019-1, 2019-2, 2020-4 y 2021-4.**

Lo grave es cuáles: los de calendario A y segundo semestre, que son los que traen
la cobertura nacional de ~14.000 sedes. Los que sí abren de 2015 a 2022 son de
calendario B, con unos 350 establecimientos.

`ingest_icfes.py` detecta estos archivos y los omite con una advertencia. La serie
histórica se reconstruye desde los microdatos de datos.gov.co.

*Reportado al ICFES en septiembre de 2026.*

## Alcance: cinco años, y por qué cada fuente lo aplica distinto

**Archivos agregados del ICFES → 2021 en adelante.** El recorte sale casi gratis:
de los nueve periodos que quedan fuera, **siete venían dañados en el servidor del
ICFES**. Los dos que sí abrían son de calendario B, con ~350 establecimientos: no
dan cobertura nacional. Quedan seis periodos usables con ~14.000 sedes cada uno,
que es lo que alimenta la tendencia por colegio.

**Microdatos → solo cinco periodos (2019-2022).** No por antigüedad, sino por lo
que aportan. El resultado por sede de estos años ya viene en los archivos
agregados; reconstruirlo desde los microdatos es repetir trabajo con más latencia.
Lo que **solo** está en los microdatos es la conectividad declarada del hogar, y
eso se detiene en 2022. Se conservan los dos censos nacionales grandes —2019-4 con
1.096.524 registros y 2022-4 con 1.065.888— porque dan el antes y el después de la
pandemia sobre la brecha digital. 2019-4 se sale del corte de cinco años a
propósito: sin él no hay con qué comparar 2022.

Los periodos retirados quedan escritos uno por uno con su razón, en
`ARCHIVOS_FUERA_DE_ALCANCE` y `PERIODOS_EXCLUIDOS`, y la ingesta se detiene si
alguien pide uno. Están así para que nadie los reponga creyendo que fueron un
descuido.

---

## Computadores Para Educar tiene tres trampas

Es el registro de *«esto ya te lo dieron»*, que es justo lo que hace falta antes
de prometerle equipos a una institución. Pero leerlo de frente produce cifras
absurdas. Verificado contando valores distintos por año sobre los 1.121
municipios:

**1. Tres columnas son cifras nacionales repetidas en cada fila.**
`meta_terminales_entregadas`, `meta_docentes_formados` y —la más traicionera—
`sedes_beneficiadas` tienen **un solo valor distinto en todo el país** para cada
año. Un municipio de Santander aparece en 2012 con 155 computadores entregados y,
al lado, «3.889 sedes beneficiadas». No son suyas: son del programa entero.
Publicarlas por municipio diría que los 1.121 recibieron lo mismo que el país.

**2. `ni_os_por_terminal` es departamental, no municipal.** Tiene entre 33 y 35
valores distintos por año: uno por departamento. El municipal es
`ni_os_por_terminal_municipal`. Los nombres invitan al error exacto, así que la
ingesta los renombra para que el nombre diga lo que el dato es.

**3. Desde 2020 hay una fila por mes, no por año.** Hasta 2019 son 1.121 filas
anuales; en 2021 son 14.581 — cortes mensuales acumulados, más una fila basura con
`fecha_corte` 1900-01-01. Sumarlas multiplicaría lo entregado por doce.

Y una advertencia que va pegada a toda respuesta: **es historia, no presente.** El
programa entrega a más de 1.000 municipios al año hasta 2015, a 354 en 2017, a 65
en 2019, y el dataset deja de actualizarse en febrero de 2023. Dice qué se entregó
ya, no cuántos equipos hay hoy ni en qué estado.

---

### La reforma de 2014 parte la serie en dos

El dataset de microdatos trae 23 periodos desde 2010, pero **solo se ingieren los
posteriores a 2014-2**, y no por comodidad: antes de esa fecha era otra prueba.

| periodo | lectura | matemáticas | c. naturales | inglés | global |
|---|---|---|---|---|---|
| 2011-1 | — | 60.14 | — | 79.55 | — |
| 2012-2 | — | 45 | — | 48 | — |
| 2014-1 | — | 55 | — | 75 | — |
| **2014-2** | **35** | 45 | **43** | 42 | **212** |
| 2019-4 | 69 | 66 | 65 | 71 | 339 |

Hasta 2014-1 no existen `punt_lectura_critica` ni `punt_global`: vienen nulos
porque esas áreas no se evaluaban así. Y los puntajes de 2010 y 2011 son
decimales sobre otra escala —de ahí los `"35,2"` que revientan el cast a número.

Ingerirlos no daría un error: daría una **serie falsa**. Un municipio mostraría
una "evolución" de 2010 a 2022 cuyo primer tramo mide otra cosa. Para una
herramienta que existe para sustentar diagnósticos, inventar una tendencia es
peor que no tenerla.

`PERIODOS_EXCLUIDOS` deja escrito cada periodo omitido con su razón, y la ingesta
se detiene si alguien pide uno de ellos. Está así para que nadie los "recupere"
dentro de seis meses creyendo que fueron un olvido.

### Trampas de tipado

- Los puntajes de `kgxf-xxbe` están almacenados como **texto**, no como número.
  Toda agregación necesita `avg(punt_global::number)` o la consulta falla con
  `query.soql.type-mismatch`.
- Los valores numéricos de Computadores Para Educar usan **coma decimal**
  (`"2,00"`, `",00"`). Hay que normalizarlos antes de promediar.
- Los códigos DANE se tratan siempre como **texto**. Leídos como número pierden
  los ceros a la izquierda y dejan de cruzar con los datos del MEN.
- Las coordenadas de DIVIPOLA vienen con **coma decimal** (`"-75,581775"`). Leídas
  directo quedan nulas y el punto desaparece del mapa sin ningún error.
- El PIB trae los departamentos en Mayúscula Inicial («Cundinamarca») mientras el
  MEN los trae en mayúscula sostenida («CUNDINAMARCA»), y **el `LIKE` de Socrata
  distingue mayúsculas**: filtrar por nombre devuelve cero filas sin avisar.
- Los códigos de departamento del PIB van **sin cero a la izquierda** (`"5"`).
- SECOP guarda el municipio **por nombre, no por código DANE**, y lo escribe
  distinto que el MEN: 86 municipios difieren en tildes o mayúsculas, y 74 tienen
  más de una grafía dentro del propio SECOP. Bogotá aparece como `Bogotá` y como
  `Bogotá D.C.`, mientras el MEN la llama `Bogotá, D.C.`. Por eso `ingest_secop.py`
  pide el vocabulario de SECOP al arrancar en vez de adivinar la ortografía ajena.
- Socrata devuelve **1.000 filas si no se pasa `$limit`**, sin avisar de que hay más.
- **El MEN publica el mismo municipio con dos códigos distintos.** Abejorral
  aparece como `"5002"` y como `"05002"` en el mismo dataset, según el año: 149
  de sus 1.272 códigos vienen sin el cero a la izquierda. Ninguna otra fuente
  tiene el problema —DIVIPOLA, CPE y Saber 11 usan los cinco dígitos—, y el MEN
  es justo el que define el universo de municipios y sirve de llave para todo lo
  demás. Sin normalizar, esos 149 municipios (**el 13 % del país**) se parten en
  dos fichas con la mitad de los años cada una, y la variante sin rellenar no
  cruza con coordenadas, ni con Saber 11, ni con CPE. No falla con un error:
  entrega el municipio a medias y se ve como si el dato no existiera. Se rellena
  en la ingesta, al construir las fichas **y** al levantar el servidor: son tres
  puntos distintos donde puede entrar un parquet de otra corrida.

---

## Estructura

```
ingest_icfes.py       Descarga y normaliza los agregados del ICFES
ingest_datos_gov.py   MEN municipal, Computadores Para Educar y microdatos
ingest_secop.py       Contratación pública con objeto educativo, por municipio
ingest_territorio.py  Ubicación de municipios y poblados, y economía departamental
ingest_poblacion.py   Habitantes y población en edad escolar por municipio (DANE, Excel)
ingest_matricula.py   Matrícula por municipio y por sede, y docentes por ETC (MEN)
construir_fichas.py   Precalcula las fichas por municipio y por sede
server.py             Servidor MCP con las herramientas de consulta
probar_server.py      Pruebas del servidor, sin tocar la red
web/                  Front público
```

## Ejecutar la ingesta

```bash
pip install pandas requests openpyxl xlrd pyarrow
python ingest_icfes.py --salida ./data
python ingest_datos_gov.py --salida ./data
python ingest_secop.py --salida ./data
python ingest_territorio.py --salida ./data
python ingest_poblacion.py --salida ./data
python ingest_matricula.py --salida ./data
python construir_fichas.py --datos ./data --salida ./data
```

El script cachea las descargas en `.cache_icfes/`, procesa cada periodo de forma
independiente —un archivo dañado no tumba la corrida— e imprime un reporte con el
estado de cada uno antes de escribir `data/saber11_agregado.parquet`.

---

## PISA 2025

Publicado el 8 de septiembre de 2026. Colombia quedó por debajo del promedio de
la OCDE en las tres áreas:

| Área | Colombia 2022 | Colombia 2025 | OCDE 2025 | Brecha |
|---|---|---|---|---|
| Lectura | 409 | **399** | 461 | −62 |
| Matemáticas | 383 | **381** | 463 | −82 |
| Ciencias | 411 | **414** | 482 | −68 |

Una lectura honesta del ciclo necesita un dato más: **el promedio de la OCDE
también cayó**, unos 14 puntos en lectura y 9 en matemáticas, y es el más bajo
registrado hasta ahora. Parte del acortamiento de la brecha no es mejora de
Colombia. Ciencias es el único dominio donde la distancia se redujo por ambos
lados: Colombia subió 3 puntos y la OCDE bajó 3.

Ese matiz va dentro del archivo, en `contexto`, para que el agente lo diga al
presentar la comparación en vez de celebrar una brecha más corta.

---

## El agente

`server.py` expone las fichas como herramientas MCP. Lo consulta un agente
alojado en Microsoft Foundry; el front solo dibuja lo que el servidor le pasa.

### Toda consulta ocurre dentro de un territorio

No hay forma de preguntar "por el país" por la puerta principal. Quien consulta
elige departamento y municipio, y el agente trabaja ahí. Tres razones, en orden
de peso:

- Un diagnóstico es territorial. *"¿Cómo está la educación en Colombia?"* no tiene
  respuesta útil; *"¿cómo está Soacha frente a Cundinamarca?"* sí.
- Comparar contra el promedio nacional es comparar contra Bogotá.
- Una consulta acotada es barata y predecible, que es lo que permite mantener
  esto abierto sin que el costo dependa de cuánta gente pregunte.

`ranking_nacional` existe, pero exige un token que el proxy inyecta solo para las
organizaciones con acceso completo. Sin token responde que no y ofrece trabajar
por departamento; el agente no puede adivinarlo y no lo pide al usuario.

### Herramientas

| Herramienta | Para qué |
|---|---|
| `ubicar_lugar` | **¿Dónde queda esto?** Un nombre → municipio, departamento y mapa |
| `listar_departamentos` · `listar_municipios` | El selector de territorio, paso 1 y paso 2 |
| `ficha_municipio` | El diagnóstico completo de un municipio |
| `colegios_del_municipio` | Sus sedes con el último Saber 11 |
| `buscar_colegio` · `ficha_colegio` | Una sede por nombre y luego en detalle |
| `evolucion_municipio` | Serie histórica de un indicador del MEN |
| `senales_departamento` | **Focalización**: dónde empezar dentro de un departamento |
| `contratos_municipio` | Los mayores contratos, con enlace al expediente en SECOP |
| `comparar_ocde` | Colombia frente a la OCDE en PISA |
| `estado_de_los_datos` | Qué está cargado, con qué corte y con qué umbrales |
| `ranking_nacional` | Todo el país. Requiere acceso completo |

### Para quien no conoce Colombia

Todo lo anterior da por hecho que quien pregunta sabe dónde queda el sitio.
`ubicar_lugar` cierra ese hueco: busca entre los 1.122 municipios y los 8.161
centros poblados, y responde en qué municipio y departamento está, con un mapa.

La ficha municipal, además de las cifras educativas, cuenta **dónde queda** (a
cuántos kilómetros de la capital de su departamento y de Bogotá), **qué tan
disperso es** (cuántos poblados tiene fuera de la cabecera, qué porcentaje de sus
sedes es rural) y **de qué vive la región** (las tres actividades que más pesan en
el PIB de su departamento).

Tres precisiones que van pegadas a cada respuesta:

- **El mapa ubica, no mide.** No existe ningún indicador educativo a nivel de
  vereda ni de centro poblado. Sin esa advertencia, un mapa con puntos se lee como
  si tuviéramos cifras por vereda.
- **Las distancias son en línea recta.** Con tres cordilleras de por medio, la
  ruta real puede ser el triple. Nunca se presenta como tiempo de viaje.
- **El PIB es del departamento.** Atribuírselo al municipio sería inventar, así que
  la respuesta dice «Departamento de X» de forma explícita.

Y una salvaguarda que importa más de lo que parece: **los nombres se repiten
muchísimo.** «Pueblo Nuevo» existe en 41 municipios de 19 departamentos. Cada
lugar del índice lleva cuántos homónimos tiene, y con más de uno el agente está
obligado a preguntar. Mandar un diagnóstico al municipio equivocado es peor que no
responder.

### Lo que el servidor garantiza

- Una sede con menos de diez evaluados **no entrega promedio**. Entrega una
  explicación de por qué no: la cifra sería inestable y podría identificar
  estudiantes. La sede sí aparece en los listados —que un colegio sea pequeño es
  información—, solo que sin número.
- Un año sin reporte sale **nulo, no cero**. Un hueco en la serie es un dato en sí
  mismo: dice que el municipio dejó de reportar.
- Toda respuesta trae `fuente` y `corte`.
- El agente **no dibuja gráficas**. Cada herramienta devuelve un bloque `vis` que
  elige un componente de un catálogo cerrado y le pasa los datos; el front tiene
  esos componentes prefabricados. Así no se ejecuta código generado por IA en un
  sitio público y todas las respuestas se ven igual de bien.
- Las advertencias viajan **dentro** del bloque `vis`, no aparte. Una gráfica
  esconde las salvedades mejor que un párrafo.
- Un municipio que no existe no se reemplaza por el más parecido: se dice que no
  se encontró. Uno ambiguo devuelve las opciones para que el usuario elija.

### Pruebas

```bash
python probar_server.py
```

Genera un territorio sintético —dos departamentos, cuarenta municipios,
doscientas cuarenta sedes, con huecos de reporte y sedes demasiado pequeñas a
propósito—, construye las fichas y ejerce las herramientas. **No toca la red**, y
es deliberado: si dependieran de las fuentes públicas, una caída del ICFES un
martes cualquiera parecería un error nuestro.

Lo que verifica no es que el código corra, sino que cumpla lo que promete: que no
se pueda consultar sin territorio, que la muestra pequeña no publique promedio,
que los huecos no se vuelvan ceros, que las comparaciones sean departamentales y
que la consulta nacional esté cerrada sin token.

---

## Licencia

Código bajo licencia MIT (ver `LICENSE`).

Los datos consultados pertenecen a sus entidades productoras (ICFES,
Ministerio de Educación Nacional, MinTIC, OCDE) y se rigen por las licencias de
sus respectivos portales de datos abiertos. Brújula no los redistribuye: los
consulta y los cita.

## Contacto

Fundación Startin — [hola@startin.org.co](mailto:hola@startin.org.co) · [startin.org.co](https://startin.org.co)
