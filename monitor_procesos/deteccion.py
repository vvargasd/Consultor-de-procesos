"""Detección de cambios en dos niveles — núcleo del sistema (sección 5 del spec).

Funciones puras: reciben datos ya obtenidos (de la API, del estado guardado)
y devuelven una decisión. No llaman a la API ni a la base de datos, así se
pueden probar sin red y sin tocar disco.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum, auto
from typing import Any

from monitor_procesos.estado_db import EstadoProceso

ZONA_BOGOTA_NOMBRE = "America/Bogota"


class Accion(Enum):
    PROCESO_NUEVO = auto()  # sin estado previo: sembrar baseline, sin alerta
    PRIVADO_NUEVO = auto()  # esPrivado pasó a True: reportar una sola vez
    SIN_CAMBIO = auto()  # nada que hacer
    NOVEDAD_NIVEL1 = auto()  # la fecha cambió: traer actuaciones y notificar
    NOVEDAD_NIVEL2 = auto()  # fecha igual pero dentro de ventana: chequear conteo


@dataclass(frozen=True)
class DecisionProceso:
    accion: Accion
    razon: str


def solo_fecha(valor_iso: str) -> date:
    """fechaUltimaActuacion de la API viene como '...T00:00:00' (sección 3.1)."""
    return datetime.fromisoformat(valor_iso).date()


def decidir_accion(
    proceso_api: dict[str, Any],
    estado_guardado: EstadoProceso | None,
    ventana_dias: int,
    ahora: datetime,
) -> DecisionProceso:
    """Decide qué hacer con UN proceso (un idProceso), en el nivel barato:
    solo usa datos que ya vinieron en la respuesta de consultar_radicado(),
    sin llamar al endpoint de actuaciones.
    """
    if estado_guardado is None:
        return DecisionProceso(Accion.PROCESO_NUEVO, "sin estado previo, se registra")

    if proceso_api["esPrivado"]:
        if not estado_guardado.es_privado:
            return DecisionProceso(Accion.PRIVADO_NUEVO, "esPrivado pasó a True")
        return DecisionProceso(Accion.SIN_CAMBIO, "sigue privado, ya se reportó antes")

    fecha_api = solo_fecha(proceso_api["fechaUltimaActuacion"])
    fecha_guardada = estado_guardado.fecha_ultima_actuacion

    if fecha_guardada is None or fecha_api != fecha_guardada:
        return DecisionProceso(
            Accion.NOVEDAD_NIVEL1, f"fechaUltimaActuacion cambió: {fecha_guardada} -> {fecha_api}"
        )

    dentro_de_ventana = (ahora.date() - fecha_api) <= timedelta(days=ventana_dias)
    if dentro_de_ventana:
        return DecisionProceso(
            Accion.NOVEDAD_NIVEL2,
            f"fecha sin cambios ({fecha_api}) pero dentro de la ventana de "
            f"{ventana_dias} días: verificar conteo",
        )

    return DecisionProceso(Accion.SIN_CAMBIO, f"fecha sin cambios ({fecha_api}) y fuera de ventana")


def actuaciones_nuevas(
    actuaciones_api: list[dict[str, Any]],
    cantidad_guardada: int | None,
    cantidad_actual: int,
) -> list[dict[str, Any]]:
    """Las actuaciones que aparecieron desde la última corrida.

    actuaciones_api debe venir en el orden que entrega la API: descendente,
    más reciente primero (sección 3.2). Se toman las primeras
    (cantidad_actual - cantidad_guardada) del listado — funciona igual para
    el caso "cambió la fecha" y para el caso "misma fecha, dos actuaciones el
    mismo día" (sección 5: el problema que motiva el nivel de refuerzo),
    porque no depende de la fecha en absoluto, solo del conteo.

    Si nunca se había guardado un conteo (no debería pasar en la práctica:
    todo proceso se siembra con su conteo la primera vez que se ve, ver
    Accion.PROCESO_NUEVO), se asume 0 para no ocultar actuaciones por error
    de cálculo — mejor reportar de más que quedarse en silencio (sección 7).
    """
    n_nuevas = max(0, cantidad_actual - (cantidad_guardada or 0))
    return actuaciones_api[:n_nuevas]


if __name__ == "__main__":
    from zoneinfo import ZoneInfo

    zona = ZoneInfo(ZONA_BOGOTA_NOMBRE)

    def _proceso(fecha_ultima_actuacion: str, es_privado: bool = False) -> dict[str, Any]:
        return {"fechaUltimaActuacion": fecha_ultima_actuacion, "esPrivado": es_privado}

    def _estado(fecha: date | None, cantidad: int | None = 10, es_privado: bool = False) -> EstadoProceso:
        return EstadoProceso(
            id_proceso=1,
            llave_proceso="x",
            id_conexion=None,
            despacho=None,
            sujetos_procesales=None,
            es_privado=es_privado,
            fecha_ultima_actuacion=fecha,
            cantidad_actuaciones=cantidad,
            ultima_revision_ok=None,
        )

    ahora = datetime(2026, 9, 24, 8, 0, tzinfo=zona)

    # 1. proceso nuevo -> PROCESO_NUEVO
    d = decidir_accion(_proceso("2026-09-15T00:00:00"), None, 7, ahora)
    assert d.accion is Accion.PROCESO_NUEVO, d
    print("1. proceso nuevo: OK")

    # 2. fecha cambió -> NOVEDAD_NIVEL1
    d = decidir_accion(_proceso("2026-09-20T00:00:00"), _estado(date(2026, 9, 15)), 7, ahora)
    assert d.accion is Accion.NOVEDAD_NIVEL1, d
    print("2. fecha cambió: OK")

    # 3. fecha igual, dentro de ventana (7 días) -> NOVEDAD_NIVEL2
    #    ahora=2026-09-24, fecha=2026-09-20 -> 4 días, dentro de ventana de 7
    d = decidir_accion(_proceso("2026-09-20T00:00:00"), _estado(date(2026, 9, 20)), 7, ahora)
    assert d.accion is Accion.NOVEDAD_NIVEL2, d
    print("3. fecha igual, dentro de ventana: OK")

    # 4. fecha igual, fuera de ventana -> SIN_CAMBIO
    #    ahora=2026-09-24, fecha=2026-08-01 -> 54 días, fuera de ventana de 7
    d = decidir_accion(_proceso("2026-08-01T00:00:00"), _estado(date(2026, 8, 1)), 7, ahora)
    assert d.accion is Accion.SIN_CAMBIO, d
    print("4. fecha igual, fuera de ventana: OK")

    # 5. esPrivado pasa de False a True -> PRIVADO_NUEVO
    d = decidir_accion(_proceso("2026-09-15T00:00:00", es_privado=True), _estado(date(2026, 9, 15), es_privado=False), 7, ahora)
    assert d.accion is Accion.PRIVADO_NUEVO, d
    print("5. esPrivado nuevo: OK")

    # 6. esPrivado sigue True -> SIN_CAMBIO (no se re-reporta cada corrida)
    d = decidir_accion(_proceso("2026-09-15T00:00:00", es_privado=True), _estado(date(2026, 9, 15), es_privado=True), 7, ahora)
    assert d.accion is Accion.SIN_CAMBIO, d
    print("6. esPrivado ya reportado: OK")

    # 7. actuaciones_nuevas: caso normal, count subió de 118 a 120 -> las 2 primeras
    listado = [{"consActuacion": 120}, {"consActuacion": 119}, {"consActuacion": 118}]
    nuevas = actuaciones_nuevas(listado, cantidad_guardada=118, cantidad_actual=120)
    assert nuevas == listado[:2], nuevas
    print("7. actuaciones_nuevas (caso normal): OK")

    # 8. actuaciones_nuevas: dos actuaciones el MISMO día (el bug que motiva el nivel 2)
    #    fecha no cambió pero el conteo sí -> deben detectarse igual, sin usar fecha
    mismo_dia = [
        {"fechaActuacion": "2026-09-24T00:00:00", "consActuacion": 121},
        {"fechaActuacion": "2026-09-24T00:00:00", "consActuacion": 120},
        {"fechaActuacion": "2026-09-15T00:00:00", "consActuacion": 119},
    ]
    nuevas = actuaciones_nuevas(mismo_dia, cantidad_guardada=120, cantidad_actual=121)
    assert nuevas == mismo_dia[:1], nuevas
    print("8. actuaciones_nuevas (mismo día, conteo detecta lo que la fecha no): OK")

    # 9. sin conteo guardado (no debería pasar, pero no debe ocultar nada)
    nuevas = actuaciones_nuevas(listado, cantidad_guardada=None, cantidad_actual=120)
    assert nuevas == listado, nuevas
    print("9. actuaciones_nuevas (sin conteo previo, reporta todo): OK")

    print("\nTodas las pruebas de deteccion pasaron.")
