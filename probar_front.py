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
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"

# Un servicio de mentiras: lo que el orquestador contestaría a cada ruta.
TERRITORIOS = {"Cundinamarca": ["Soacha", "Fusagasugá"], "Nariño": ["San Andres de Tumaco"]}
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


def main() -> int:
    from playwright.sync_api import sync_playwright

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
            if cuerpo is None:
                return route.fulfill(status=404, body="")
            route.fulfill(status=200, content_type="application/json",
                          headers={"access-control-allow-origin": "*"},
                          body=json.dumps(cuerpo, ensure_ascii=False))
        pagina.route(f"{API}/**", ruta)

        print("\n== 1. consultar.html arranca sin errores ==")
        pagina.goto((WEB / "consultar.html").as_uri())
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

        print("\n== 4. Una pregunta se pinta bien ==")
        pagina.select_option("#sel-depto", "Nariño")
        pagina.select_option("#sel-mun", "San Andres de Tumaco")
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

        print("\n== 5. index.html arranca y muestra el aviso de enlace caducado ==")
        errores.clear()
        pagina.goto((WEB / "index.html").as_uri() + "?acceso=caducado")
        pagina.wait_for_timeout(500)
        ok(not errores, "sin errores de JavaScript")
        ok(pagina.eval_on_selector("#caducado", "e => !e.hidden"), "el aviso de enlace caducado se ve")

        navegador.close()

    print("\n" + "=" * 64)
    if fallos:
        print(f"FALLARON {len(fallos)} comprobaciones:")
        for f in fallos: print(f"  · {f}")
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
