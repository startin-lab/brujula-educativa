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
| Resultados agregados por establecimiento | ICFES | Promedio, desviación y evaluados por área, sede por sede | 2024 – 2025 |
| Resultados únicos Saber 11 (`kgxf-xxbe`) | ICFES | Microdatos por estudiante; incluye internet y computador en el hogar | 2010 – 2022 |
| Estadísticas en educación por municipio (`nudc-7mev`) | MinEducación | 41 indicadores de cobertura, deserción, aprobación y repitencia por nivel | 2011 – 2024 |
| Computadores Para Educar (`pyqj-s96k`) | MinTIC | Niños por terminal, equipos y tabletas entregados, docentes formados | hasta 2023 |
| Indicadores de infraestructura (`3ncw-3qwq`) | MinEducación | Aulas nuevas y mejoradas por sede | hasta 2021 |
| SECOP Integrado (`rpmr-utcd`) | Colombia Compra Eficiente | Contratos cuyo objeto menciona educación, por municipio de la entidad | 2026-09 |
| DIVIPOLA municipios (`gdxc-w37w`) | DANE | Los 1.122 municipios con código oficial y coordenadas | vigente |
| DIVIPOLA centros poblados (`xaxy-8nri`) | DANE | 8.161 poblados con nombre propio y coordenadas | vigente |
| PIB departamental (`kgyi-qc7j`) | DANE | De qué vive cada departamento: 13 actividades económicas | 2005 – 2023 |
| PISA | OCDE | Colombia frente al promedio OCDE en lectura, matemáticas y ciencias | ciclo 2025 |

Todas son fuentes oficiales y públicas. Brújula no produce datos propios ni
estimaciones.

---

## Lo que Brújula no sabe

Una herramienta de datos vale por lo que reconoce que ignora. Estas limitaciones
están documentadas a propósito y se declaran en las respuestas:

- **Docentes.** No existe un dataset nacional abierto de planta docente de básica
  y media. Lo único disponible es el conteo de docentes *formados* dentro de
  Computadores Para Educar.
- **2023.** Los microdatos públicos terminan en 2022 y el archivo agregado de ese
  año está dañado en el servidor del ICFES. No hay resultados por colegio para 2023.
- **Por grado.** La cobertura viene por nivel (transición, primaria, secundaria,
  media), no grado por grado.
- **Brecha digital del hogar.** Los microdatos que indican si el estudiante tiene
  internet y computador en casa se detienen en 2022, aunque el rendimiento por
  colegio llegue hasta 2025.
- **PISA no habla de tu colegio.** Es una prueba muestral que caracteriza al país.
  Brújula no mezcla ese dato con los resultados institucionales.
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

---

## Estructura

```
ingest_icfes.py       Descarga y normaliza los agregados del ICFES
ingest_datos_gov.py   MEN municipal, Computadores Para Educar y microdatos
ingest_secop.py       Contratación pública con objeto educativo, por municipio
ingest_territorio.py  Ubicación de municipios y poblados, y economía departamental
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
python construir_fichas.py --datos ./data --salida ./data
```

El script cachea las descargas en `.cache_icfes/`, procesa cada periodo de forma
independiente —un archivo dañado no tumba la corrida— e imprime un reporte con el
estado de cada uno antes de escribir `data/saber11_agregado.parquet`.

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
