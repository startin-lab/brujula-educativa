#!/usr/bin/env python3
"""
Brújula Educativa — Orquestador
Fundación Startin

La pieza que une las tres partes: recibe la pregunta del front, decide si esa
persona puede hacerla, se la pasa al modelo con las herramientas del servidor
MCP, y devuelve la respuesta con sus visualizaciones y sus salvedades.

QUIÉN DIBUJA LAS GRÁFICAS

  El modelo NO. Las herramientas del MCP devuelven un campo `vis` con la
  visualización ya armada, elegida de un catálogo cerrado de siete tipos. El
  orquestador la recoge tal cual y la manda al front.

  Es deliberado. Un modelo que genera sus propias gráficas puede escoger la
  escala que favorezca la historia que está contando, y ese es exactamente el
  tipo de error que este proyecto existe para no cometer. Aquí el modelo
  escribe el texto; los números y su forma vienen de los datos.

LAS SALVEDADES NO DEPENDEN DEL MODELO

  Cada herramienta devuelve `advertencias`. El orquestador las junta y las
  envía por separado, además de dárselas al modelo. Si el modelo se olvida de
  mencionar que la deserción es de 2022 y no de 2024, la advertencia llega
  igual y el front la pinta pegada a la cifra.

EL TOKEN NACIONAL NUNCA ENTRA AL PROMPT

  `ranking_nacional` pide un token. Si ese token viajara en las instrucciones
  del modelo, cualquiera que consiguiera que el modelo lo repitiera tendría
  acceso al país entero. Así que el modelo llama a la herramienta SIN token y
  es el orquestador quien lo inyecta, y solo si el visitante tiene acceso
  completo. Para todos los demás, la llamada se rechaza aquí mismo.

FALLA CERRADO

  Si el portero no puede decidir —contadores caídos, presupuesto sin leer— no
  se llama al modelo. Gastar sin saber cuánto se lleva gastado es justo lo que
  el portero existe para impedir.

Variables:
  BRUJULA_MCP_URL              http://.../sse del servidor MCP
  BRUJULA_FOUNDRY_ENDPOINT     https://<recurso>.cognitiveservices.azure.com/
  BRUJULA_MODELO               nombre del despliegue del modelo en Foundry
  BRUJULA_TOKEN_NACIONAL       habilita las consultas de país entero
  BRUJULA_BUZON                de dónde salen los enlaces de acceso
  BRUJULA_SITIO                dirección pública, para armar esos enlaces
  BRUJULA_HORAS_ENLACE         cuánto vive un enlace de acceso (72 por defecto)
  AZURE_STORAGE_CUENTA         contadores del portero (identidad administrada)
"""

# OJO: aquí NO va `from __future__ import annotations`.
#
# Con ese import, las anotaciones de tipo quedan como texto y FastAPI las
# resuelve contra el espacio de nombres del MÓDULO. Como los modelos de este
# archivo se declaran dentro de crear_app(), FastAPI no los encontraba y
# trataba el cuerpo de la petición como parámetros de URL: toda llamada
# respondía 422 «field required» sin tocar una línea de nuestra lógica.
# En Python 3.12 no hace falta para escribir dict[str, Any] ni str | None.

import json
import logging
import os
from typing import Any

LOG = logging.getLogger("brujula.orquestador")

MCP_URL = os.environ.get("BRUJULA_MCP_URL", "http://localhost:8080/sse")
FOUNDRY = os.environ.get("BRUJULA_FOUNDRY_ENDPOINT", "")
MODELO = os.environ.get("BRUJULA_MODELO", "")
TOKEN_NACIONAL = os.environ.get("BRUJULA_TOKEN_NACIONAL", "")
BUZON = os.environ.get("BRUJULA_BUZON", "brujula@startin.org.co")
SITIO = os.environ.get("BRUJULA_SITIO", "https://brujula.startinlab.org")
# Un enlace de acceso que no vence es una llave tirada en un buzón para siempre.
HORAS_ENLACE = int(os.environ.get("BRUJULA_HORAS_ENLACE", "72"))

# Cuántas vueltas de herramientas se permiten antes de cortar. Una pregunta
# normal usa dos o tres. El tope existe para que un modelo que se enreda no
# gaste el presupuesto del día en una sola pregunta.
MAX_VUELTAS = int(os.environ.get("BRUJULA_MAX_VUELTAS", "6"))

# Familias de modelo que rechazan `temperature`. Se detecta por el nombre del
# despliegue, que es lo único que conocemos aquí.
_SIN_TEMPERATURA = any(
    marca in MODELO.lower() for marca in ("claude", "opus", "sonnet", "haiku")
)

INSTRUCCIONES = """\
Eres Brújula Educativa, de la Fundación Startin. Respondes preguntas sobre
educación en Colombia usando únicamente datos abiertos oficiales.

CÓMO TRABAJAS

- Todo diagnóstico es territorial. Nunca respondas «sobre el país» salvo que
  te lo pidan con acceso completo. Si no sabes de qué municipio o departamento
  hablan, pregúntalo antes de consultar.
- Usa las herramientas SIEMPRE, incluso si crees saber la respuesta. Ninguna
  cifra sale de tu memoria: toda cifra viene de una herramienta de esta sesión.
  No inventes, no estimes, no redondees de cabeza. Si una herramienta no trae un dato,
  di que no está publicado.
- Cuando una herramienta devuelva advertencias, incorpóralas a tu respuesta en
  lenguaje llano. Son el contexto sin el cual la cifra engaña.
- Las señales indican dónde revisar. No son hallazgos ni acusaciones. Nunca
  afirmes que hubo desvío de recursos, irregularidad o mala gestión: di que
  ahí vale la pena pedir documentos.
- Si hay varios lugares con el mismo nombre, pregunta cuál antes de seguir.
  Mandar un diagnóstico al municipio equivocado es peor que preguntar.

CÓMO ESCRIBES

- En español, claro y directo, sin adornos. Quien te lee suele estar
  decidiendo dónde poner un esfuerzo limitado.
- No describas las gráficas: el sistema las muestra aparte. Explica qué
  significan los números y qué no se puede concluir de ellos.
- Di siempre de qué año es cada cifra cuando el año importe.
"""


# --------------------------------------------------------------------- #
#  Herramientas del MCP
# --------------------------------------------------------------------- #

def _esquema_a_openai(herramienta: Any) -> dict[str, Any]:
    """Traduce una herramienta MCP al formato de «tools» del modelo."""
    return {
        "type": "function",
        "function": {
            "name": herramienta.name,
            "description": (herramienta.description or "").strip()[:1024],
            "parameters": herramienta.inputSchema or {"type": "object", "properties": {}},
        },
    }


class Herramientas:
    """
    Envoltorio del servidor MCP.

    Se conecta una vez y mantiene la sesión. El servidor lee parquet desde
    disco, así que las llamadas son de milisegundos y no vale la pena abrir y
    cerrar una sesión por pregunta.
    """

    def __init__(self, url: str = MCP_URL) -> None:
        self.url = url
        self._sesion = None
        self._catalogo: list[dict[str, Any]] = []

    async def abrir(self) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        self._ctx = sse_client(self.url)
        lectura, escritura = await self._ctx.__aenter__()
        self._ses_ctx = ClientSession(lectura, escritura)
        self._sesion = await self._ses_ctx.__aenter__()
        await self._sesion.initialize()
        listado = await self._sesion.list_tools()
        self._catalogo = [_esquema_a_openai(h) for h in listado.tools]
        LOG.info("MCP conectado: %s herramientas", len(self._catalogo))

    async def cerrar(self) -> None:
        for ctx in ("_ses_ctx", "_ctx"):
            objeto = getattr(self, ctx, None)
            if objeto is not None:
                try:
                    await objeto.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    @property
    def catalogo(self) -> list[dict[str, Any]]:
        return self._catalogo

    async def llamar(self, nombre: str, argumentos: dict[str, Any],
                     acceso_completo: bool) -> dict[str, Any]:
        """
        Ejecuta una herramienta. Aquí se aplica la única regla de acceso que el
        modelo no puede saltarse: el país entero.
        """
        if nombre == "ranking_nacional":
            if not acceso_completo or not TOKEN_NACIONAL:
                return {
                    "encontrado": False,
                    "motivo": "Las consultas de país entero requieren acceso "
                              "completo. Responde sobre un departamento.",
                }
            # El token lo pone el orquestador, no el modelo.
            argumentos = {**argumentos, "token": TOKEN_NACIONAL}

        resultado = await self._sesion.call_tool(nombre, argumentos)
        textos = [c.text for c in resultado.content if getattr(c, "text", None)]
        if not textos:
            return {"encontrado": False, "motivo": "La herramienta no devolvió nada."}
        try:
            return json.loads(textos[0])
        except json.JSONDecodeError:
            return {"texto": textos[0]}


# --------------------------------------------------------------------- #
#  Modelo
# --------------------------------------------------------------------- #

def cliente_modelo():
    """
    Cliente contra Azure AI Foundry, autenticado con identidad administrada.

    Sin clave de API: el contenedor pide un token con su propia identidad. Lo
    mismo que hacen el servidor y la ingesta contra Storage.
    """
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    if not FOUNDRY or not MODELO:
        raise RuntimeError(
            "Faltan BRUJULA_FOUNDRY_ENDPOINT y BRUJULA_MODELO: sin eso no hay "
            "a quién preguntarle."
        )
    proveedor = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=FOUNDRY,
        azure_ad_token_provider=proveedor,
        api_version="2024-10-21",
    )


# --------------------------------------------------------------------- #
#  El ciclo
# --------------------------------------------------------------------- #

async def responder(pregunta: str, departamento: str, municipio: str,
                    herramientas: Herramientas, modelo: Any,
                    acceso_completo: bool = False) -> dict[str, Any]:
    """
    Una pregunta, de principio a fin.

    Devuelve el texto del modelo, las visualizaciones que trajeron las
    herramientas, las advertencias de cada fuente y el consumo en tokens, que
    es lo que el portero necesita para descontar del presupuesto.
    """
    ambito = []
    if departamento:
        ambito.append(f"Departamento: {departamento}")
    if municipio:
        ambito.append(f"Municipio: {municipio}")
    contexto = " · ".join(ambito) if ambito else "Sin territorio elegido todavía."

    mensajes = [
        {"role": "system", "content": INSTRUCCIONES},
        {"role": "system", "content": f"Ámbito de esta consulta — {contexto}"},
        {"role": "user", "content": pregunta},
    ]

    vis: list[dict[str, Any]] = []
    advertencias: list[str] = []
    cortes: set[str] = set()
    entrada = salida = 0

    for vuelta in range(MAX_VUELTAS):
        # `temperature` se manda solo si el modelo la acepta. Los modelos
        # Claude en Foundry NO admiten `temperature` ni `top_k`, y mandarla
        # hace fallar la llamada entera. Como la elección de modelo es una
        # variable de entorno, el código tiene que aguantar las dos familias
        # sin que nadie recuerde editarlo el día del cambio.
        extra = {} if _SIN_TEMPERATURA else {"temperature": 0}
        respuesta = modelo.chat.completions.create(
            model=MODELO,
            messages=mensajes,
            tools=herramientas.catalogo,
            **extra,
        )
        uso = getattr(respuesta, "usage", None)
        if uso:
            entrada += uso.prompt_tokens or 0
            salida += uso.completion_tokens or 0

        eleccion = respuesta.choices[0].message
        if not eleccion.tool_calls:
            return {
                "respuesta": eleccion.content or "",
                "vis": vis,
                "advertencias": sorted(set(advertencias)),
                "corte": sorted(cortes)[-1] if cortes else None,
                "tokens_entrada": entrada,
                "tokens_salida": salida,
                "vueltas": vuelta + 1,
            }

        mensajes.append(eleccion.model_dump(exclude_none=True))
        for llamada in eleccion.tool_calls:
            try:
                argumentos = json.loads(llamada.function.arguments or "{}")
            except json.JSONDecodeError:
                argumentos = {}
            datos = await herramientas.llamar(
                llamada.function.name, argumentos, acceso_completo
            )

            # Las visualizaciones y las salvedades se recogen aquí, no se le
            # piden al modelo: así llegan aunque el modelo las ignore.
            bloque = datos.get("vis")
            if isinstance(bloque, dict):
                vis.append(bloque)
            elif isinstance(bloque, list):
                vis.extend(b for b in bloque if isinstance(b, dict))
            advertencias.extend(a for a in datos.get("advertencias", []) if a)
            if datos.get("corte"):
                cortes.add(str(datos["corte"]))

            mensajes.append({
                "role": "tool",
                "tool_call_id": llamada.id,
                "content": json.dumps(datos, ensure_ascii=False)[:60_000],
            })

    return {
        "respuesta": "La consulta se enredó y se cortó para no seguir gastando. "
                     "Prueba con una pregunta más concreta.",
        "vis": vis,
        "advertencias": sorted(set(advertencias)),
        "corte": sorted(cortes)[-1] if cortes else None,
        "tokens_entrada": entrada,
        "tokens_salida": salida,
        "vueltas": MAX_VUELTAS,
    }


# --------------------------------------------------------------------- #
#  Correo
# --------------------------------------------------------------------- #

def enviar_correo(destinatario: str, asunto: str, cuerpo_html: str) -> None:
    """
    Manda un correo por Microsoft Graph con la identidad administrada.

    No hay clave de API ni contraseña de buzón: el contenedor pide un token con
    su propia identidad. Del lado del tenant, esa identidad tiene el permiso de
    envío restringido por política a UN solo buzón, así que aunque este código
    se equivocara de remitente, Exchange lo rechazaría.
    """
    import urllib.error
    import urllib.request

    from azure.identity import DefaultAzureCredential

    credencial = DefaultAzureCredential()
    token = credencial.get_token("https://graph.microsoft.com/.default").token

    mensaje = {
        "message": {
            "subject": asunto,
            "body": {"contentType": "HTML", "content": cuerpo_html},
            "toRecipients": [{"emailAddress": {"address": destinatario}}],
        },
        "saveToSentItems": True,
    }
    peticion = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/users/{BUZON}/sendMail",
        data=json.dumps(mensaje).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as respuesta:
            if respuesta.status not in (200, 202):
                raise RuntimeError(f"Graph respondió {respuesta.status}")
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Graph rechazó el envío ({exc.code}): {detalle}") from exc


def correo_de_acceso(nombre: str, enlace: str) -> str:
    saludo = f"Hola, {nombre.split()[0]}." if nombre.strip() else "Hola."
    return f"""\
<p>{saludo}</p>
<p>Ya puedes usar Br&uacute;jula Educativa sin l&iacute;mite de consultas.
   Abre este enlace desde el dispositivo en el que vayas a consultar:</p>
<p><a href="{enlace}">Entrar a Br&uacute;jula Educativa</a></p>
<p>El enlace sirve una sola vez y vence en {HORAS_ENLACE} horas.</p>
<hr>
<p style="color:#56527A;font-size:13px">
  Br&uacute;jula Educativa es un proyecto de la Fundaci&oacute;n Startin. Es gratuito.<br>
  Datos abiertos del Ministerio de Educaci&oacute;n, ICFES, MinTIC, DANE,
  Computadores Para Educar, Colombia Compra Eficiente y OCDE.<br>
  Para conocer, actualizar, rectificar o suprimir tus datos, o revocar tu
  autorizaci&oacute;n: notificaciones@startin.org.co
</p>"""


# --------------------------------------------------------------------- #
#  El servicio
# --------------------------------------------------------------------- #

def _ip_del_cliente(peticion: Any) -> str:
    """
    La IP real detrás del ingress de Container Apps.

    `request.client.host` sería la del balanceador, igual para todo el mundo,
    y el límite por IP dejaría de existir sin que nada lo indicara.
    """
    reenviada = peticion.headers.get("x-forwarded-for", "")
    if reenviada:
        return reenviada.split(",")[0].strip()
    return getattr(peticion.client, "host", "") or "desconocida"


def crear_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, EmailStr, Field

    import proxy

    class Consulta(BaseModel):
        pregunta: str = Field(min_length=3, max_length=500)
        departamento: str = ""
        municipio: str = ""
        visitante: str = ""

    class Registro(BaseModel):
        nombre: str = Field(min_length=2, max_length=80)
        organizacion: str = Field(min_length=2, max_length=120)
        correo: EmailStr
        proposito: str = Field(min_length=10, max_length=400)
        autoriza: bool = False
        politica: str = ""

    estado: dict[str, Any] = {}

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI):
        # El portero y el correo son lo primero, y lo único imprescindible:
        # el registro de nuevos usuarios no tiene por qué caerse porque el
        # servidor de datos esté en mantenimiento o todavía sin corte.
        estado["portero"] = proxy.construir_portero()

        estado["herramientas"] = Herramientas()
        try:
            await estado["herramientas"].abrir()
        except Exception as exc:  # noqa: BLE001
            # Arrancar igual es deliberado. Si el MCP no responde, /preguntar
            # dirá que no hay datos —con todas las letras— mientras /registrar
            # y /entrar siguen funcionando. Negarse a arrancar convertiría un
            # problema de una pieza en la caída de todo el servicio.
            LOG.error("Sin servidor MCP (%s). Las consultas quedan cerradas; "
                      "el registro sigue abierto.", exc)

        try:
            estado["modelo"] = cliente_modelo()
        except Exception as exc:  # noqa: BLE001
            estado["modelo"] = None
            LOG.error("Sin modelo configurado (%s). Las consultas quedan cerradas.", exc)

        yield
        await estado["herramientas"].cerrar()

    app = FastAPI(title="Brújula Educativa", lifespan=ciclo_de_vida)

    # Solo el front publicado. Un comodín aquí significaría que cualquier
    # página puede gastar el presupuesto de la fundación desde el navegador
    # de sus visitantes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o for o in os.environ.get(
            "BRUJULA_ORIGENES", "https://brujula.startinlab.org").split(",") if o],
        allow_methods=["POST", "GET"],
        allow_headers=["content-type"],
    )

    @app.get("/salud")
    async def salud() -> dict[str, Any]:
        herramientas = len(estado["herramientas"].catalogo)
        return {
            "herramientas": herramientas,
            "modelo": MODELO or "sin configurar",
            "consultas": "abiertas" if (herramientas and estado.get("modelo"))
                         else "cerradas: falta el corte de datos o el modelo",
            "registro": "abierto",
            "portero": estado["portero"].estado(),
        }

    @app.post("/preguntar")
    async def preguntar(consulta: Consulta, peticion: Request) -> JSONResponse:
        if not estado["herramientas"].catalogo or not estado.get("modelo"):
            return JSONResponse(
                {"motivo": "Todavía no hay un corte de datos publicado. El "
                           "registro sí está abierto; te avisamos en cuanto "
                           "Brújula pueda responder."},
                status_code=503,
            )

        ip = _ip_del_cliente(peticion)
        visitante = consulta.visitante or ip
        portero = estado["portero"]

        veredicto = portero.evaluar(
            consulta.pregunta, ip, visitante,
            consulta.departamento, consulta.municipio,
        )
        if veredicto.desde_cache and veredicto.respuesta:
            # Sale de caché: no se llama al modelo y no se descuenta nada.
            return JSONResponse({**veredicto.respuesta, "desde_cache": True})
        if not veredicto.permitir:
            cuerpo = {"motivo": veredicto.motivo}
            if veredicto.espera_segundos:
                cuerpo["espera_segundos"] = veredicto.espera_segundos
            return JSONResponse(cuerpo, status_code=veredicto.codigo)

        try:
            resultado = await responder(
                consulta.pregunta, consulta.departamento, consulta.municipio,
                estado["herramientas"], estado["modelo"],
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("La consulta falló")
            return JSONResponse(
                {"motivo": "No pudimos responder esta consulta. "
                           "Vuelve a intentarlo en un momento."},
                status_code=502,
            )

        usd = portero.registrar_consumo(
            consulta.pregunta, ip, visitante, resultado,
            resultado["tokens_entrada"], resultado["tokens_salida"],
            consulta.departamento, consulta.municipio,
        )
        LOG.info("Consulta atendida: %s vueltas, %.4f USD", resultado["vueltas"], usd)
        return JSONResponse({**resultado, "desde_cache": False})

    @app.post("/registrar")
    async def registrar(datos: Registro) -> JSONResponse:
        # Sin autorización no se guarda nada. La casilla del formulario es lo
        # que la ley pide poder probar, así que si no viene marcada no hay
        # registro, ni siquiera "por ahora".
        if not datos.autoriza:
            return JSONResponse(
                {"motivo": "Necesitamos tu autorización para tratar los datos."},
                status_code=400,
            )

        ficha = secrets.token_urlsafe(32)
        portero = estado["portero"]
        try:
            portero.almacen.guardar_registro(ficha, {
                "nombre": datos.nombre,
                "organizacion": datos.organizacion,
                "correo": str(datos.correo),
                "proposito": datos.proposito,
                "autoriza": True,
                "politica": datos.politica or "https://startin.org.co/privacidad/",
                "cuando": time.time(),
                "usado": False,
            })
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo guardar el registro")
            return JSONResponse(
                {"motivo": "No pudimos guardar la solicitud. Inténtalo de nuevo."},
                status_code=503,
            )

        enlace = f"{SITIO}/entrar?t={ficha}"
        try:
            enviar_correo(
                str(datos.correo),
                "Tu acceso a Brújula Educativa",
                correo_de_acceso(datos.nombre, enlace),
            )
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo enviar el correo de acceso")
            return JSONResponse(
                {"motivo": "Guardamos tu solicitud pero no pudimos enviarte el "
                           "correo. Escríbenos a hola@startin.org.co."},
                status_code=502,
            )
        return JSONResponse({"motivo": "Listo. Te enviamos un enlace de acceso al "
                                       "correo; revisa también la carpeta de no deseados."})

    @app.get("/entrar")
    async def entrar(t: str = ""):
        from fastapi.responses import RedirectResponse

        portero = estado["portero"]
        datos = None
        try:
            datos = portero.almacen.leer_registro(t) if t else None
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo leer el registro")

        vencido = bool(datos) and (time.time() - float(datos.get("cuando", 0))
                                   > HORAS_ENLACE * 3600)
        if not datos or datos.get("usado") or vencido:
            return RedirectResponse(f"{SITIO}/?acceso=caducado", status_code=303)

        # La credencial se emite AQUÍ, no en el formulario: así el enlace sirve
        # en el dispositivo donde se abra el correo, que rara vez es el mismo
        # donde se llenó el formulario.
        credencial = secrets.token_urlsafe(24)
        portero.almacen.acreditar(credencial)
        portero.almacen.marcar_registro_usado(t)
        return RedirectResponse(f"{SITIO}/consultar.html#acceso={credencial}",
                                status_code=303)


    return app


# `uvicorn orquestador:app` necesita el objeto ya construido. La conexión al
# MCP y al modelo no ocurre aquí sino en el ciclo de vida, cuando el servidor
# arranca: así un fallo de red al importar no deja un módulo a medio cargar.
app = crear_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PUERTO", "8000")))
