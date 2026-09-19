#!/usr/bin/env python3
"""
Brújula Educativa — Pruebas del proxy de control de costo
Fundación Startin

Este es el archivo que decide si la factura se puede disparar. Las pruebas no
comprueban que el código corra: comprueban que **no se pueda gastar de más**,
que es distinto y más difícil.

Lo que se verifica:
  · el presupuesto diario corta de verdad, y corta ANTES de llamar al modelo
  · una respuesta en caché no gasta presupuesto ni consume cuota del visitante
  · el territorio entra en la clave de caché (si no, Soacha respondería Leticia)
  · el límite por IP frena un script
  · las preguntas libres se acaban
  · si el contador falla, NO se deja pasar la petición

Uso:
    python probar_proxy.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy as P  # noqa: E402

FALLOS: list[str] = []


def check(nombre: str, condicion: bool, detalle: object = "") -> None:
    print(f"  {'OK  ' if condicion else 'FALLA'}  {nombre}" + ("" if condicion else f"  ::  {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


class AlmacenRoto(P.AlmacenMemoria):
    """Simula el almacén caído: toda lectura de contador revienta."""

    def gasto_del_dia(self, dia):  # noqa: ANN001, ANN201
        raise RuntimeError("almacén no disponible")

    def peticiones_recientes(self, ip, ventana_s):  # noqa: ANN001, ANN201
        raise RuntimeError("almacén no disponible")


# --------------------------------------------------------------------------- #
# Tabla falsa: imita Azure Table Storage, incluidos los conflictos de ETag
# --------------------------------------------------------------------------- #

import random  # noqa: E402
import threading  # noqa: E402


class TablaFalsa:
    """
    Imita lo justo de Azure Table Storage para probar la concurrencia: entidades
    con ETag, y una escritura condicional que FALLA si el ETag cambió.

    El `sleep` aleatorio entre la lectura y la escritura no es adorno: sin él,
    los hilos casi nunca se solapan y la prueba pasaría aunque el código
    estuviera mal. Ensancha a propósito la ventana donde se pierde una
    actualización.
    """

    def __init__(self) -> None:
        self.filas: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self.conflictos = 0

    def get_entity(self, particion, clave):  # noqa: ANN001, ANN201
        with self._lock:
            fila = self.filas.get((particion, clave))
            if fila is None:
                raise LookupError("ResourceNotFoundError: no existe")
            return dict(fila)

    def create_entity(self, entidad):  # noqa: ANN001, ANN201
        with self._lock:
            k = (entidad["PartitionKey"], entidad["RowKey"])
            if k in self.filas:
                raise ValueError("ResourceExistsError: ya existe")
            self.filas[k] = {**entidad, "etag": "v1"}

    def update_entity(self, entidad, mode=None, etag=None, match_condition=None):  # noqa: ANN001, ANN201, ARG002
        time.sleep(random.uniform(0, 0.004))   # ensancha la ventana de carrera
        with self._lock:
            k = (entidad["PartitionKey"], entidad["RowKey"])
            actual = self.filas.get(k)
            if actual is None:
                raise LookupError("ResourceNotFoundError: no existe")
            if etag is not None and actual["etag"] != etag:
                self.conflictos += 1
                raise RuntimeError("ResourceModifiedError 412: el ETag cambió")
            version = int(actual["etag"][1:]) + 1
            self.filas[k] = {**entidad, "etag": f"v{version}"}

    def query_entities(self, filtro):  # noqa: ANN001, ANN201
        """Solo entiende el filtro que usa el proxy: partición + rango de RowKey."""
        import re as _re
        m = _re.search(r"PartitionKey eq '([^']+)'", filtro)
        desde = _re.search(r"RowKey ge '([^']+)'", filtro)
        hasta = _re.search(r"RowKey le '([^']+)'", filtro)
        with self._lock:
            return [dict(f) for (pk, rk), f in self.filas.items()
                    if (not m or pk == m.group(1))
                    and (not desde or rk >= desde.group(1))
                    and (not hasta or rk <= hasta.group(1))]

    def upsert_entity(self, entidad):  # noqa: ANN001, ANN201
        with self._lock:
            k = (entidad["PartitionKey"], entidad["RowKey"])
            version = int(self.filas.get(k, {}).get("etag", "v0")[1:]) + 1
            self.filas[k] = {**entidad, "etag": f"v{version}"}


def probar_almacen_compartido() -> None:
    print("\n== 10. Almacén compartido: no se pierden actualizaciones ==")
    tabla = TablaFalsa()
    almacen = P.AlmacenTablas(cliente_tabla=tabla)

    check("arranca en cero", almacen.gasto_del_dia("2026-09-19") == 0.0)
    almacen.sumar_gasto("2026-09-19", 1.25)
    check("suma sobre una fila nueva", almacen.gasto_del_dia("2026-09-19") == 1.25)
    almacen.sumar_gasto("2026-09-19", 0.75)
    check("suma sobre una fila existente", almacen.gasto_del_dia("2026-09-19") == 2.0)

    # Lo que de verdad importa: varias réplicas sumando a la vez.
    HILOS, POR_HILO, MONTO = 8, 25, 0.01
    errores: list[Exception] = []

    def replica() -> None:
        for _ in range(POR_HILO):
            try:
                almacen.sumar_gasto("2026-09-20", MONTO)
            except Exception as exc:  # noqa: BLE001
                errores.append(exc)

    hilos = [threading.Thread(target=replica) for _ in range(HILOS)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    esperado = HILOS * POR_HILO * MONTO
    obtenido = almacen.gasto_del_dia("2026-09-20")
    check(f"{HILOS} réplicas x {POR_HILO} sumas sin errores", not errores,
          errores[:1])
    check(f"el total es exacto ({esperado:.2f})", abs(obtenido - esperado) < 1e-9,
          f"obtenido {obtenido:.4f}")
    print(f"      {tabla.conflictos} conflictos de ETag (con fila única eran 478)")
    check("fragmentar bajó la contención drásticamente", tabla.conflictos < 100,
          tabla.conflictos)

    print("\n== 11. La caché respeta el límite de tamaño ==")
    almacen.guardar_cache("chica", {"dato": "x" * 100})
    check("una respuesta pequeña se cachea", almacen.leer_cache("chica") is not None)
    almacen.guardar_cache("grande", {"dato": "x" * (P.AlmacenTablas.LIMITE_CACHE_BYTES + 1000)})
    check("una respuesta enorme NO se cachea, en vez de truncarse",
          almacen.leer_cache("grande") is None)

    print("\n== 12. El límite por IP funciona por hora ==")
    for _ in range(3):
        almacen.registrar_peticion("190.85.1.1")
    check("cuenta las peticiones de esa IP", almacen.peticiones_recientes("190.85.1.1", 3600) == 3)
    check("otra IP no se ve afectada", almacen.peticiones_recientes("190.85.1.2", 3600) == 0)
    almacen.registrar_peticion("2800:e2:1::5")
    check("una IPv6 no rompe la clave de la tabla",
          almacen.peticiones_recientes("2800:e2:1::5", 3600) == 1)

    print("\n== 13. Si la tabla se cae, el Portero falla cerrado ==")
    class TablaCaida(TablaFalsa):
        def get_entity(self, particion, clave):  # noqa: ANN001, ANN201
            raise RuntimeError("el servicio no responde")

    portero = P.Portero(almacen=P.AlmacenTablas(cliente_tabla=TablaCaida()),
                        presupuesto_diario_usd=100.0)
    v = portero.evaluar("algo", "1.1.1.1", "v", "META", "Villavicencio")
    check("no deja pasar la petición", not v.permitir, v.motivo)
    check("responde 503", v.codigo == 503)


def main() -> int:
    print("== 1. El presupuesto diario corta ==")
    p = P.Portero(presupuesto_diario_usd=1.00)
    v = p.evaluar("cobertura", "1.1.1.1", "v1", "CUNDINAMARCA", "Soacha")
    check("con presupuesto disponible, pasa", v.permitir)

    # 200.000 de entrada + 20.000 de salida = USD 0,90 de un presupuesto de 1,00
    usd = p.registrar_consumo("cobertura", "1.1.1.1", "v1", {"r": 1}, 200_000, 20_000,
                              "CUNDINAMARCA", "Soacha")
    check("el costo se calcula con las tarifas", abs(usd - (0.2 * 3.0 + 0.02 * 15.0)) < 1e-9, usd)
    check("el gasto queda registrado", abs(p.almacen.gasto_del_dia(p._hoy()) - usd) < 1e-9)

    # Con 0,90 gastados y una estimación de 0,05, todavía cabe: 0,95 < 1,00.
    # El límite tiene que probarse por los dos lados, o no se está probando.
    v = p.evaluar("pregunta que todavia cabe", "2.2.2.2", "v2", "CUNDINAMARCA", "Soacha")
    check("con 0,90 de 1,00 gastados, TODAVÍA pasa", v.permitir, v.motivo)

    # Un poco más y ya no cabe.
    p.registrar_consumo("otra", "2.2.2.2", "v2", {"r": 2}, 30_000, 0, "CUNDINAMARCA", "Soacha")
    v = p.evaluar("otra pregunta distinta", "3.3.3.3", "v3", "CUNDINAMARCA", "Soacha")
    check("agotado el presupuesto, NO pasa", not v.permitir, v.motivo)
    check("responde 503", v.codigo == 503)
    check("el mensaje explica que es gratuita y acotada",
          "gratuita" in v.motivo and "hola@startin.org.co" in v.motivo)

    print("\n== 2. La caché no gasta ==")
    p = P.Portero(presupuesto_diario_usd=1.00)
    p.registrar_consumo("cuantos colegios hay", "1.1.1.1", "v1", {"dato": 42}, 1000, 500,
                        "CUNDINAMARCA", "Soacha")
    gasto_antes = p.almacen.gasto_del_dia(p._hoy())
    usadas_antes = p.almacen.preguntas_usadas("v9")

    v = p.evaluar("cuantos colegios hay", "9.9.9.9", "v9", "CUNDINAMARCA", "Soacha")
    check("la segunda vez sale de caché", v.desde_cache and v.respuesta == {"dato": 42})
    check("no gastó presupuesto", p.almacen.gasto_del_dia(p._hoy()) == gasto_antes)
    check("no consumió cuota del visitante", p.almacen.preguntas_usadas("v9") == usadas_antes)

    v2 = p.evaluar("¿Cuántos colegios hay?", "9.9.9.9", "v9", "cundinamarca", "soacha")
    check("normaliza puntuación, tildes y mayúsculas", v2.desde_cache, v2.motivo)

    print("\n== 3. El territorio entra en la clave ==")
    v3 = p.evaluar("cuantos colegios hay", "9.9.9.9", "v9", "AMAZONAS", "Leticia")
    check("misma pregunta, otro municipio: NO usa la caché", not v3.desde_cache)
    check("y deja pasar la consulta real", v3.permitir)

    print("\n== 4. El límite por IP frena un script ==")
    p = P.Portero(presupuesto_diario_usd=100.0)
    bloqueado_en = None
    for i in range(P.LIMITE_IP_POR_HORA + 5):
        v = p.evaluar(f"pregunta numero {i}", "7.7.7.7", f"visitante{i}", "TOLIMA", "Ibagué")
        if not v.permitir and v.codigo == 429:
            bloqueado_en = i
            break
        if v.permitir and not v.desde_cache:
            p.registrar_consumo(f"pregunta numero {i}", "7.7.7.7", f"visitante{i}",
                                {"r": i}, 100, 50, "TOLIMA", "Ibagué")
    check("bloquea al llegar al límite", bloqueado_en == P.LIMITE_IP_POR_HORA, bloqueado_en)
    check("sugiere cuánto esperar", v.espera_segundos is not None)
    otra = p.evaluar("pregunta nueva", "8.8.8.8", "otro", "TOLIMA", "Ibagué")
    check("otra IP no queda castigada", otra.permitir)

    print("\n== 5. Las preguntas libres se acaban ==")
    p = P.Portero(presupuesto_diario_usd=100.0)
    for i in range(P.PREGUNTAS_LIBRES):
        v = p.evaluar(f"consulta {i}", f"10.0.0.{i}", "mismo-visitante", "HUILA", "Neiva")
        check(f"  libre {i+1}/{P.PREGUNTAS_LIBRES}", v.permitir, v.motivo) if i == 0 else None
        p.registrar_consumo(f"consulta {i}", f"10.0.0.{i}", "mismo-visitante",
                            {"r": i}, 100, 50, "HUILA", "Neiva")
    v = p.evaluar("una mas", "10.0.0.99", "mismo-visitante", "HUILA", "Neiva")
    check(f"la número {P.PREGUNTAS_LIBRES + 1} pide registro", not v.permitir and v.codigo == 402)
    check("el mensaje pregunta por organización y propósito",
          "organización" in v.motivo and "usarás los datos" in v.motivo)

    libre = p.evaluar("una mas", "10.0.0.99", "otro-visitante", "HUILA", "Neiva")
    check("otro visitante tiene sus propias preguntas", libre.permitir or libre.desde_cache)

    con_acceso = p.evaluar("una mas", "10.0.0.99", "mismo-visitante", "HUILA", "Neiva",
                           acceso_completo=True)
    check("con acceso completo no aplica la cuota", con_acceso.permitir or con_acceso.desde_cache)

    print("\n== 6. Si el contador falla, NO se deja pasar ==")
    p = P.Portero(almacen=AlmacenRoto(), presupuesto_diario_usd=100.0)
    v = p.evaluar("cualquier cosa", "1.2.3.4", "v", "META", "Villavicencio")
    check("falla cerrado, no abierto", not v.permitir, v.motivo)
    check("responde 503, no 200", v.codigo == 503)

    print("\n== 7. La caché expira ==")
    p = P.Portero(presupuesto_diario_usd=100.0)
    p.registrar_consumo("algo", "1.1.1.1", "v", {"r": 1}, 100, 50, "CAUCA", "Popayán")
    clave = P.clave_cache("algo", "CAUCA", "Popayán")
    guardado, resp = p.almacen.cache[clave]
    p.almacen.cache[clave] = (guardado - (P.CACHE_DIAS * 86400 + 60), resp)
    check("una entrada vencida no se sirve", p.almacen.leer_cache(clave) is None)

    print("\n== 8. El estado es legible para un humano ==")
    p = P.Portero(presupuesto_diario_usd=8.5)
    p.registrar_consumo("x", "1.1.1.1", "v", {"r": 1}, 500_000, 50_000, "", "")
    e = p.estado()
    check("reporta gastado y disponible",
          e["gastado_hoy_usd"] > 0 and e["disponible_hoy_usd"] < 8.5)
    check("reporta el porcentaje usado", 0 < e["porcentaje_usado"] < 100, e["porcentaje_usado"])
    check("suman al presupuesto del día",
          abs(e["gastado_hoy_usd"] + e["disponible_hoy_usd"] - 8.5) < 0.01)

    print("\n== 9. El reparto mensual es coherente ==")
    check("el diario sale del mensual",
          abs(P.PRESUPUESTO_DIARIO_USD - P.PRESUPUESTO_MENSUAL_USD / 30) < 1e-9)
    check("se reserva un margen sin gastar", P.FRACCION_UTILIZABLE < 1.0)
    techo_mes = P.PRESUPUESTO_DIARIO_USD * P.FRACCION_UTILIZABLE * 30
    check("el techo anual nunca supera el tope mensual",
          techo_mes <= P.PRESUPUESTO_MENSUAL_USD, round(techo_mes, 2))
    print(f"      gasto máximo posible en 30 días: USD {techo_mes:.2f} "
          f"de un tope de {P.PRESUPUESTO_MENSUAL_USD:.0f}")



    probar_almacen_compartido()

    print("\n" + "=" * 64)
    if FALLOS:
        print(f"FALLARON {len(FALLOS)}:")
        for f in FALLOS:
            print("  ·", f)
        return 1
    print("Todas las verificaciones pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
