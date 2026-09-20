#!/usr/bin/env python3
"""
Brújula Educativa — Proxy de control de costo
Fundación Startin

Se para entre el front público y el agente. Todo lo que el agente cuesta pasa
por aquí, y aquí es donde se corta.

POR QUÉ ESTE ARCHIVO EXISTE

  Azure NO tiene un freno duro por dólares. Los presupuestos de Cost Management
  avisan por correo cuando ya gastaste; no detienen nada. Para un servicio
  público y gratuito eso es inaceptable: basta que alguien lo comparta en un
  grupo de WhatsApp de 5.000 docentes para que la factura de un mes se vaya en
  una tarde, sin que nadie se entere hasta el corte.

  El tope real se arma por fuera, en capas, y cada capa ataja un tipo distinto
  de gasto:

    1. CACHÉ.        Misma pregunta, mismo territorio, misma respuesta. En una
                     herramienta pública la mayoría de las preguntas se repiten,
                     y los datos solo cambian una vez al mes. Es la capa que más
                     ahorra y la única que además hace el servicio más rápido.
    2. LÍMITE POR IP. Frena al que hace 500 preguntas seguidas, sea un script o
                     un entusiasta.
    3. CUOTA DEL VISITANTE. Las preguntas libres antes de pedir registro.
    4. PRESUPUESTO DIARIO. El freno duro. Si el día se agota, el proxy deja de
                     llamar al modelo. No degrada, no encola: responde que hoy
                     no hay más y ofrece lo que tenga en caché.

  El presupuesto se lleva EN DÓLARES, no en tokens, porque el límite que puso la
  fundación está en dólares. Convertir es trabajo del proxy, no de quien decide.

FALLA CERRADO

  Si el almacenamiento de contadores no responde, el proxy NO deja pasar la
  petición «por si acaso». Un contador caído significa que no sabemos cuánto
  llevamos gastado, y gastar sin saber es exactamente lo que este archivo
  existe para impedir. Ante la duda, no se llama al modelo.

Uso:
    pip install fastapi uvicorn httpx azure-data-tables azure-identity
    export BRUJULA_PRESUPUESTO_MENSUAL_USD=300
    export AZURE_STORAGE_CUENTA=...          # o AZURE_STORAGE_CONNECTION_STRING
    uvicorn proxy:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

LOG = logging.getLogger("proxy")

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

def _num(nombre: str, defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, defecto))
    except ValueError:
        LOG.warning("%s no es numérico; se usa %s", nombre, defecto)
        return defecto


# El tope que fijó la fundación. Todo lo demás se deriva de aquí.
PRESUPUESTO_MENSUAL_USD = _num("BRUJULA_PRESUPUESTO_MENSUAL_USD", 300.0)

# Se reparte por día en vez de dejar gastar todo el mes en una tarde. Un pico de
# tráfico un martes no puede dejar el servicio muerto hasta fin de mes.
DIAS_DEL_MES = 30
PRESUPUESTO_DIARIO_USD = PRESUPUESTO_MENSUAL_USD / DIAS_DEL_MES

# Reserva: no se gasta el 100 % del día. El último 15 % queda para que las
# respuestas en caché y los mensajes de error sigan funcionando, y para absorber
# el desfase entre lo estimado y lo real de la última petición.
FRACCION_UTILIZABLE = 0.85

# Precio por millón de tokens. Se configura porque cambia, y porque el modelo
# puede cambiar. Si estos números están mal, el tope está mal: son el único
# punto donde el proxy traduce consumo a dinero.
USD_POR_MILLON_ENTRADA = _num("BRUJULA_USD_POR_MILLON_ENTRADA", 3.0)
USD_POR_MILLON_SALIDA = _num("BRUJULA_USD_POR_MILLON_SALIDA", 15.0)

PREGUNTAS_LIBRES = int(_num("BRUJULA_PREGUNTAS_LIBRES", 10))
LIMITE_IP_POR_HORA = int(_num("BRUJULA_LIMITE_IP_HORA", 30))

# Los datos se refrescan una vez al mes: una respuesta de hace una semana sigue
# siendo la respuesta correcta. Siete días es conservador a propósito.
CACHE_DIAS = int(_num("BRUJULA_CACHE_DIAS", 7))

# Estimación previa a la llamada. Se usa solo para decidir si ARRANCAR la
# petición; después se reconcilia con el consumo real. Se estima alto para que
# el error vaya siempre hacia no gastar de más.
COSTO_ESTIMADO_USD = _num("BRUJULA_COSTO_ESTIMADO_USD", 0.05)


def costo_usd(tokens_entrada: int, tokens_salida: int) -> float:
    return (tokens_entrada / 1e6) * USD_POR_MILLON_ENTRADA + \
           (tokens_salida / 1e6) * USD_POR_MILLON_SALIDA


# --------------------------------------------------------------------------- #
# Normalización de preguntas para la caché
# --------------------------------------------------------------------------- #

def normalizar_pregunta(texto: str) -> str:
    """
    Dos personas que preguntan lo mismo con distinta puntuación deben compartir
    la respuesta en caché. No se intenta entender la pregunta —eso costaría una
    llamada al modelo, que es justo lo que se quiere evitar—: se normaliza la
    forma y se acepta que «cobertura en Soacha» y «cuál es la cobertura de
    Soacha» sean entradas distintas.
    """
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clave_cache(pregunta: str, departamento: str, municipio: str) -> str:
    """
    El territorio ENTRA en la clave. Sin él, «¿cómo va la cobertura?» en Soacha
    devolvería la respuesta guardada para Leticia, que es el peor error posible
    en una herramienta de diagnóstico territorial.
    """
    crudo = "|".join([
        normalizar_pregunta(pregunta),
        normalizar_pregunta(departamento),
        normalizar_pregunta(municipio),
    ])
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #

@dataclass
class Veredicto:
    """Qué hacer con una petición y por qué. El motivo se le muestra al usuario."""
    permitir: bool
    motivo: str = ""
    codigo: int = 200
    desde_cache: bool = False
    respuesta: dict | None = None
    espera_segundos: int | None = None


class AlmacenMemoria:
    """
    Contadores en memoria. Sirve para pruebas y para una sola instancia.

    NO SIRVE EN PRODUCCIÓN CON VARIAS RÉPLICAS: cada réplica llevaría su propio
    contador y el tope diario se multiplicaría por el número de réplicas. Para
    eso está AlmacenTablas, que comparte el estado en Azure Table Storage.
    """

    def __init__(self) -> None:
        self._registros: dict[str, dict] = {}
        self._acreditados: set[str] = set()
        self.gasto: dict[str, float] = {}      # día -> USD
        self.peticiones: dict[str, list[float]] = {}   # ip -> timestamps
        self.usadas: dict[str, int] = {}       # visitante -> preguntas
        self.cache: dict[str, tuple[float, dict]] = {}

    def gasto_del_dia(self, dia: str) -> float:
        return self.gasto.get(dia, 0.0)

    def sumar_gasto(self, dia: str, usd: float) -> None:
        self.gasto[dia] = self.gasto.get(dia, 0.0) + usd

    def peticiones_recientes(self, ip: str, ventana_s: int) -> int:
        ahora = time.time()
        recientes = [t for t in self.peticiones.get(ip, []) if ahora - t < ventana_s]
        self.peticiones[ip] = recientes
        return len(recientes)

    def registrar_peticion(self, ip: str) -> None:
        self.peticiones.setdefault(ip, []).append(time.time())

    def preguntas_usadas(self, visitante: str) -> int:
        return self.usadas.get(visitante, 0)

    def sumar_pregunta(self, visitante: str) -> None:
        self.usadas[visitante] = self.usadas.get(visitante, 0) + 1

    # -- registro y acreditación ------------------------------------------ #

    def guardar_registro(self, ficha: str, datos: dict) -> None:
        self._registros[ficha] = datos

    def leer_registro(self, ficha: str) -> dict | None:
        return self._registros.get(ficha)

    def marcar_registro_usado(self, ficha: str) -> None:
        if ficha in self._registros:
            self._registros[ficha] = {**self._registros[ficha], "usado": True}

    def acreditar(self, visitante: str) -> None:
        self._acreditados.add(visitante)

    def esta_acreditado(self, visitante: str) -> bool:
        return visitante in self._acreditados

    def desacreditar(self, visitante: str) -> None:
        self._acreditados.discard(visitante)

    def leer_cache(self, clave: str) -> dict | None:
        entrada = self.cache.get(clave)
        if not entrada:
            return None
        guardado, respuesta = entrada
        if time.time() - guardado > CACHE_DIAS * 86400:
            del self.cache[clave]
            return None
        return respuesta

    def guardar_cache(self, clave: str, respuesta: dict) -> None:
        self.cache[clave] = (time.time(), respuesta)


# --------------------------------------------------------------------------- #
# Almacén compartido (Azure Table Storage)
# --------------------------------------------------------------------------- #

class ConflictoDeVersion(Exception):
    """Otra réplica escribió primero. Hay que releer y reintentar."""


# El SDK de Azure se importa si está; si no, se usan equivalentes inertes.
#
# No es un capricho: las pruebas de concurrencia —las únicas que demuestran que
# el contador de gasto no pierde sumas— tienen que poder correr en cualquier
# máquina y en CI, sin credenciales ni paquetes de nube. Un almacén que solo se
# puede probar contra el servicio real termina sin probarse.
try:
    from azure.core import MatchConditions as _MatchConditions
    from azure.data.tables import UpdateMode as _UpdateMode
    _SDK_AZURE = True
except ImportError:  # pragma: no cover
    class _MatchConditions:  # type: ignore[no-redef]
        IfNotModified = "IfNotModified"

    class _UpdateMode:  # type: ignore[no-redef]
        REPLACE = "replace"

    _SDK_AZURE = False


class AlmacenTablas:
    """
    Contadores compartidos entre réplicas, sobre Azure Table Storage.

    POR QUÉ NO BASTA CON GUARDAR EN ALGÚN LADO

      El problema de un contador de gasto repartido no es la persistencia: es la
      concurrencia. Dos réplicas que atienden una petición cada una leen «llevo
      3,00 USD», suman lo suyo y escriben. La segunda pisa a la primera y uno de
      los dos gastos DESAPARECE del contador. Con suficiente tráfico —que es
      justo cuando el tope importa— el contador se queda muy por debajo del
      gasto real y el freno nunca salta.

      La solución es la actualización condicional: se lee el valor con su ETag y
      se escribe exigiendo que el ETag no haya cambiado. Si cambió, otra réplica
      llegó primero, y hay que releer y volver a intentar. Es lo que hace
      `_sumar_atomico`, y es la única parte de este archivo donde un error
      silencioso cuesta dinero de verdad.

    LÍMITE QUE HAY QUE CONOCER

      Una entidad de Table Storage admite 64 KB por propiedad y 1 MB en total.
      Una respuesta del agente con varias visualizaciones puede pasarse. Cuando
      eso ocurre NO se cachea y se anota: es preferible pagar esa respuesta cada
      vez a inventar un truncamiento que devuelva datos incompletos.
    """

    LIMITE_CACHE_BYTES = 60_000
    REINTENTOS_ESCRITURA = 8

    # El contador del día se reparte en varias filas.
    #
    # POR QUÉ NO BASTA CON REINTENTAR. Cada petición suma al gasto del mismo
    # día: con varias réplicas, todas escriben en la MISMA fila y se pisan entre
    # sí. Medido con ocho hilos y 200 sumas sobre una sola fila: 478 conflictos
    # de ETag, y con reintentos a secas algunos hilos se rinden y se pierde el
    # 28 % del gasto. Un contador que pierde gasto no frena nada.
    #
    # Repartir el contador en FRAGMENTOS quita la contención de raíz: cada
    # escritura cae en una fila al azar, así que dos réplicas casi nunca compiten
    # por la misma. Leer cuesta una consulta por rango en vez de una lectura
    # puntual, que es un precio bajo por un contador que no miente.
    FRAGMENTOS = 16

    def __init__(self, cliente_tabla=None, cadena_conexion: str | None = None,
                 prefijo: str = "brujula") -> None:
        if cliente_tabla is not None:
            self._tabla = cliente_tabla
            return
        if not _SDK_AZURE:
            raise RuntimeError("Falta azure-data-tables: pip install azure-data-tables")
        from azure.data.tables import TableServiceClient  # noqa: PLC0415

        # Dos formas de identificarse, y se prefiere la primera:
        #   1. AZURE_STORAGE_CUENTA + identidad administrada: sin claves.
        #   2. AZURE_STORAGE_CONNECTION_STRING: una clave con permiso total.
        cuenta = os.environ.get("AZURE_STORAGE_CUENTA", "")
        cadena = cadena_conexion or os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        if cuenta and not cadena_conexion:
            from azure.identity import DefaultAzureCredential  # noqa: PLC0415

            servicio = TableServiceClient(
                endpoint=f"https://{cuenta}.table.core.windows.net",
                credential=DefaultAzureCredential(),
            )
        elif cadena:
            servicio = TableServiceClient.from_connection_string(cadena)
        else:
            raise RuntimeError(
                "AlmacenTablas necesita AZURE_STORAGE_CUENTA (identidad) o "
                "AZURE_STORAGE_CONNECTION_STRING. Sin almacén compartido, cada "
                "réplica llevaría su propio tope."
            )
        nombre = f"{prefijo}contadores"
        try:
            servicio.create_table(nombre)
        except Exception:  # noqa: BLE001
            pass  # ya existía
        self._tabla = servicio.get_table_client(nombre)

    # -- primitivas ------------------------------------------------------- #

    @staticmethod
    def _es(exc: Exception, *marcas: str) -> bool:
        """
        Reconoce el tipo de error mirando el nombre de la clase Y el mensaje.

        El SDK de Azure lanza `ResourceNotFoundError`; una tabla simulada o una
        versión distinta del SDK pueden lanzar otra cosa con el mismo sentido.
        Mirar solo el nombre de la clase haría que un «no existe» se propagara
        como error real y el proxy respondiera 503 con la tabla sana.
        """
        texto = f"{type(exc).__name__} {exc}"
        return any(m in texto for m in marcas)

    def _leer(self, particion: str, clave: str) -> dict | None:
        try:
            return dict(self._tabla.get_entity(particion, clave))
        except Exception as exc:  # noqa: BLE001
            if self._es(exc, "ResourceNotFound", "404", "no existe"):
                return None
            raise

    @staticmethod
    def _etiqueta(entidad) -> str | None:
        """
        El etag de una fila, que es lo que permite escribir sin pisar a otro.

        Está aquí porque cuesta encontrarlo: el SDK no lo devuelve como una
        columna más, sino colgado en `.metadata`. Convertir la entidad a dict
        —que es lo natural y lo que hacía `_leer`— lo tira a la basura sin
        decir nada, y la escritura condicionada falla después con un mensaje
        que habla de otra cosa: «IfNotModified must be specified with etag».
        """
        meta = getattr(entidad, "metadata", None) or {}
        return (meta.get("etag") or meta.get("odata.etag")
                or entidad.get("odata.etag") or entidad.get("etag"))

    def _leer_con_etiqueta(self, particion: str, clave: str):
        """Como _leer, pero además devuelve el etag. Lo usa la suma atómica."""
        try:
            entidad = self._tabla.get_entity(particion, clave)
        except Exception as exc:  # noqa: BLE001
            if self._es(exc, "ResourceNotFound", "404", "no existe"):
                return None, None
            raise
        return dict(entidad), self._etiqueta(entidad)

    def _sumar_atomico(self, particion: str, clave: str, campo: str, delta: float) -> float:
        """
        Suma sin perder actualizaciones. Relee y reintenta mientras otra réplica
        vaya ganando la carrera; si tras varios intentos no lo logra, LANZA en
        vez de escribir a ciegas. Un contador que miente es peor que uno que
        falla: el que falla hace que el proxy responda 503 y no gaste.
        """
        for intento in range(self.REINTENTOS_ESCRITURA):
            if intento:
                # Espera creciente con ruido: sin el ruido, las réplicas que
                # chocaron vuelven a chocar todas juntas en el mismo instante.
                time.sleep(random.uniform(0, 0.02 * (2 ** min(intento, 5))))
            actual, etiqueta = self._leer_con_etiqueta(particion, clave)
            if actual is None:
                nueva = {"PartitionKey": particion, "RowKey": clave, campo: delta}
                try:
                    self._tabla.create_entity(nueva)
                    return float(delta)
                except Exception as exc:  # noqa: BLE001
                    if self._es(exc, "ResourceExists", "409", "ya existe"):
                        continue   # otra réplica la creó primero; releer
                    raise
            valor = float(actual.get(campo, 0.0)) + delta
            actual[campo] = valor
            try:
                if etiqueta:
                    self._tabla.update_entity(
                        actual, mode=_UpdateMode.REPLACE, etag=etiqueta,
                        match_condition=_MatchConditions.IfNotModified,
                    )
                else:
                    # Sin etag no hay forma de condicionar la escritura, y
                    # escribir a ciegas es exactamente como un contador
                    # empieza a mentir. Esto no es una carrera perdida sino un
                    # problema de estructura, así que no se reintenta: se dice.
                    raise RuntimeError(
                        f"La fila {particion}/{clave} llegó sin etag; sin él no "
                        f"se puede sumar sin arriesgar perder actualizaciones."
                    )
                return valor
            except ConflictoDeVersion:
                continue
            except Exception as exc:  # noqa: BLE001
                if self._es(exc, "ResourceModified", "412"):
                    continue   # otra réplica ganó la carrera: releer y reintentar
                raise
        raise RuntimeError(
            f"No se pudo actualizar {particion}/{clave} tras "
            f"{self.REINTENTOS_ESCRITURA} intentos: demasiada concurrencia."
        )

    # -- interfaz que usa el Portero -------------------------------------- #

    def gasto_del_dia(self, dia: str) -> float:
        """Suma de todos los fragmentos del día, en una sola consulta por rango."""
        filtro = (f"PartitionKey eq 'gasto' and RowKey ge '{dia}-00' "
                  f"and RowKey le '{dia}-99'")
        total = 0.0
        for fila in self._tabla.query_entities(filtro):
            total += float(dict(fila).get("usd", 0.0))
        return total

    def sumar_gasto(self, dia: str, usd: float) -> None:
        fragmento = random.randrange(self.FRAGMENTOS)
        self._sumar_atomico("gasto", f"{dia}-{fragmento:02d}", "usd", usd)

    def peticiones_recientes(self, ip: str, ventana_s: int) -> int:
        # Se cuenta por hora en vez de guardar una lista de marcas de tiempo:
        # una fila por IP y hora, que además caduca sola al cambiar la hora.
        fila = self._leer("ip", self._clave_hora(ip))
        return int(fila.get("n", 0)) if fila else 0

    def registrar_peticion(self, ip: str) -> None:
        self._sumar_atomico("ip", self._clave_hora(ip), "n", 1)

    def preguntas_usadas(self, visitante: str) -> int:
        fila = self._leer("visitante", visitante)
        return int(fila.get("n", 0)) if fila else 0

    def sumar_pregunta(self, visitante: str) -> None:
        self._sumar_atomico("visitante", visitante, "n", 1)

    # -- registro y acreditación ------------------------------------------ #

    def guardar_registro(self, ficha: str, datos: dict) -> None:
        self._tabla.upsert_entity({
            "PartitionKey": "registro", "RowKey": ficha,
            "json": json.dumps(datos, ensure_ascii=False), "guardado": time.time(),
        })

    def leer_registro(self, ficha: str) -> dict | None:
        fila = self._leer("registro", ficha)
        if not fila:
            return None
        try:
            return json.loads(fila["json"])
        except (KeyError, ValueError):
            return None

    def marcar_registro_usado(self, ficha: str) -> None:
        datos = self.leer_registro(ficha)
        if datos is not None:
            self.guardar_registro(ficha, {**datos, "usado": True})

    def acreditar(self, visitante: str) -> None:
        self._tabla.upsert_entity({
            "PartitionKey": "acreditado", "RowKey": visitante,
            "desde": time.time(),
        })

    def esta_acreditado(self, visitante: str) -> bool:
        return self._leer("acreditado", visitante) is not None

    def desacreditar(self, visitante: str) -> None:
        try:
            self._tabla.delete_entity("acreditado", visitante)
        except Exception as exc:  # noqa: BLE001
            if not self._es(exc, "ResourceNotFound", "404", "no existe"):
                raise

    def leer_cache(self, clave: str) -> dict | None:
        fila = self._leer("cache", clave)
        if not fila:
            return None
        if time.time() - float(fila.get("guardado", 0)) > CACHE_DIAS * 86400:
            return None
        try:
            return json.loads(fila["json"])
        except (KeyError, ValueError):
            return None

    def guardar_cache(self, clave: str, respuesta: dict) -> None:
        texto = json.dumps(respuesta, ensure_ascii=False)
        if len(texto.encode("utf-8")) > self.LIMITE_CACHE_BYTES:
            LOG.info("Respuesta de %s bytes: no se cachea (límite de Table Storage). "
                     "Se pagará cada vez.", len(texto))
            return
        self._tabla.upsert_entity({
            "PartitionKey": "cache", "RowKey": clave,
            "json": texto, "guardado": time.time(),
        })

    @staticmethod
    def _clave_hora(ip: str) -> str:
        # La IP va sanitizada: ':' y '/' no son válidos en una RowKey.
        limpia = re.sub(r"[^A-Za-z0-9._-]", "_", ip)
        return f"{limpia}-{datetime.now(timezone.utc):%Y%m%d%H}"


# --------------------------------------------------------------------------- #
# Portero
# --------------------------------------------------------------------------- #

class Portero:
    """
    Decide si una petición llega al modelo. El orden de las capas importa: lo
    más barato de comprobar va primero, y la caché va antes que cualquier cuota
    porque una respuesta cacheada no gasta presupuesto ni debería consumirle al
    visitante una de sus preguntas libres.
    """

    def __init__(self, almacen=None, presupuesto_diario_usd: float | None = None) -> None:
        self.almacen = almacen or AlmacenMemoria()
        self.presupuesto_diario = (
            presupuesto_diario_usd
            if presupuesto_diario_usd is not None
            else PRESUPUESTO_DIARIO_USD * FRACCION_UTILIZABLE
        )

    @staticmethod
    def _hoy() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def evaluar(self, pregunta: str, ip: str, visitante: str,
                departamento: str = "", municipio: str = "",
                acceso_completo: bool = False,
                registrado: bool = False) -> Veredicto:
        clave = clave_cache(pregunta, departamento, municipio)

        # --- 1. Límite por IP ------------------------------------------- #
        # Va primero porque es lo único que frena un script, y un script puede
        # vaciar el presupuesto del día antes de que la caché se caliente.
        try:
            recientes = self.almacen.peticiones_recientes(ip, 3600)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Contador de IP no disponible: %s", exc)
            return Veredicto(False, "No podemos atender en este momento.", 503)

        if not acceso_completo and recientes >= LIMITE_IP_POR_HORA:
            return Veredicto(
                False,
                f"Has hecho {recientes} consultas en la última hora. "
                "Espera un rato e intenta de nuevo.",
                429,
                espera_segundos=600,
            )

        # --- 2. Caché ---------------------------------------------------- #
        # Antes de las cuotas: una respuesta guardada no cuesta nada, así que no
        # tiene por qué gastarle al visitante una de sus preguntas libres.
        try:
            guardada = self.almacen.leer_cache(clave)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Caché no disponible: %s", exc)
            guardada = None

        if guardada is not None:
            self.almacen.registrar_peticion(ip)
            return Veredicto(True, "Respuesta en caché", 200,
                             desde_cache=True, respuesta=guardada)

        # --- 3. Presupuesto del día -------------------------------------- #
        try:
            gastado = self.almacen.gasto_del_dia(self._hoy())
        except Exception as exc:  # noqa: BLE001
            # Falla cerrado: sin contador no sabemos cuánto llevamos.
            LOG.error("Contador de gasto no disponible: %s", exc)
            return Veredicto(False, "No podemos atender en este momento.", 503)

        if gastado + COSTO_ESTIMADO_USD > self.presupuesto_diario:
            return Veredicto(
                False,
                "Brújula alcanzó su límite de consultas por hoy. Es una "
                "herramienta gratuita con un presupuesto acotado. Vuelve mañana, "
                "o escríbenos a hola@startin.org.co si necesitas acceso continuo "
                "para un proyecto.",
                503,
            )

        # --- 4. Cuota del visitante -------------------------------------- #
        # Registrarse levanta el tope de las diez preguntas, pero NO abre las
        # consultas de país entero: eso sigue detrás del token nacional. Son
        # dos permisos distintos y conviene que no se confundan.
        if not acceso_completo and not registrado:
            usadas = self.almacen.preguntas_usadas(visitante)
            if usadas >= PREGUNTAS_LIBRES:
                return Veredicto(
                    False,
                    f"Usaste tus {PREGUNTAS_LIBRES} consultas libres. Cuéntanos "
                    "de qué organización eres y para qué usarás los datos y "
                    "seguimos: el registro está en la página de inicio.",
                    402,
                )

        return Veredicto(True, "", 200)

    def registrar_consumo(self, pregunta: str, ip: str, visitante: str,
                          respuesta: dict, tokens_entrada: int, tokens_salida: int,
                          departamento: str = "", municipio: str = "") -> float:
        """
        Después de la llamada: se anota lo que costó de verdad y se guarda la
        respuesta. Reconciliar con el consumo real —en vez de quedarse con la
        estimación— es lo que mantiene el contador pegado a la factura.
        """
        usd = costo_usd(tokens_entrada, tokens_salida)
        self.almacen.sumar_gasto(self._hoy(), usd)
        self.almacen.registrar_peticion(ip)
        self.almacen.sumar_pregunta(visitante)
        self.almacen.guardar_cache(clave_cache(pregunta, departamento, municipio), respuesta)
        return usd

    def estado(self) -> dict:
        gastado = self.almacen.gasto_del_dia(self._hoy())
        return {
            "fecha": self._hoy(),
            "presupuesto_mensual_usd": round(PRESUPUESTO_MENSUAL_USD, 2),
            "presupuesto_diario_usd": round(self.presupuesto_diario, 4),
            "gastado_hoy_usd": round(gastado, 4),
            "disponible_hoy_usd": round(max(0.0, self.presupuesto_diario - gastado), 4),
            "porcentaje_usado": round(gastado / self.presupuesto_diario * 100, 1)
                                if self.presupuesto_diario else None,
            "preguntas_libres": PREGUNTAS_LIBRES,
            "limite_ip_por_hora": LIMITE_IP_POR_HORA,
            "cache_dias": CACHE_DIAS,
        }


# --------------------------------------------------------------------------- #

def construir_portero() -> Portero:
    """
    Table Storage si hay cómo; memoria si no, avisando fuerte.

    El aviso importa: con contadores en memoria y varias réplicas, el tope
    diario se multiplica por el número de réplicas sin que nada lo indique.
    """
    if os.environ.get("AZURE_STORAGE_CUENTA") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        try:
            return Portero(almacen=AlmacenTablas())
        except Exception as exc:  # noqa: BLE001
            LOG.error("No se pudo usar Table Storage (%s). Se sigue en memoria.", exc)
    LOG.warning(
        "CONTADORES EN MEMORIA. Válido para una sola instancia. Con varias "
        "réplicas cada una llevaría su propio tope y el límite de gasto se "
        "multiplicaría en silencio."
    )
    return Portero()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    print(json.dumps(construir_portero().estado(), ensure_ascii=False, indent=2))
