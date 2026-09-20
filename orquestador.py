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
  BRUJULA_SITIO                dónde vive la página (la Static Web App)
  BRUJULA_API                  dónde vive ESTE servicio, que es otra máquina
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
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

LOG = logging.getLogger("brujula.orquestador")

MCP_URL = os.environ.get("BRUJULA_MCP_URL", "http://localhost:8080/sse")
FOUNDRY = os.environ.get("BRUJULA_FOUNDRY_ENDPOINT", "")
MODELO = os.environ.get("BRUJULA_MODELO", "")
TOKEN_NACIONAL = os.environ.get("BRUJULA_TOKEN_NACIONAL", "")
BUZON = os.environ.get("BRUJULA_BUZON", "brujula@startin.org.co")
SITIO = os.environ.get("BRUJULA_SITIO", "https://brujula.startinlab.org")
# Y aquí está la trampa que nos costó un correo entregado y un 404 en la cara:
# brujula.startinlab.org es la Static Web App, que solo sabe servir archivos.
# /entrar y /revocar son rutas de ESTE proceso, que corre en otra máquina. Un
# enlace de acceso armado sobre SITIO apunta a un sitio que no tiene esa ruta,
# y Azure responde su propio 404 —uno que ni siquiera parece nuestro—.
#
# Mientras la API no tenga nombre propio, el valor por defecto es el del
# contenedor. Cuando acceso.brujula.startinlab.org esté en pie, se cambia esta
# variable de entorno y los correos vuelven a salir con el dominio de la
# fundación, sin tocar una línea de código.
API_PROPIA = os.environ.get(
    "BRUJULA_API",
    "https://brujula-orquestador.ambitiousplant-035eb04f.eastus2.azurecontainerapps.io",
).rstrip("/")
# A dónde llega el aviso de cada registro nuevo. No es una formalidad: una
# herramienta pública que no sabe a quién le está sirviendo no puede decir que
# rinde cuentas. Además es el único camino para revocar un acceso.
AVISOS = os.environ.get("BRUJULA_AVISOS", "portales@startin.org.co")
# Un enlace de acceso que no vence es una llave tirada en un buzón para siempre.
HORAS_ENLACE = int(os.environ.get("BRUJULA_HORAS_ENLACE", "72"))
# Dominios de correo de la casa. Quien confirme un enlace de acceso enviado a
# uno de estos dominios entra con nivel «interno»: sin cupo, sin tope por IP,
# sin corte diario, con consultas de país entero, y con el panel de
# administración. La verificación es el propio enlace: para tenerlo hay que
# poder leer ese buzón. Comparación exacta del dominio, no «termina en».
DOMINIOS_INTERNOS = {d.strip().lower() for d in
                     os.environ.get("BRUJULA_DOMINIOS_INTERNOS", "startin.org.co").split(",")
                     if d.strip()}


def nivel_para(correo: str) -> str:
    dominio = (correo or "").rsplit("@", 1)[-1].strip().lower()
    return "interno" if dominio in DOMINIOS_INTERNOS else "registrado"

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

    Abre una sesión por consulta y la cierra al terminar.

    La versión anterior abría UNA sesión al arrancar y la guardaba para
    siempre. Parecía lo eficiente —el servidor lee parquet de disco, las
    llamadas son de milisegundos— y funcionó en todas las pruebas locales.
    En producción fallaba todas las consultas.

    El motivo: por debajo, sse_client levanta un grupo de tareas de anyio, y
    esos grupos pertenecen a la tarea que los creó. La sesión nacía en el
    arranque de la aplicación y se usaba después desde las tareas que atienden
    cada petición, que son otras. El catálogo de herramientas —que se pide en
    el mismo arranque, en la misma tarea— llegaba perfecto, así que /salud
    informaba «13 herramientas, consultas abiertas» mientras toda llamada real
    moría. Un diagnóstico que se ve sano y no lo está es la peor forma de
    fallar, y es justo lo que Brújula existe para no hacer.

    Abrir y cerrar por consulta cuesta una fracción de segundo frente a los
    segundos que tarda el modelo, y de paso arregla algo que costaba caro: el
    servidor MCP se puede reiniciar sin dejar al orquestador hablándole a una
    sesión muerta hasta que alguien lo reinicie a mano.
    """

    def __init__(self, url: str = MCP_URL) -> None:
        self.url = url
        self._catalogo: list[dict[str, Any]] = []

    @asynccontextmanager
    async def sesion(self):
        """Una sesión MCP viva mientras dure el bloque. Se abre y se cierra
        dentro de la misma tarea, que es la única forma en que anyio lo
        permite."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(self.url) as (lectura, escritura):
            async with ClientSession(lectura, escritura) as ses:
                await ses.initialize()
                yield ses

    async def abrir(self) -> None:
        """Pide el catálogo. No deja nada abierto."""
        async with self.sesion() as ses:
            listado = await ses.list_tools()
            self._catalogo = [_esquema_a_openai(h) for h in listado.tools]
        LOG.info("MCP responde: %s herramientas", len(self._catalogo))

    async def reintentar(self) -> bool:
        """Vuelve a pedir el catálogo si quedó vacío porque el MCP no estaba.
        Así el orquestador se recupera solo cuando el otro vuelve."""
        if self._catalogo:
            return True
        try:
            await self.abrir()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("El servidor MCP sigue sin responder: %s", exc)
        return bool(self._catalogo)

    async def una(self, nombre: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        """Una sola herramienta, sin modelo: abre sesión, llama, cierra."""
        async with self.sesion() as ses:
            return await self.llamar(ses, nombre, argumentos, False)

    async def territorios(self) -> dict[str, list[str]]:
        """
        El mapa completo: cada departamento con sus municipios.

        La página necesita esto para armar los dos desplegables, y no puede
        salir de una consulta normal: preguntarle al modelo «dame la lista de
        municipios» costaría una llamada al modelo cada vez que alguien abre la
        página, para devolver algo que no cambia entre cortes.

        Son 33 consultas a DuckDB sobre una sola sesión, de milisegundos cada
        una. El resultado se guarda en memoria del lado del orquestador.
        """
        async with self.sesion() as ses:
            lista = await self.llamar(ses, "listar_departamentos", {}, False)
            mapa: dict[str, list[str]] = {}
            for fila in lista.get("departamentos", []):
                nombre = (fila or {}).get("departamento")
                if not nombre:
                    continue
                municipios = await self.llamar(
                    ses, "listar_municipios", {"departamento": nombre}, False)
                mapa[nombre] = [m["municipio"]
                                for m in municipios.get("municipios", [])
                                if (m or {}).get("municipio")]
        return mapa

    async def cerrar(self) -> None:
        return None

    @property
    def catalogo(self) -> list[dict[str, Any]]:
        return self._catalogo

    async def llamar(self, sesion, nombre: str, argumentos: dict[str, Any],
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

        try:
            resultado = await sesion.call_tool(nombre, argumentos)
        except Exception as exc:  # noqa: BLE001
            # Sin este registro, un fallo de herramienta llega al modelo como
            # un texto suelto, el modelo lo cuenta a su manera y en los logs no
            # queda nada. Se pierde una tarde averiguando qué pasó.
            LOG.exception("La herramienta %s falló con %s", nombre, argumentos)
            return {"encontrado": False,
                    "motivo": f"La herramienta {nombre} no pudo ejecutarse."}

        textos = [c.text for c in resultado.content if getattr(c, "text", None)]
        if getattr(resultado, "isError", False):
            LOG.error("La herramienta %s devolvió error con %s: %s",
                      nombre, argumentos, (textos[0] if textos else "")[:400])
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
                    acceso_completo: bool = False,
                    sede: str = "", cod_sede: str = "") -> dict[str, Any]:
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
    if sede:
        # Con sede elegida, la pregunta es sobre ESA sede. Se le da al modelo
        # el código DANE para que use ficha_colegio sin adivinar entre
        # homónimas, y se le dice que el municipio queda como referencia.
        ambito.append(f"Sede elegida: {sede}" + (f" (código DANE {cod_sede})" if cod_sede else "")
                      + ". Responde sobre esta sede con ficha_colegio; el municipio y el "
                        "departamento son su referencia de comparación.")
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

    # Una sola sesión MCP para toda la consulta: se abre aquí y se cierra
    # al salir, en esta misma tarea. Es la condición que anyio impone y que
    # la versión anterior rompía sin que nada lo dijera.
    async with herramientas.sesion() as sesion:
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
                    sesion, llamada.function.name, argumentos, acceso_completo
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


def correo_de_aviso(datos: dict, revocar: str) -> str:
    """El aviso que llega a la fundación cuando alguien se registra."""
    def limpio(t: str) -> str:
        return (str(t).replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;").replace('"', "&quot;"))
    return f"""\
<p>Nuevo acceso a Br&uacute;jula Educativa.</p>
<table cellpadding="5" style="border-collapse:collapse">
  <tr><td><b>Nombre</b></td><td>{limpio(datos.get("nombre", ""))}</td></tr>
  <tr><td><b>Organizaci&oacute;n</b></td><td>{limpio(datos.get("organizacion", ""))}</td></tr>
  <tr><td><b>Correo</b></td><td>{limpio(datos.get("correo", ""))}</td></tr>
  <tr><td valign="top"><b>Para qu&eacute;</b></td><td>{limpio(datos.get("proposito", ""))}</td></tr>
</table>
<p>Ya puede consultar sin l&iacute;mite. Si algo no cuadra,
   <a href="{revocar}">revoca este acceso</a>; la persona vuelve al cupo de diez
   consultas libres y puede registrarse de nuevo.</p>
<p style="color:#56527A;font-size:13px">
  Aviso autom&aacute;tico. Autoriz&oacute; el tratamiento de sus datos conforme a la
  pol&iacute;tica de privacidad de la Fundaci&oacute;n Startin.
</p>"""


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

    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field

    import proxy

    class Consulta(BaseModel):
        pregunta: str = Field(min_length=3, max_length=500)
        departamento: str = ""
        municipio: str = ""
        # Opcional: una sede concreta. Vacío significa «todo el municipio».
        sede: str = Field(default="", max_length=160)
        cod_sede: str = Field(default="", max_length=20, pattern=r"^[0-9]*$")
        visitante: str = ""

    class Registro(BaseModel):
        nombre: str = Field(min_length=2, max_length=80)
        organizacion: str = Field(min_length=2, max_length=120)
        # Se valida con un patrón y no con EmailStr de Pydantic. EmailStr
        # arrastra el paquete email-validator, y su ausencia no se nota al
        # importar: revienta al construir el esquema, con el contenedor ya
        # arrancando. Eso dejó una revisión entera sin levantar. Para lo que
        # necesitamos —descartar erratas antes de gastar un envío— basta esto;
        # la validación de verdad la hace el servidor de correo al entregar.
        correo: str = Field(min_length=5, max_length=120,
                            pattern=r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
        proposito: str = Field(min_length=10, max_length=400)
        autoriza: bool = False
        politica: str = ""

    def _nivel(portero: Any, visitante: str) -> str:
        """El nivel de la credencial: "" (libre), "registrado" o "interno".
        Fallar aquí no puede tumbar la consulta: en la duda, visitante libre."""
        if not visitante:
            return ""
        try:
            return portero.almacen.nivel_de(visitante) or ""
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo comprobar la acreditación")
            return ""

    def _acreditado(portero: Any, visitante: str) -> bool:
        return bool(_nivel(portero, visitante))

    def _estado_visitante(portero: Any, visitante: str, registrado: bool, nivel: str = "") -> dict:
        if registrado:
            return {"registrado": True, "restantes": None, "nivel": nivel or "registrado"}
        try:
            usadas = portero.almacen.preguntas_usadas(visitante) if visitante else 0
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudieron leer las consultas usadas")
            usadas = 0
        return {"registrado": False, "nivel": "",
                "restantes": max(0, proxy.PREGUNTAS_LIBRES - usadas)}

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
        allow_headers=["content-type", "x-brujula-acceso"],
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
        # Si el catálogo quedó vacío porque el MCP no estaba en pie cuando
        # arrancamos, se reintenta aquí en vez de exigir un reinicio a mano.
        if not estado["herramientas"].catalogo:
            await estado["herramientas"].reintentar()

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

        # Sin esta línea, registrarse no servía para nada. El portero sabe
        # saltarse el tope de diez consultas para quien se registró —recibe un
        # parámetro `registrado` para eso—, pero aquí nunca se lo pasábamos:
        # la credencial llegaba, se guardaba en el navegador, y el tope seguía
        # cayendo igual a la consulta once. Quien se toma el trabajo de contar
        # quién es y para qué merece que eso tenga efecto.
        nivel = _nivel(portero, visitante)
        registrado = bool(nivel)
        # Solo el acceso interno abre el país entero y se salta los topes.
        acceso_completo = nivel == "interno"

        veredicto = portero.evaluar(
            consulta.pregunta, ip, visitante,
            consulta.departamento, consulta.municipio,
            acceso_completo=acceso_completo,
            registrado=registrado, sede=consulta.sede,
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
                acceso_completo=acceso_completo,
                sede=consulta.sede, cod_sede=consulta.cod_sede,
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
            sede=consulta.sede, registrado=registrado,
        )
        LOG.info("Consulta atendida: %s vueltas, %.4f USD", resultado["vueltas"], usd)
        return JSONResponse({**resultado, "desde_cache": False,
                             **_estado_visitante(portero, visitante, registrado, nivel)})

    @app.get("/territorios")
    async def territorios():
        """
        Los departamentos y municipios que hay en el corte vigente.

        Existe porque la página traía la lista escrita a mano —dos
        departamentos y diez municipios, de cuando era una maqueta—. Alguien de
        Nariño abría Brújula, no encontraba su departamento y se iba con la
        idea de que la herramienta no cubre su territorio. Cubre los 1.100 y
        pico municipios del país; lo que faltaba era decirlo.
        """
        guardado = estado.get("territorios")
        if guardado and time.time() - guardado["cuando"] < 6 * 3600:
            return JSONResponse(guardado["mapa"],
                                headers={"cache-control": "public, max-age=3600"})

        if not estado["herramientas"].catalogo:
            await estado["herramientas"].reintentar()
        try:
            mapa = await estado["herramientas"].territorios()
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo armar la lista de territorios")
            # Sin lista, la página se queda con la suya: es mejor que un
            # desplegable vacío.
            return JSONResponse({}, status_code=503)

        if mapa:
            estado["territorios"] = {"mapa": mapa, "cuando": time.time()}
        return JSONResponse(mapa, headers={"cache-control": "public, max-age=3600"})

    @app.get("/ficha")
    async def ficha(departamento: str = "", municipio: str = ""):
        """
        El contexto de un municipio, sin modelo y sin gastar cupo.

        Cuando alguien elige su territorio, antes de preguntar nada, ya debería
        ver dónde queda, cuánta gente en edad escolar tiene, de qué vive el
        departamento y a qué distancia están las capitales. Todo eso lo trae
        la herramienta ficha_municipio desde hace tiempo, pero solo salía si
        alguien gastaba una de sus consultas en pedirlo. Orientarse no debería
        costar una pregunta.
        """
        dep, mun = departamento.strip(), municipio.strip()
        if not dep or not mun:
            return JSONResponse({"motivo": "Faltan departamento y municipio."}, status_code=400)

        clave = f"{dep.casefold()}|{mun.casefold()}"
        fichas = estado.setdefault("fichas", {})
        guardada = fichas.get(clave)
        if guardada and time.time() - guardada["cuando"] < 6 * 3600:
            return JSONResponse(guardada["datos"], headers={"cache-control": "public, max-age=1800"})

        if not estado["herramientas"].catalogo:
            await estado["herramientas"].reintentar()
        try:
            datos = await estado["herramientas"].una(
                "ficha_municipio", {"departamento": dep, "municipio": mun})
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo armar la ficha de %s, %s", mun, dep)
            return JSONResponse({"motivo": "El servidor de datos no respondió."}, status_code=503)

        if datos.get("encontrado"):
            if len(fichas) > 1500:      # sin esto crecería para siempre
                fichas.clear()
            fichas[clave] = {"datos": datos, "cuando": time.time()}
        return JSONResponse(datos, status_code=200 if datos.get("encontrado") else 404,
                            headers={"cache-control": "public, max-age=1800"})

    @app.get("/colegios")
    async def colegios(departamento: str = "", municipio: str = ""):
        """
        Las sedes de un municipio, para el tercer desplegable. Sin modelo.

        Solo aparecen las que tienen resultados en Saber 11 —es la única fuente
        abierta con datos por sede—, así que una escuela de primaria no va a
        estar. La página lo dice al lado del selector; aquí solo se listan.
        """
        dep, mun = departamento.strip(), municipio.strip()
        if not dep or not mun:
            return JSONResponse({"motivo": "Faltan departamento y municipio."}, status_code=400)
        clave = f"{dep.casefold()}|{mun.casefold()}"
        listas = estado.setdefault("colegios", {})
        guardada = listas.get(clave)
        if guardada and time.time() - guardada["cuando"] < 6 * 3600:
            return JSONResponse(guardada["datos"], headers={"cache-control": "public, max-age=1800"})

        if not estado["herramientas"].catalogo:
            await estado["herramientas"].reintentar()
        try:
            # limite=0: todas las sedes. La herramienta trae un tope de 40
            # pensado para el modelo; el selector de la página necesita la
            # lista completa —Bogotá pasa de 700— o la persona no encuentra
            # su colegio y cree que no existe.
            datos = await estado["herramientas"].una(
                "colegios_del_municipio",
                {"departamento": dep, "municipio": mun, "orden": "nombre", "limite": 0})
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo listar las sedes de %s, %s", mun, dep)
            return JSONResponse({"motivo": "El servidor de datos no respondió."}, status_code=503)

        sedes = sorted(
            [{"cod": str(x.get("cod_dane_sede") or ""), "nombre": x.get("nombre") or "",
              "naturaleza": x.get("naturaleza") or "", "zona": x.get("zona") or "",
              "evaluados": x.get("evaluados"), "matricula": x.get("matricula")}
             for x in datos.get("sedes", []) if x.get("nombre")],
            key=lambda x: x["nombre"].casefold())
        salida = {"encontrado": bool(datos.get("encontrado")), "sedes": sedes,
                  "total": int(datos.get("total_sedes") or len(sedes)),
                  "fuente": "ICFES — Saber 11: solo colegios con estudiantes evaluados en grado 11."}
        if sedes:
            if len(listas) > 1500:
                listas.clear()
            listas[clave] = {"datos": salida, "cuando": time.time()}
        return JSONResponse(salida, headers={"cache-control": "public, max-age=1800"})


    # ------------------------------------------------------------------ #
    #  Administración. Solo con credencial de nivel «interno», que se
    #  obtiene confirmando un enlace enviado a un correo de la fundación.
    #  La credencial viaja en una cabecera, nunca en la URL: una URL acaba
    #  en el historial, en un pantallazo, en un mensaje reenviado.
    # ------------------------------------------------------------------ #

    def _exigir_interno(peticion: Request):
        credencial = (peticion.headers.get("x-brujula-acceso") or "").strip()
        if not credencial or _nivel(estado["portero"], credencial) != "interno":
            return JSONResponse({"motivo": "Este panel es solo para el equipo de la fundación. "
                                           "Entra con un enlace enviado a tu correo institucional."},
                                status_code=403)
        return None

    @app.get("/admin/resumen")
    async def admin_resumen(peticion: Request):
        """
        Quién se ha registrado y cuánto se está usando. Para el equipo.

        Junta lo que el portero ya cuenta —gasto, consultas, accesos— con la
        lista de registros. No hay una base de datos aparte de «analítica»:
        una herramienta que promete no rastrear a nadie no debería tenerla.
        """
        negado = _exigir_interno(peticion)
        if negado:
            return negado
        portero = estado["portero"]
        alm = portero.almacen
        hoy = datetime.now(timezone.utc).date()

        registros = []
        try:
            for r in alm.listar_registros():
                registros.append({
                    "ficha": r.get("ficha"),
                    "nombre": r.get("nombre"), "organizacion": r.get("organizacion"),
                    "correo": r.get("correo"), "proposito": r.get("proposito"),
                    "cuando": r.get("cuando"),
                    "estado": ("revocado" if r.get("revocado") else
                               "activo" if r.get("usado") else "sin confirmar"),
                    "nivel": nivel_para(r.get("correo", "")) if r.get("usado") else "",
                })
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudieron listar los registros")
        registros.sort(key=lambda r: r.get("cuando") or 0, reverse=True)

        dias = []
        for i in range(13, -1, -1):
            d = hoy - timedelta(days=i)
            clave = d.strftime("%Y-%m-%d")
            try:
                dias.append({"dia": clave,
                             "consultas": alm.consultas_del_dia(clave),
                             "usd": round(alm.gasto_del_dia(clave), 4)})
            except Exception:  # noqa: BLE001
                dias.append({"dia": clave, "consultas": None, "usd": None})

        try:
            accesos = alm.contar_acreditados()
        except Exception:  # noqa: BLE001
            accesos = {}
        try:
            gasto_mes = round(alm.gasto_del_mes(hoy.strftime("%Y-%m")), 4)
        except Exception:  # noqa: BLE001
            gasto_mes = None

        return JSONResponse({
            "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "registros": registros,
            "totales": {
                "registros": len(registros),
                "activos": sum(1 for r in registros if r["estado"] == "activo"),
                "sin_confirmar": sum(1 for r in registros if r["estado"] == "sin confirmar"),
                "revocados": sum(1 for r in registros if r["estado"] == "revocado"),
                "accesos_vigentes": accesos,
            },
            "uso": {
                "dias": dias,
                "gasto_mes_usd": gasto_mes,
                "presupuesto_mensual_usd": proxy.PRESUPUESTO_MENSUAL_USD,
                "presupuesto_diario_usd": round(portero.presupuesto_diario, 2),
                "herramientas": len(estado["herramientas"].catalogo),
                "modelo": MODELO,
            },
        })

    @app.post("/admin/revocar")
    async def admin_revocar(peticion: Request):
        """Revoca un acceso desde el panel. Misma operación que /revocar, otra puerta."""
        negado = _exigir_interno(peticion)
        if negado:
            return negado
        try:
            cuerpo = await peticion.json()
        except Exception:  # noqa: BLE001
            cuerpo = {}
        ficha = str((cuerpo or {}).get("ficha") or "")
        portero = estado["portero"]
        datos = portero.almacen.leer_registro(ficha) if ficha else None
        if not datos:
            return JSONResponse({"motivo": "No existe ese registro."}, status_code=404)
        credencial = datos.get("credencial")
        if credencial:
            portero.almacen.desacreditar(credencial)
        portero.almacen.guardar_registro(ficha, {**datos, "revocado": True, "credencial": None})
        LOG.info("Acceso revocado desde el panel: %s", datos.get("correo", "?"))
        return JSONResponse({"motivo": f"Acceso de {datos.get('correo', 'ese registro')} revocado."})

    @app.get("/estado")
    async def estado_de_visitante(v: str = "", peticion: Request = None):
        """
        En qué estado llega quien abre la página: cuántas consultas libres le
        quedan, o si ya se registró.

        Existe por una razón concreta: sin esto, alguien que acaba de registrarse
        y entra por el enlace del correo veía «10 consultas libres» en pantalla,
        que es justo lo que su registro dejó de ser. Una cifra equivocada en el
        sitio más visible de la página hace dudar de todo lo demás.
        """
        portero = estado["portero"]
        quien = v or (_ip_del_cliente(peticion) if peticion else "")
        nivel = _nivel(portero, quien)
        return JSONResponse(_estado_visitante(portero, quien, bool(nivel), nivel))

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
                "correo": datos.correo,
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

        enlace = f"{API_PROPIA}/entrar?t={ficha}"
        try:
            enviar_correo(
                datos.correo,
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
        # El aviso a la fundación va después y no puede tumbar el registro:
        # que nuestro correo interno falle no es problema de quien se registró.
        try:
            enviar_correo(
                AVISOS,
                f"Brújula: nuevo acceso — {datos.organizacion}",
                correo_de_aviso(
                    {"nombre": datos.nombre, "organizacion": datos.organizacion,
                     "correo": datos.correo, "proposito": datos.proposito},
                    f"{API_PROPIA}/revocar?t={ficha}",
                ),
            )
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo avisar del registro a %s", AVISOS)

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
        nivel = nivel_para(datos.get("correo", ""))
        portero.almacen.acreditar(credencial, nivel=nivel, correo=datos.get("correo", ""))
        LOG.info("Acceso %s concedido a %s", nivel, datos.get("correo", "?"))
        # Se guarda junto al registro: sin esto, revocar sería imposible.
        portero.almacen.guardar_registro(t, {**datos, "usado": True,
                                             "credencial": credencial})
        return RedirectResponse(f"{SITIO}/consultar.html#acceso={credencial}",
                                status_code=303)


    @app.get("/revocar")
    async def revocar(t: str = ""):
        """
        Quita un acceso concedido. El enlace vive solo en el aviso interno.

        Revocar es deliberadamente poco dramático: la persona vuelve al cupo de
        diez consultas libres y puede registrarse otra vez. No es un castigo,
        es deshacer.
        """
        from fastapi.responses import PlainTextResponse

        portero = estado["portero"]
        try:
            datos = portero.almacen.leer_registro(t) if t else None
        except Exception:  # noqa: BLE001
            LOG.exception("No se pudo leer el registro al revocar")
            datos = None

        if not datos:
            return PlainTextResponse("Ese enlace de revocación no corresponde a "
                                     "ningún acceso.", status_code=404)

        credencial = datos.get("credencial")
        if credencial:
            portero.almacen.desacreditar(credencial)
        portero.almacen.guardar_registro(t, {**datos, "revocado": True,
                                             "credencial": None})
        quien = datos.get("correo", "ese acceso")
        LOG.info("Acceso revocado: %s (%s)", quien, datos.get("organizacion", ""))
        return PlainTextResponse(
            f"Listo. El acceso de {quien} quedó revocado: vuelve al cupo de diez "
            f"consultas libres y puede registrarse de nuevo si hace falta."
        )


    return app


# `uvicorn orquestador:app` necesita el objeto ya construido. La conexión al
# MCP y al modelo no ocurre aquí sino en el ciclo de vida, cuando el servidor
# arranca: así un fallo de red al importar no deja un módulo a medio cargar.
app = crear_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PUERTO", "8000")))
