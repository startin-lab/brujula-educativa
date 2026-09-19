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

---

## Estructura

```
ingest_icfes.py       Descarga y normaliza los agregados del ICFES
ingest_datos_gov.py   MEN municipal, Computadores Para Educar y microdatos
mcp/                  Servidor MCP con las herramientas de consulta
web/                  Front público
```

## Ejecutar la ingesta

```bash
pip install pandas requests openpyxl xlrd pyarrow
python ingest_icfes.py --salida ./data
```

El script cachea las descargas en `.cache_icfes/`, procesa cada periodo de forma
independiente —un archivo dañado no tumba la corrida— e imprime un reporte con el
estado de cada uno antes de escribir `data/saber11_agregado.parquet`.

---

## Licencia

Código bajo licencia MIT (ver `LICENSE`).

Los datos consultados pertenecen a sus entidades productoras (ICFES,
Ministerio de Educación Nacional, MinTIC, OCDE) y se rigen por las licencias de
sus respectivos portales de datos abiertos. Brújula no los redistribuye: los
consulta y los cita.

## Contacto

Fundación Startin — [hola@startin.org.co](mailto:hola@startin.org.co) · [startin.org.co](https://startin.org.co)
