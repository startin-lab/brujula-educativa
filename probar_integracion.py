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

    def create(self, **kw):
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
    orquestador.cliente_modelo = lambda: ModeloSimulado()
    orquestador.enviar_correo = lambda *a, **k: None

    from fastapi.testclient import TestClient

    fallos: list[str] = []

    def ok(condicion: bool, texto: str) -> None:
        print(f"  {'OK   ' if condicion else 'FALLA'} {texto}")
        if not condicion:
            fallos.append(texto)

    with TestClient(orquestador.crear_app()) as cliente:
        print("\n== 1. El orquestador ve el catálogo del MCP ==")
        salud = cliente.get("/salud").json()
        ok(salud["herramientas"] == 3, "el catálogo llegó completo")
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

        print("\n== 5. El registro sigue vivo aunque no haya modelo ==")
        r3 = cliente.post("/registrar", json={
            "nombre": "Ana Ruiz", "organizacion": "Secretaría de Boyacá",
            "correo": "ana@boyaca.gov.co", "autoriza": True,
            "proposito": "Diagnóstico de cobertura en municipios del norte"})
        ok(r3.status_code == 200, "el registro responde 200")

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
