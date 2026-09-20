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
TERRITORIOS = {"Cundinamarca": ["Soacha", "Fusagasugá"], "Nariño": ["San Andres de Tumaco"]}
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
    "saber11": {"sedes_evaluadas": 51},
    "economia_del_departamento": {"ambito": "Departamento de Nariño", "anio": 2023,
        "actividades_principales": [
            {"actividad": "Administración pública y defensa; planes de seguridad social de afiliación obligatoria; educación; actividades de atención de la salud humana y de servicios sociales", "pct_del_pib": 27.0},
            {"actividad": "Agricultura, ganadería, caza, silvicultura y pesca", "pct_del_pib": 14.2}],
        "advertencia": "El PIB solo se publica por departamento."},
    "senales": [{"clave": "cobertura_neta_baja"}, {"clave": "brecha_digital_alta"}],
}
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
        def ruta(route, request):
            url = request.url
            cuerpo = None
            if url.startswith(f"{API}/territorios"): cuerpo = TERRITORIOS
            elif url.startswith(f"{API}/estado"):    cuerpo = {"registrado": False, "restantes": 10}
            elif url.startswith(f"{API}/preguntar"): cuerpo = RESPUESTA
            elif url.startswith(f"{API}/ficha"):     cuerpo = FICHA
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
        ok("60.491" in inicio, "muestra la población de 5 a 16 años")
        ok("178" in inicio and "Pasto" in inicio, "la distancia a la capital")
        ok("Popayán" in inicio and "Cali" in inicio, "las capitales cercanas")
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
        ok(pagina.eval_on_selector_all("#inicio svg circle.cap", "e => e.length") == 3, "y las tres capitales cercanas")
        ok("DANE" in inicio, "acredita la fuente de las siluetas")
        ok(pagina.inner_text("#chip-cupo").strip().lower() == "10 consultas libres", "la ficha no descontó cupo")
        ok(not errores, "sin errores de JavaScript al pintar la ficha" + (f" → {errores[0][:90]}" if errores else ""))

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
        rotulos = pagina.eval_on_selector_all(".plano .punto b", "els => els.length")
        ok(rotulos == 1, f"el mapa con 31 puntos rotula solo el destacado ({rotulos} rótulo)")
        ok("31" not in pagina.inner_text(".vis") or "sin rotular" in pagina.inner_text("#bloques"),
           "y dice cuántos quedan sin rótulo")
        ok("Cobertura neta baja" in pagina.inner_text("#bloques"), "la señal se muestra legible, no como identificador")
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
