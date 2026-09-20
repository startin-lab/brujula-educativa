#!/usr/bin/env python3
"""
Brújula Educativa — Prueba de integración orquestador ↔ servidor MCP
Fundación Startin

POR QUÉ EXISTE ESTE ARCHIVO

  El 19 de septiembre de 2026 el servicio estuvo horas informando «13
  herramientas, consultas abiertas» mientras fallaba toda consulta real. Las
  pruebas que teníamos no lo habrían visto: `probar_server.py` prueba el
  servidor MCP por dentro y `probar_proxy.py` prueba el portero por dentro,
  pero nadie probaba a los dos hablándose.

  El fallo vivía justo en esa costura. El orquestador abría una sesión MCP al
  arrancar y la guardaba; por debajo, sse_client levanta un grupo de tareas de
  anyio, que pertenece a la tarea que lo creó. Pedir el catálogo funcionaba
  —misma tarea— y llamar a una herramienta desde la tarea de una petición, no.
  De ahí un diagnóstico sano sobre un servicio roto.

  Esta prueba levanta un servidor MCP de mentiras en un puerto local, monta el
  orquestador de verdad contra él con un modelo simulado, y comprueba que una
  pregunta cruza entera: catálogo, llamada a herramienta, visualizaciones,
  advertencias, fecha de corte y descuento de cuota. Y lo hace DOS veces, que
  es lo que distingue una sesión que se reabre bien de una que funcionó una
  vez por casualidad.

  No necesita Azure, ni modelo, ni datos: corre en cualquier portátil.

      python3 probar_integracion.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

PUERTO = int(os.environ.get("PUERTO_PRUEBA", "8931"))
RAIZ = os.path.dirname(os.path.abspath(__file__))

SERVIDOR_FALSO = '''
import json, sys
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP("falso", transport_security=TransportSecuritySettings(
    allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
    allowed_origins=["http://localhost", "http://localhost:*",
                     "http://127.0.0.1", "http://127.0.0.1:*"]))

@mcp.tool()
def listar_departamentos() -> str:
    """Los departamentos disponibles."""
    return json.dumps({"encontrado": True, "departamentos": [
        {"departamento": "CUNDINAMARCA", "municipios": 2},
        {"departamento": "NARIÑO", "municipios": 1},
    ]}, ensure_ascii=False)


@mcp.tool()
def listar_municipios(departamento: str) -> str:
    """Los municipios de un departamento."""
    mapa = {"CUNDINAMARCA": ["Soacha", "Fusagasugá"], "NARIÑO": ["Túquerres"]}
    return json.dumps({"encontrado": True, "departamento": departamento,
                       "municipios": [{"municipio": m}
                                      for m in mapa.get(departamento, [])]},
                      ensure_ascii=False)


@mcp.tool()
def colegios_del_municipio(municipio: str, departamento: str = "", orden: str = "resultado",
                           limite: int = 40) -> str:
    """Las sedes de un municipio."""
    # El orquestador debe pedir limite=0 para el selector: se deja constancia
    # en la respuesta para que la prueba lo verifique.
    return json.dumps({"encontrado": True, "total_sedes": 2, "limite_recibido": limite, "sedes": [
        {"cod_dane_sede": "2", "nombre": "IE Zeta", "naturaleza": "OFICIAL", "zona": "RURAL", "evaluados": 30, "matricula": 410},
        {"cod_dane_sede": "1", "nombre": "IE Alfa", "naturaleza": "NO OFICIAL", "zona": "URBANO", "evaluados": 90, "matricula": 1200},
    ]}, ensure_ascii=False)


@mcp.tool()
def ficha_municipio(departamento: str, municipio: str) -> str:
    """Ficha de un municipio."""
    return json.dumps({
        "encontrado": True, "municipio": municipio, "corte": "2026-09-20",
        "vis": {"tipo": "cifras",
                "datos": [{"etiqueta": "Cobertura neta", "valor": 91.2}]},
        "advertencias": ["La cobertura viene por nivel, no por grado."],
    }, ensure_ascii=False)

mcp.settings.host = "127.0.0.1"
mcp.settings.port = int(sys.argv[1])
mcp.run(transport="sse")
'''


def _mensaje(contenido=None, llamadas=None):
    m = types.SimpleNamespace(content=contenido, tool_calls=llamadas, role="assistant")
    m.model_dump = lambda exclude_none=True: {
        "role": "assistant", "content": contenido,
        "tool_calls": [
            {"id": t.id, "type": "function",
             "function": {"name": t.function.name, "arguments": t.function.arguments}}
            for t in (llamadas or [])] or None}
    return m


class ModeloSimulado:
    """
    Pide la herramienta si aún no tiene su resultado; si ya lo tiene, responde.

    Decide por el estado de la conversación y no por un contador propio: un
    contador global haría que la segunda pregunta de la prueba saltara la
    herramienta y la prueba pasaría sin haber probado nada.
    """

    def __init__(self):
        self.chat = types.SimpleNamespace(completions=self)
        self.visto: list[list[dict]] = []

    def create(self, **kw):
        self.visto.append(kw.get("messages", []))
        ya_llamo = any(m.get("role") == "tool" for m in kw.get("messages", []))
        if not ya_llamo:
            llamada = types.SimpleNamespace(
                id="c1", type="function",
                function=types.SimpleNamespace(
                    name="ficha_municipio",
                    arguments=json.dumps({"departamento": "CUNDINAMARCA",
                                          "municipio": "Soacha"})))
            msg = _mensaje(None, [llamada])
        else:
            msg = _mensaje("La cobertura neta de Soacha es 91,2%.", None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)],
            usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def _esperar(url: str, segundos: float = 30) -> None:
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.URLError:
            time.sleep(0.4)
        except Exception:
            return          # respondió algo: con eso basta
    raise RuntimeError(f"{url} no respondió en {segundos}s")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "mcp_falso.py")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(SERVIDOR_FALSO)

        servidor = subprocess.Popen(
            [sys.executable, ruta, str(PUERTO)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _esperar(f"http://127.0.0.1:{PUERTO}/sse")
            time.sleep(1.0)
            return _comprobar()
        finally:
            servidor.terminate()


def _comprobar() -> int:
    os.environ["BRUJULA_MCP_URL"] = f"http://127.0.0.1:{PUERTO}/sse"
    os.environ.setdefault("BRUJULA_FOUNDRY_ENDPOINT", "https://simulado/")
    os.environ.setdefault("BRUJULA_MODELO", "modelo-simulado")
    sys.path.insert(0, RAIZ)

    import orquestador
    modelo = ModeloSimulado()
    orquestador.cliente_modelo = lambda: modelo
    # Los correos no salen: se guardan para leer el enlace de acceso como lo
    # haría la persona que lo recibe.
    correos: list[tuple[str, str, str]] = []
    orquestador.enviar_correo = lambda dest, asunto, html: correos.append((dest, asunto, html))

    from fastapi.testclient import TestClient

    fallos: list[str] = []

    def ok(condicion: bool, texto: str) -> None:
        print(f"  {'OK   ' if condicion else 'FALLA'} {texto}")
        if not condicion:
            fallos.append(texto)

    with TestClient(orquestador.crear_app()) as cliente:
        print("\n== 1. El orquestador ve el catálogo del MCP ==")
        salud = cliente.get("/salud").json()
        ok(salud["herramientas"] == 4, "el catálogo llegó completo")
        ok(salud["consultas"] == "abiertas", "las consultas quedan abiertas")

        print("\n== 2. Una pregunta cruza entera ==")
        r = cliente.post("/preguntar", json={
            "pregunta": "¿cómo está la cobertura?", "departamento": "CUNDINAMARCA",
            "municipio": "Soacha", "visitante": "visitante-1"})
        d = r.json()
        ok(r.status_code == 200, "responde 200")
        ok(d.get("corte") == "2026-09-20", "la fecha de corte viene de la herramienta")
        ok([v.get("tipo") for v in d.get("vis", [])] == ["cifras"],
           "la visualización llega aunque el modelo no la mencione")
        ok(len(d.get("advertencias", [])) == 1, "la advertencia de la fuente llega")
        ok(d.get("restantes") == 9, "el portero descontó una consulta libre")

        print("\n== 3. La sesión MCP se reabre en la siguiente consulta ==")
        # Este es el caso que la versión con sesión eterna no pasaba: la
        # primera llamada podía colarse y las siguientes morían.
        r2 = cliente.post("/preguntar", json={
            "pregunta": "¿y la deserción?", "departamento": "CUNDINAMARCA",
            "municipio": "Soacha", "visitante": "visitante-2"})
        d2 = r2.json()
        ok(r2.status_code == 200, "la segunda consulta responde 200")
        ok(bool(d2.get("vis")), "la segunda consulta también trae datos")

        print("\n== 4. La página recibe el país entero, no una maqueta ==")
        # La lista de territorios estuvo escrita a mano en el HTML —dos
        # departamentos— y desde fuera parecía que Brújula solo cubría esos.
        t = cliente.get("/territorios")
        mapa = t.json()
        ok(t.status_code == 200, "responde 200")
        ok(sorted(mapa) == ["CUNDINAMARCA", "NARIÑO"],
           "llegan todos los departamentos del corte")
        ok(mapa.get("NARIÑO") == ["Túquerres"],
           "cada departamento trae sus municipios")

        print("\n== 4b. Las sedes del municipio y el ámbito por sede ==")
        c = cliente.get("/colegios", params={"departamento": "CUNDINAMARCA", "municipio": "Soacha"})
        ok(c.json().get("total") == 2 and c.json()["sedes"][0].get("matricula") == 1200,
           "el selector recibe el total y la matrícula de cada sede")
        sedes = c.json().get("sedes", [])
        ok(c.status_code == 200 and [s["nombre"] for s in sedes] == ["IE Alfa", "IE Zeta"],
           "lista las sedes ordenadas por nombre, con código")
        ok(all({"cod", "nombre", "naturaleza", "zona"} <= set(s) for s in sedes), "con los campos que el selector necesita")
        r4 = cliente.post("/preguntar", json={"pregunta": "¿cómo le va?", "departamento": "CUNDINAMARCA",
            "municipio": "Soacha", "sede": "IE Alfa", "cod_sede": "1", "visitante": "visitante-3"})
        ok(r4.status_code == 200, "una pregunta con sede responde 200")
        sistema = " ".join(m.get("content", "") for m in modelo.visto[-1] if m.get("role") == "system")
        ok("Sede elegida: IE Alfa" in sistema and "código DANE 1" in sistema,
           "y el modelo recibe la sede y su código en el ámbito")
        r5 = cliente.post("/preguntar", json={"pregunta": "¿cómo le va?", "departamento": "CUNDINAMARCA",
            "municipio": "Soacha", "sede": "IE Zeta", "cod_sede": "2", "visitante": "visitante-3"})
        ok(r5.json().get("desde_cache") is False, "la misma pregunta sobre otra sede no sale de la caché de la primera")
        r6 = cliente.post("/preguntar", json={"pregunta": "¿cómo le va?", "departamento": "CUNDINAMARCA",
            "municipio": "Soacha", "sede": "IE Alfa", "cod_sede": "1", "visitante": "visitante-3"})
        ok(r6.json().get("desde_cache") is True, "y repetirla sobre la misma sede sí")

        print("\n== 5. El registro sigue vivo aunque no haya modelo ==")
        r3 = cliente.post("/registrar", json={
            "nombre": "Ana Ruiz", "organizacion": "Secretaría de Boyacá",
            "correo": "ana@boyaca.gov.co", "autoriza": True,
            "proposito": "Diagnóstico de cobertura en municipios del norte"})
        ok(r3.status_code == 200, "el registro responde 200")

        print("\n== 6. Un correo de Startin entra con acceso interno y ve el panel ==")
        import re
        def entrar_con(correo: str) -> str:
            """Registro → enlace del correo → credencial, como lo vive la persona."""
            correos.clear()
            r = cliente.post("/registrar", json={
                "nombre": "Prueba", "organizacion": "Org", "correo": correo,
                "autoriza": True, "proposito": "Probar niveles de acceso"})
            assert r.status_code == 200, r.text
            enlace = next(re.search(r"/entrar\?t=([A-Za-z0-9_-]+)", html).group(1)
                          for dest, _, html in correos if dest == correo)
            r = cliente.get("/entrar", params={"t": enlace}, follow_redirects=False)
            assert r.status_code == 303, r.status_code
            return re.search(r"#acceso=([A-Za-z0-9_-]+)", r.headers["location"]).group(1)

        cred_int = entrar_con("freddym@startin.org.co")
        cred_reg = entrar_con("ana@boyaca.gov.co")
        e_int = cliente.get("/estado", params={"v": cred_int}).json()
        e_reg = cliente.get("/estado", params={"v": cred_reg}).json()
        ok(e_int.get("registrado") is True and e_int.get("nivel") == "interno",
           f"el correo @startin.org.co queda con nivel interno ({e_int})")
        ok(e_reg.get("registrado") is True and e_reg.get("nivel") == "registrado",
           f"otro correo queda registrado, sin más ({e_reg})")
        # Muchas más preguntas que el cupo libre, siempre desde la misma IP del
        # cliente de pruebas: ni el cupo ni el tope por IP deben aparecer.
        codigos = set()
        for i in range(__import__("proxy").PREGUNTAS_LIBRES_IP_DIA + 5):
            r = cliente.post("/preguntar", json={"pregunta": f"pregunta interna {i}", "departamento": "NARIÑO",
                "municipio": "Tumaco", "visitante": cred_int})
            codigos.add(r.status_code)
        ok(codigos == {200}, f"el acceso interno no se frena por cupo ni por IP ({sorted(codigos)})")
        ok(r.json().get("registrado") is True and r.json().get("restantes") is None,
           "y la respuesta no le cuenta consultas libres")

        ad = cliente.get("/admin/resumen", headers={"X-Brujula-Acceso": cred_int})
        ok(ad.status_code == 200, f"/admin/resumen abre con la credencial interna ({ad.status_code})")
        cuerpo = ad.json()
        ok(cuerpo["totales"]["registros"] >= 3 and cuerpo["totales"]["activos"] >= 2,
           f"lista los registros y cuántos están activos ({cuerpo['totales']})")
        ok(any(r.get("correo") == "freddym@startin.org.co" and r.get("nivel") == "interno" for r in cuerpo["registros"]),
           "marca el registro interno como tal")
        ok(len(cuerpo["uso"]["dias"]) == 14 and cuerpo["uso"]["presupuesto_mensual_usd"] > 0,
           "trae 14 días de uso y el presupuesto del mes")
        ok(cliente.get("/admin/resumen", headers={"X-Brujula-Acceso": cred_reg}).status_code == 403,
           "una credencial registrada corriente recibe 403")
        ok(cliente.get("/admin/resumen").status_code == 403, "sin cabecera, 403")
        ok(cliente.get("/admin/resumen", params={"acceso": cred_int}).status_code == 403,
           "la credencial en la URL no sirve: solo en la cabecera")

        ficha_reg = next(r["ficha"] for r in cuerpo["registros"] if r.get("correo") == "ana@boyaca.gov.co" and r["estado"] == "activo")
        rv = cliente.post("/admin/revocar", json={"ficha": ficha_reg}, headers={"X-Brujula-Acceso": cred_int})
        ok(rv.status_code == 200, f"revocar desde el panel responde 200 ({rv.status_code})")
        ok(cliente.get("/estado", params={"v": cred_reg}).json().get("registrado") is False,
           "y la persona vuelve al cupo libre")
        ok(cliente.post("/admin/revocar", json={"ficha": ficha_reg}, headers={"X-Brujula-Acceso": cred_reg}).status_code == 403,
           "nadie más puede revocar")

    print("\n" + "=" * 64)
    if fallos:
        print(f"FALLARON {len(fallos)} comprobaciones:")
        for f in fallos:
            print(f"  · {f}")
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
