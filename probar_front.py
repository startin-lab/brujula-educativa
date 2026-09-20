#!/usr/bin/env python3
"""
Brújula Educativa — Prueba de humo de la página en un navegador real
Fundación Startin

POR QUÉ EXISTE

  La noche del 19 de septiembre de 2026 se publicó un consultar.html que
  pasaba la comprobación de sintaxis y estaba roto: una línea usaba la
  constante API antes de declararla, el navegador lanzaba ReferenceError al
  arrancar y la página quedaba con el desplegable de departamentos vacío.
  `node --check` no ve eso; solo un navegador ejecutando la página lo ve.

  Esta prueba abre web/consultar.html e index.html en Chromium sin cabeza,
  con el servicio simulado dentro de la propia página, y verifica lo que un
  usuario vería: que no hay errores de JavaScript, que los desplegables se
  llenan, que una respuesta con markdown se formatea, que el pie de «datos de
  muestra» aparece solo cuando no hay servicio, y que el mapa no rotula de
  más. Tarda unos segundos y no necesita red.

      python3 probar_front.py
"""
import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"

# Un servicio de mentiras: lo que el orquestador contestaría a cada ruta.
TERRITORIOS = {"Cundinamarca": ["Soacha", "Fusagasugá"], "Nariño": ["San Andres de Tumaco", "Pasto"]}
FICHA = {
    "encontrado": True, "municipio": "San Andres de Tumaco", "departamento": "Nariño", "corte": "2026-09-20",
    "ubicacion": {"lat": 1.8, "lon": -78.8, "tipo": "Municipio", "capital_del_departamento": "Pasto",
                  "km_a_la_capital_departamental": 178, "km_a_bogota": 620, "sedes_total": 100,
                  "sedes_rurales": 51, "pct_sedes_rurales": 51.0,
                  "capitales_cercanas": [
                      {"ciudad": "Pasto", "departamento": "Nariño", "lat": 1.21, "lon": -77.28, "km": 178.0},
                      {"ciudad": "Popayán", "departamento": "Cauca", "lat": 2.44, "lon": -76.61, "km": 252.0},
                      {"ciudad": "Cali", "departamento": "Valle del Cauca", "lat": 3.45, "lon": -76.53, "km": 314.0}],
                  "advertencia": "Distancia en línea recta, no por carretera."},
    "indicadores": {"anio": 2024, "cobertura_neta": 66.1, "poblacion_5_16": 60491},
    "poblacion": {"anio": 2026, "habitantes": 265312, "de_5_a_18": 71400, "pct_de_5_a_18": 26.9,
                  "fuente": "DANE — proyecciones de población 2018-2042 (CNPV 2018)"},
    "matricula": {"anio": 2025, "estudiantes": 58210, "oficial": 52389, "no_oficial": 5821},
    "docentes": {"anio": 2022, "entidad_territorial_certificada": "Tumaco", "la_etc_es_este_municipio": True,
                 "docentes_oficiales": 2310, "estudiantes_oficiales_por_docente": 22.7},
    "saber11": {"sedes_evaluadas": 51},
    "economia_del_departamento": {"ambito": "Departamento de Nariño", "anio": 2023,
        "actividades_principales": [
            {"actividad": "Administración pública y defensa; planes de seguridad social de afiliación obligatoria; educación; actividades de atención de la salud humana y de servicios sociales", "pct_del_pib": 27.0},
            {"actividad": "Agricultura, ganadería, caza, silvicultura y pesca", "pct_del_pib": 14.2}],
        "advertencia": "El PIB solo se publica por departamento."},
    "senales": [{"clave": "cobertura_neta_baja"}, {"clave": "brecha_digital_alta"}],
}
COLEGIOS = {"encontrado": True, "sedes": [
    {"cod": "152835000011", "nombre": "IE Ciudadela Tumac", "naturaleza": "OFICIAL", "zona": "URBANO", "evaluados": 120},
    {"cod": "152835000022", "nombre": "IE Robert Mario Bischoff", "naturaleza": "OFICIAL", "zona": "URBANO", "evaluados": 88},
]}
RESPUESTA = {
    "respuesta": "En **Tumaco** la cobertura es baja:\n\n- **Neta:** **66,1 %**\n- **Bruta:** 81,2 %\n\n"
                 "**Señales**\n1. Cobertura neta baja.\n2. Brecha digital alta.",
    "vis": [
        {"tipo": "cifras", "titulo": "Tumaco", "cifras": [{"etiqueta": "Cobertura neta", "valor": 66.1, "unidad": "%"}]},
        {"tipo": "mapa", "titulo": "Dónde queda", "puntos":
            [{"nombre": "Tumaco", "lat": 1.8, "lon": -78.8, "destacado": True}] +
            [{"nombre": f"Vereda {i}", "lat": 1.8 + i / 100, "lon": -78.8 + i / 100, "destacado": False} for i in range(30)]},
        {"tipo": "senales", "titulo": "Qué conviene revisar",
         "items": [{"clave": "cobertura_neta_baja", "explicacion": "Por debajo del 80 %."}]},
    ],
    "advertencias": ["Indicadores con corte 2024."], "corte": "2026-09-20",
    "registrado": False, "restantes": 9, "desde_cache": False, "vueltas": 2,
}


class _Silencioso(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass


def servir_web():
    """
    web/ por HTTP en un puerto libre. Abrir los HTML como file:// no sirve: el
    navegador no deja hacer fetch() de archivos locales, y la página necesita
    pedir el mapa. Así también se prueba tal como se publica.
    """
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Silencioso, directory=str(WEB)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main() -> int:
    from playwright.sync_api import sync_playwright
    servidor, base = servir_web()

    fallos: list[str] = []

    def ok(cond: bool, texto: str) -> None:
        print(f"  {'OK   ' if cond else 'FALLA'} {texto}")
        if not cond:
            fallos.append(texto)

    with sync_playwright() as pw:
        navegador = pw.chromium.launch()
        pagina = navegador.new_page()
        errores: list[str] = []
        pagina.on("pageerror", lambda e: errores.append(str(e)))
        # Solo excepciones de la página. Un recurso externo que no carga —las
        # fuentes de Google desde una red cerrada, por ejemplo— no es un error
        # de nuestro código y no debe tumbar la prueba.
        pagina.on("console", lambda m: errores.append(m.text)
                  if m.type == "error" and "Failed to load resource" not in m.text else None)

        # El servicio simulado responde a lo que la página pida.
        API = "https://acceso.brujula.startinlab.org"
        enviados: list[dict] = []
        def ruta(route, request):
            url = request.url
            if url.startswith(f"{API}/preguntar"):
                try: enviados.append(json.loads(request.post_data or "{}"))
                except Exception: pass
            cuerpo = None
            if url.startswith(f"{API}/territorios"): cuerpo = TERRITORIOS
            elif url.startswith(f"{API}/estado"):    cuerpo = {"registrado": False, "restantes": 10}
            elif url.startswith(f"{API}/preguntar"): cuerpo = RESPUESTA
            elif url.startswith(f"{API}/ficha"):     cuerpo = FICHA
            elif url.startswith(f"{API}/colegios"):  cuerpo = COLEGIOS
            if cuerpo is None:
                return route.fulfill(status=404, body="")
            route.fulfill(status=200, content_type="application/json",
                          headers={"access-control-allow-origin": "*"},
                          body=json.dumps(cuerpo, ensure_ascii=False))
        pagina.route(f"{API}/**", ruta)

        print("\n== 1. consultar.html arranca sin errores ==")
        pagina.goto(f"{base}/consultar.html")
        pagina.wait_for_timeout(800)
        ok(not errores, "sin errores de JavaScript al cargar" + (f" → {errores[0][:90]}" if errores else ""))

        print("\n== 2. Los desplegables se llenan con la lista del servicio ==")
        n = pagina.eval_on_selector("#sel-depto", "e => e.options.length - 1")
        ok(n == len(TERRITORIOS), f"departamentos: {n}")
        ok(pagina.eval_on_selector("#sel-depto", "e => e.value") == "Cundinamarca",
           "conserva el departamento preferido aunque cambie el formato del nombre")
        ok(pagina.eval_on_selector("#sel-mun", "e => e.value") == "Soacha", "y el municipio")

        print("\n== 3. El pie de «datos de muestra» solo sin servicio ==")
        ok(pagina.eval_on_selector("#aviso-muestra", "e => e.hidden") is True,
           "con servicio configurado, el aviso está oculto")

        print("\n== 4. Al elegir municipio aparece la ficha inicial, sin gastar cupo ==")
        pagina.select_option("#sel-depto", "Nariño")
        pagina.select_option("#sel-mun", "San Andres de Tumaco")
        pagina.wait_for_selector("#inicio .datos", timeout=5000)
        pagina.wait_for_selector("#inicio svg", timeout=5000)
        inicio = pagina.inner_text("#inicio")
        etiquetas = pagina.eval_on_selector_all("#inicio .dato .e", "els => els.map(e => e.textContent)")
        ok(etiquetas[0].startswith("Habitantes") and "DANE 2026" in etiquetas[0] and "265.312" in inicio,
           f"primero los habitantes, con año y fuente ({etiquetas[0]})")
        ok(etiquetas[1].startswith("Población de 5 a 18") and "71.400" in inicio and "26,9 %" in inicio,
           "luego la población de 5 a 18 con su porcentaje")
        ok(etiquetas[2].startswith("Estudiantes matriculados") and "58.210" in inicio and "90 % oficial" in inicio,
           "luego los estudiantes matriculados con el peso del sector oficial")
        ok(etiquetas[3].startswith("Docentes oficiales") and "2.310" in inicio and "23 estudiantes por docente" in inicio,
           "luego los docentes, con estudiantes por docente porque la ETC es el municipio")
        ok("60.491" not in inicio, "y la franja MEN de 5 a 16 ya no se repite cuando hay DANE")
        ok("178" in inicio and "Pasto" in inicio, "la distancia a la capital")
        ok("Popayán" not in inicio and "Cali" not in inicio, "sin lista de capitales cercanas: solo la propia y Bogotá")
        ok("620" in inicio and "Bogotá" in inicio, "la distancia a Bogotá")
        ok("Agropecuario y pesca" in inicio and "Gobierno, educación y salud" in inicio,
           "de qué vive el departamento, con las ramas del DANE en nombre corto")
        ok("afiliación obligatoria" not in inicio, "sin la denominación completa de la CIIU en pantalla")
        ok(pagina.eval_on_selector("#inicio .lista .fila b[title]", "e => e.title.includes('afiliación obligatoria')"),
           "pero la denominación exacta se conserva en el título")
        ok("2 señales" in inicio, "cuántas señales hay, sin decir cuáles todavía")
        ok(pagina.eval_on_selector_all("#inicio svg g.principal path.dpto", "e => e.length") == 32,
           "el mapa dibuja los 32 departamentos continentales")
        ok(pagina.eval_on_selector_all("#inicio svg g.principal path.dpto.elegido", "e => e.length") == 1,
           "y resalta el elegido")
        # Acercado al departamento: el elegido ocupa una parte grande del lienzo,
        # y un recuadro con el país entero dice dónde estamos.
        area = pagina.eval_on_selector("#inicio svg g.principal path.dpto.elegido",
            "e => { const b = e.getBBox(); return (b.width * b.height) / (300 * 340); }")
        ok(area > 0.08, f"el mapa está acercado al departamento (ocupa {area:.0%} del lienzo)")
        ok(pagina.eval_on_selector_all("#inicio svg .recuadro rect.marco", "e => e.length") == 1,
           "con el recuadro del país indicando la zona")
        ok(pagina.eval_on_selector_all("#inicio svg circle.mun", "e => e.length") == 1, "con el municipio marcado")
        ok(pagina.eval_on_selector_all("#inicio svg circle.cap", "e => e.length") == 1, "y la capital del departamento")
        ok("DANE" in inicio, "acredita la fuente de las siluetas")
        ok(pagina.inner_text("#chip-cupo").strip().lower() == "10 consultas libres", "la ficha no descontó cupo")
        ok(not errores, "sin errores de JavaScript al pintar la ficha" + (f" → {errores[0][:90]}" if errores else ""))

        print("\n== 4b. El tercer selector: un colegio, o todos ==")
        pagina.wait_for_selector("#campo-sede:not([hidden])", timeout=5000)
        ok(pagina.eval_on_selector("#sel-sede", "e => e.options.length") == 3,
           "lista los colegios con Saber 11 más la opción «todos»")
        ok("primaria" in pagina.inner_text("#nota-sede").lower(), "avisa que los de solo primaria no aparecen")
        chips_mun = pagina.inner_text("#preguntas")
        ok("qué dato lo sustenta" in chips_mun and "NO se puede concluir" in chips_mun,
           "las preguntas del municipio piden diagnóstico, no descripción")
        pagina.select_option("#sel-sede", "IE Ciudadela Tumac")
        ok("ciudadela" in pagina.inner_text("#chip-alcance").lower(), "el chip del ámbito muestra la sede")
        chips_sede = pagina.inner_text("#preguntas")
        ok("esta sede" in chips_sede and "qué dato lo sustenta" not in chips_sede,
           "con sede elegida las preguntas cambian a la sede")
        pagina.select_option("#sel-sede", "")
        ok("qué dato lo sustenta" in pagina.inner_text("#preguntas"), "y vuelven al elegir «todos»")

        print("\n== 4c. Municipio grande: el desplegable se vuelve buscador ==")
        MUCHOS = {"encontrado": True, "total": 120, "sedes": [
            {"cod": f"1110010{i:05d}", "nombre": f"Colegio {'Bilingüe del Pacífico' if i == 77 else 'Distrital'} {i}",
             "naturaleza": "OFICIAL", "zona": "URBANO", "evaluados": 100, "matricula": 900 + i}
            for i in range(120)]}
        def ruta_grande(route, request):
            if request.url.startswith(f"{API}/colegios"):
                return route.fulfill(status=200, content_type="application/json",
                    headers={"access-control-allow-origin": "*"}, body=json.dumps(MUCHOS, ensure_ascii=False))
            return ruta(route, request)
        pagina.unroute(f"{API}/**"); pagina.route(f"{API}/**", ruta_grande)
        pagina.select_option("#sel-mun", "Pasto")
        pagina.wait_for_selector("#buscador-sede:not([hidden])", timeout=5000)
        ok(pagina.eval_on_selector("#sel-sede", "e => e.hidden") is True, "con 120 colegios el desplegable se esconde")
        ok(pagina.eval_on_selector("#sel-sede", "e => e.options.length") == 121, "pero guarda las 120 opciones")
        ok("escribe parte del nombre" in pagina.inner_text("#nota-sede").lower(), "y la nota explica cómo buscar")
        pagina.fill("#buscar-sede", "pacifico bilingue")
        pagina.wait_for_selector("#lista-sedes li", timeout=3000)
        items = pagina.eval_on_selector_all("#lista-sedes li[data-i]", "els => els.map(e => e.textContent)")
        ok(len(items) == 1 and "Bilingüe del Pacífico" in items[0], f"busca sin tildes y en cualquier orden ({items})")
        ok("977 estudiantes" in items[0], "y muestra la matrícula de cada resultado")
        pagina.keyboard.press("Enter")
        ok(pagina.eval_on_selector("#sel-sede", "e => e.value") == "Colegio Bilingüe del Pacífico 77",
           "Enter elige el colegio y lo deja en el selector")
        ok("bilingüe" in pagina.inner_text("#chip-alcance").lower(), "el chip del ámbito lo refleja")
        ok(pagina.eval_on_selector("#sede-elegida", "e => !e.hidden") and "Pacífico" in pagina.inner_text("#sede-elegida"),
           "y se ve cuál quedó elegido")
        pagina.fill("#buscar-sede", "zzzz")
        pagina.wait_for_selector("#lista-sedes li.nada", timeout=3000)
        ok(True, "sin coincidencias lo dice en vez de quedarse en blanco")
        pagina.click("#quitar-sede")
        ok(pagina.eval_on_selector("#sel-sede", "e => e.value") == "" and pagina.eval_on_selector("#sede-elegida", "e => e.hidden"),
           "«Todos los colegios» vuelve al municipio entero")
        pagina.fill("#buscar-sede", "distrital 1")
        pagina.wait_for_selector("#lista-sedes li[data-i]", timeout=3000)
        pagina.click("#lista-sedes li[data-i]")
        ok(pagina.eval_on_selector("#sel-sede", "e => e.value").startswith("Colegio Distrital 1"), "el clic también elige")
        ok(not errores, "sin errores de JavaScript en el buscador" + (f" → {errores[0][:90]}" if errores else ""))
        pagina.unroute(f"{API}/**"); pagina.route(f"{API}/**", ruta)
        pagina.select_option("#sel-mun", "San Andres de Tumaco")
        pagina.wait_for_selector("#sel-sede:not([hidden])", timeout=5000)
        ok(pagina.eval_on_selector("#buscador-sede", "e => e.hidden") is True, "con pocos colegios vuelve el desplegable")

        print("\n== 5. Una pregunta se pinta bien ==")
        pagina.fill("#pregunta", "¿Cómo está la cobertura?")
        pagina.click("#enviar")
        pagina.wait_for_selector(".vis", timeout=5000)
        html = pagina.inner_html("#respuesta")
        ok("**" not in html, "los asteriscos del markdown no quedan a la vista")
        ok("<strong>Neta:</strong>" in html, "las negritas se convierten")
        ok(html.count("<li>") == 4, f"las viñetas y los números son listas ({html.count('<li>')} ítems)")
        ok("<script" not in html.lower(), "no se cuela HTML")
        chip = pagina.inner_text("#chip-cupo").strip()
        # inner_text devuelve el texto como se ve, y el chip va en mayúsculas por CSS.
        ok(chip.lower() == "9 consultas libres", f"el contador baja a 9 (dice «{chip}»)")
        # El «mapita» de puntos se retiró a petición del equipo: no aportaba
        # nada que la ficha inicial no muestre ya sobre el mapa del departamento.
        ok(pagina.eval_on_selector_all(".plano", "els => els.length") == 0,
           "el mapa de puntos «Dónde queda…» ya no se pinta en las respuestas")
        ok(pagina.eval_on_selector_all("#bloques .vis", "els => els.length") == 2,
           "las otras dos visualizaciones sí se pintan")
        ok("Cobertura neta baja" in pagina.inner_text("#bloques"), "la señal se muestra legible, no como identificador")
        ok(enviados and enviados[-1].get("sede") == "" and enviados[-1].get("cod_sede") == "",
           "sin colegio elegido, la consulta va sobre todo el municipio")
        pagina.select_option("#sel-sede", "IE Robert Mario Bischoff")
        pagina.fill("#pregunta", "¿cómo le va a esta sede?")
        pagina.click("#enviar")
        pagina.wait_for_timeout(600)
        ok(enviados[-1].get("sede") == "IE Robert Mario Bischoff" and enviados[-1].get("cod_sede") == "152835000022",
           "con colegio elegido, la consulta lleva la sede y su código DANE")
        pagina.select_option("#sel-sede", "")
        ok(not errores, "sin errores de JavaScript durante la consulta" + (f" → {errores[0][:90]}" if errores else ""))

        print("\n== 6. Al agotar el cupo, el registro aparece en la misma página ==")
        ok(pagina.eval_on_selector("#registro-en-linea", "e => e.hidden") is True,
           "con cupo disponible el formulario no se ve")
        estado_agotado = {"agotado": True}
        def ruta_agotado(route, request):
            url = request.url
            if url.startswith(f"{API}/preguntar"):
                return route.fulfill(status=402, content_type="application/json",
                    headers={"access-control-allow-origin": "*"},
                    body=json.dumps({"motivo": "Usaste tus 10 consultas libres."}))
            if url.startswith(f"{API}/registrar"):
                cuerpo = json.loads(request.post_data or "{}")
                estado_agotado["registro"] = cuerpo
                return route.fulfill(status=200, content_type="application/json",
                    headers={"access-control-allow-origin": "*"},
                    body=json.dumps({"motivo": "Listo. Te enviamos un enlace de acceso al correo."}))
            return ruta(route, request)
        pagina.unroute(f"{API}/**")
        pagina.route(f"{API}/**", ruta_agotado)
        pagina.fill("#pregunta", "¿y la deserción?")
        pagina.click("#enviar")
        pagina.wait_for_selector("#registro-en-linea:not([hidden])", timeout=5000)
        ok(True, "el 402 muestra el formulario aquí mismo, sin mandar a la portada")
        ok("registrate" in pagina.inner_text("#pie-consulta").lower().replace("í", "i"),
           "y el mensaje dice dónde está")
        pagina.click("#enviar-registro")
        pagina.wait_for_timeout(300)
        ok("autorizaci" in pagina.inner_text("#resultado").lower() or "revisa" in pagina.inner_text("#resultado").lower(),
           "sin autorización no envía, y lo dice")
        ok("registro" not in estado_agotado, "y no llamó al servidor")
        pagina.fill("#nombre", "Ana Ruiz"); pagina.fill("#organizacion", "Secretaría de Nariño")
        pagina.fill("#correo", "ana@narino.gov.co")
        pagina.fill("#proposito", "Diagnóstico de cobertura en la costa pacífica")
        pagina.check("#autoriza")
        pagina.click("#enviar-registro")
        pagina.wait_for_selector("#resultado.bien", timeout=5000)
        reg = estado_agotado.get("registro", {})
        ok(reg.get("correo") == "ana@narino.gov.co" and reg.get("autoriza") is True, "envía los datos y la autorización")
        ok(reg.get("politica", "").startswith("https://startin.org.co/privacidad"), "deja constancia de qué política se aceptó")
        ok("correo" in pagina.inner_text("#registro-titulo").lower(), "y le dice que revise el correo")
        ok(not errores, "sin errores de JavaScript" + (f" → {errores[0][:90]}" if errores else ""))

        print("\n== 7. index.html arranca y muestra el aviso de enlace caducado ==")
        errores.clear()
        pagina.goto(f"{base}/index.html?acceso=caducado")
        pagina.wait_for_timeout(500)
        ok(not errores, "sin errores de JavaScript")
        ok(pagina.eval_on_selector("#caducado", "e => !e.hidden"), "el aviso de enlace caducado se ve")

        print("\n== 8. El botón de tema fija claro u oscuro y lo recuerda ==")
        pagina.emulate_media(color_scheme="light")
        pagina.click("#tema")
        ok(pagina.evaluate("document.documentElement.dataset.theme") == "dark",
           "con el sistema en claro, el primer toque pone oscuro")
        fondo = pagina.evaluate("getComputedStyle(document.body).backgroundColor")
        ok(fondo == "rgb(7, 5, 31)", f"y el fondo cambia de verdad ({fondo})")
        pagina.goto(f"{base}/consultar.html")
        pagina.wait_for_timeout(300)
        ok(pagina.evaluate("document.documentElement.dataset.theme") == "dark",
           "la elección se conserva al pasar al agente")
        pagina.click("#tema")
        ok(pagina.evaluate("document.documentElement.dataset.theme") == "light", "segundo toque: claro")
        pagina.click("#tema")
        ok(pagina.evaluate("document.documentElement.dataset.theme || ''") == "", "tercer toque: automático")
        ok(not errores, "sin errores de JavaScript")

        print("\n== 9. Acceso interno: chip propio, enlace al panel y panel funcional ==")
        RESUMEN = {
            "generado": "2026-09-20T01:00:00+00:00",
            "registros": [
                {"ficha": "f1", "nombre": "Freddy Malaver", "organizacion": "Fundación Startin",
                 "correo": "freddym@startin.org.co", "proposito": "Demostraciones", "cuando": 1758300000,
                 "estado": "activo", "nivel": "interno"},
                {"ficha": "f2", "nombre": "Ana Ruiz", "organizacion": "Secretaría de Nariño",
                 "correo": "ana@narino.gov.co", "proposito": "Cobertura", "cuando": 1758200000,
                 "estado": "activo", "nivel": "registrado"},
                {"ficha": "f3", "nombre": "Sin Confirmar", "organizacion": "X", "correo": "x@x.co",
                 "proposito": "-", "cuando": 1758100000, "estado": "sin confirmar", "nivel": ""},
            ],
            "totales": {"registros": 3, "activos": 2, "sin_confirmar": 1, "revocados": 0,
                        "accesos_vigentes": {"interno": 1, "registrado": 1}},
            "uso": {"dias": [{"dia": f"2026-09-{d:02d}", "consultas": d, "usd": d * 0.01} for d in range(7, 21)],
                    "gasto_mes_usd": 12.5, "presupuesto_mensual_usd": 300.0, "presupuesto_diario_usd": 9.0,
                    "herramientas": 13, "modelo": "gpt-4.1-mini"},
        }
        revocados: list[str] = []
        def ruta_interno(route, request):
            url = request.url
            cab = request.headers.get("x-brujula-acceso", "")
            if url.startswith(f"{API}/estado"):
                return route.fulfill(status=200, content_type="application/json",
                    headers={"access-control-allow-origin": "*"},
                    body=json.dumps({"registrado": True, "restantes": None, "nivel": "interno"}))
            if url.startswith(f"{API}/admin/"):
                if request.method == "OPTIONS":
                    return route.fulfill(status=204, headers={
                        "access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST, OPTIONS",
                        "access-control-allow-headers": "content-type, x-brujula-acceso"})
                if cab != "cred-interna":
                    return route.fulfill(status=403, content_type="application/json",
                        headers={"access-control-allow-origin": "*"}, body=json.dumps({"motivo": "no"}))
                if url.startswith(f"{API}/admin/revocar"):
                    revocados.append(json.loads(request.post_data or "{}").get("ficha"))
                    RESUMEN["registros"][1]["estado"] = "revocado"
                    return route.fulfill(status=200, content_type="application/json",
                        headers={"access-control-allow-origin": "*"}, body=json.dumps({"motivo": "ok"}))
                return route.fulfill(status=200, content_type="application/json",
                    headers={"access-control-allow-origin": "*"}, body=json.dumps(RESUMEN, ensure_ascii=False))
            return ruta(route, request)
        pagina.unroute(f"{API}/**")
        pagina.route(f"{API}/**", ruta_interno)
        # Entra por un enlace como el del correo: la credencial va en el fragmento.
        # (Desde otra página: si solo cambiara el fragmento, el navegador no
        # recargaría y la credencial nunca se leería.)
        pagina.goto("about:blank")
        pagina.goto(f"{base}/consultar.html#acceso=cred-interna")
        pagina.wait_for_timeout(700)
        ok("#acceso" not in pagina.url, "la credencial se borra de la barra de direcciones")
        chip = pagina.inner_text("#chip-cupo").strip().lower()
        ok("interno" in chip and "startin" in chip, f"el chip dice acceso interno ({chip})")
        ok(pagina.eval_on_selector("#admin-enlace", "e => !e.hidden"), "y aparece el enlace al panel del equipo")
        ok(pagina.eval_on_selector("#registro-en-linea", "e => e.hidden"), "sin formulario de registro para quien ya entró")
        pagina.click("#admin-enlace")
        pagina.wait_for_selector("#panel:not([hidden])", timeout=5000)
        ok(pagina.eval_on_selector("#aviso", "e => e.hidden"), "el panel abre sin aviso de acceso denegado")
        tarjetas = pagina.inner_text("#tarjetas")
        ok("3" in tarjetas and "2 activos" in tarjetas, "las tarjetas muestran registros y activos")
        ok("USD 12.50" in pagina.inner_text("#gasto-mes") and "USD 300.00" in pagina.inner_text("#tope-mes"),
           "el presupuesto del mes se ve con gasto y tope")
        ok(pagina.eval_on_selector_all("#dias .dia", "els => els.length") == 14, "14 días de consultas")
        filas = pagina.eval_on_selector_all("#tabla-registros tbody tr", "els => els.length")
        ok(filas == 3, f"la tabla lista los 3 registros ({filas})")
        ok("freddym@startin.org.co" in pagina.inner_text("#tabla-registros"), "con el correo de cada uno")
        ok(pagina.eval_on_selector_all("#tabla-registros .estado.interno", "els => els.length") == 1,
           "y distingue el acceso interno")
        pagina.on("dialog", lambda d: d.accept())
        pagina.click("#tabla-registros button[data-ficha='f2']")
        pagina.wait_for_timeout(700)
        ok(revocados == ["f2"], "revocar manda la ficha correcta al servicio")
        ok(pagina.eval_on_selector_all("#tabla-registros .estado.revocado", "els => els.length") == 1,
           "y la tabla se refresca mostrando el acceso revocado")
        ok(not errores, "sin errores de JavaScript en el panel" + (f" → {errores[0][:90]}" if errores else ""))

        print("\n== 10. Sin acceso interno, el panel no muestra nada ==")
        pagina.evaluate("localStorage.setItem('brujula-visitante', 'otra')")
        pagina.goto(f"{base}/admin.html")
        pagina.wait_for_selector("#aviso:not([hidden])", timeout=5000)
        ok(pagina.eval_on_selector("#panel", "e => e.hidden"), "el panel queda oculto")
        ok("startin" in pagina.inner_text("#aviso").lower(), "y explica cómo se consigue el acceso")
        ok(not errores, "sin errores de JavaScript")

        navegador.close()
    servidor.shutdown()

    print("\n" + "=" * 64)
    if fallos:
        print(f"FALLARON {len(fallos)} comprobaciones:")
        for f in fallos: print(f"  · {f}")
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
