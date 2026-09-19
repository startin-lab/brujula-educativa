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
    pip install fastapi uvicorn httpx azure-data-tables
    export BRUJULA_PRESUPUESTO_MENSUAL_USD=300
    export AZURE_STORAGE_CONNECTION_STRING=...
    uvicorn proxy:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
                acceso_completo: bool = False) -> Veredicto:
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
        if not acceso_completo:
            usadas = self.almacen.preguntas_usadas(visitante)
            if usadas >= PREGUNTAS_LIBRES:
                return Veredicto(
                    False,
                    f"Usaste tus {PREGUNTAS_LIBRES} consultas libres. Cuéntanos "
                    "de qué organización eres y para qué usarás los datos, y te "
                    "damos acceso completo.",
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

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    p = Portero()
    print(json.dumps(p.estado(), ensure_ascii=False, indent=2))
