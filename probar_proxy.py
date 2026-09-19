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
