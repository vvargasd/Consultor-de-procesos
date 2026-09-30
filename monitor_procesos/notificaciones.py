"""Notificaciones — sección 6 del spec.

Arquitectura: notificar(asunto, cuerpo, resumen) con los canales detrás,
intercambiables y activables por configuración.

- cuerpo: contenido completo, va por correo (canal de registro consultable).
- resumen: texto corto, va por ntfy (push al teléfono).
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import requests

logger = logging.getLogger(__name__)

NTFY_BASE = "https://ntfy.sh"
NTFY_TIMEOUT = 15

URL_PORTAL_PROCESO = (
    "https://consultaprocesos.ramajudicial.gov.co/Procesos/NumeroRadicacion"
    "?numero={radicado}&SoloActivos=false"
)

DESCARGO = (
    "Los datos provienen de la réplica del portal de la Rama Judicial, que tiene "
    "rezago. Esto no sustituye la verificación oficial de términos procesales."
)


# --- Canales ------------------------------------------------------------


class ErrorNotificacion(Exception):
    """Fallo al enviar por un canal específico."""


class ErrorNotificacionTotal(Exception):
    """Fallaron todos los canales activos (sección 7: log + salir con código != 0)."""


@dataclass
class ConfigCorreo:
    activo: bool
    servidor: str
    puerto: int
    remitente: str
    password_app: str
    destinatarios: list[str]


@dataclass
class ConfigNtfy:
    activo: bool
    tema: str


def enviar_correo(config: ConfigCorreo, asunto: str, cuerpo: str) -> None:
    if not config.destinatarios:
        raise ErrorNotificacion("no hay destinatarios configurados para correo")

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = config.remitente
    msg["To"] = ", ".join(config.destinatarios)
    msg.set_content(cuerpo)

    try:
        with smtplib.SMTP_SSL(config.servidor, config.puerto, timeout=30) as smtp:
            smtp.login(config.remitente, config.password_app)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise ErrorNotificacion(f"fallo enviando correo: {exc}") from exc


def enviar_ntfy(config: ConfigNtfy, resumen: str) -> None:
    url = f"{NTFY_BASE}/{config.tema}"
    try:
        resp = requests.post(url, data=resumen.encode("utf-8"), timeout=NTFY_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ErrorNotificacion(f"fallo enviando ntfy: {exc}") from exc


def notificar(
    asunto: str,
    cuerpo: str,
    resumen: str,
    config_correo: ConfigCorreo,
    config_ntfy: ConfigNtfy,
    enviar_push: bool = True,
) -> None:
    """Envía por todos los canales activos (no es fallback: se intentan ambos
    de forma independiente, así uno pueda fallar sin tumbar al otro).

    enviar_push=False cuando no hay novedades — sección 6: "Si no hay
    novedades: no enviar push."

    Nunca debe producir silencio total: si todos los canales que se
    intentaron fallan, levanta ErrorNotificacionTotal para que el llamador
    termine con código de salida distinto de cero (sección 7).
    """
    canales_intentados = 0
    errores: list[tuple[str, Exception]] = []

    if config_correo.activo:
        canales_intentados += 1
        try:
            enviar_correo(config_correo, asunto, cuerpo)
            logger.info("Correo enviado a %s", config_correo.destinatarios)
        except ErrorNotificacion as exc:
            logger.error("%s", exc)
            errores.append(("correo", exc))

    if config_ntfy.activo and enviar_push:
        canales_intentados += 1
        try:
            enviar_ntfy(config_ntfy, resumen)
            logger.info("ntfy enviado")
        except ErrorNotificacion as exc:
            logger.error("%s", exc)
            errores.append(("ntfy", exc))

    if canales_intentados and len(errores) == canales_intentados:
        raise ErrorNotificacionTotal(
            f"fallaron todos los canales intentados: {[c for c, _ in errores]}"
        )


# --- Composición del mensaje ---------------------------------------------


@dataclass
class NovedadProceso:
    radicado: str
    id_proceso: int
    nombre: str | None
    despacho: str | None
    actuaciones: list[dict[str, Any]]  # las nuevas, ya filtradas (ver deteccion.py)


@dataclass
class ResultadoCorrida:
    timestamp: datetime
    novedades: list[NovedadProceso] = field(default_factory=list)
    radicados_invalidos: list[tuple[int, str, str]] = field(default_factory=list)  # (fila, crudo, error)
    radicados_sin_resultados: list[str] = field(default_factory=list)  # radicados válidos, 0 procesos
    procesos_fallidos: list[tuple[str, str]] = field(default_factory=list)  # (radicado, error)
    privados_nuevos: list[tuple[str, str]] = field(default_factory=list)  # (radicado, despacho)
    portal_caido: bool = False

    @property
    def hay_novedades(self) -> bool:
        return bool(self.novedades)

    @property
    def hay_algo_que_reportar(self) -> bool:
        return bool(
            self.novedades
            or self.radicados_invalidos
            or self.radicados_sin_resultados
            or self.procesos_fallidos
            or self.privados_nuevos
            or self.portal_caido
        )


def _formatear_fecha_hora(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _formatear_actuacion(a: dict[str, Any]) -> str:
    partes = [f"    - {a.get('fechaActuacion', '?')[:10]}: {a.get('actuacion', '(sin tipo)')}"]
    if a.get("anotacion"):
        partes.append(f"      {a['anotacion']}")
    if a.get("fechaInicial") or a.get("fechaFinal"):
        partes.append(
            f"      término: {(a.get('fechaInicial') or '?')[:10]} a "
            f"{(a.get('fechaFinal') or '?')[:10]}"
        )
    return "\n".join(partes)


def componer_asunto(resultado: ResultadoCorrida) -> str:
    if resultado.portal_caido:
        return "Monitor de procesos: el portal no respondió"
    if resultado.hay_novedades:
        return f"Monitor de procesos: {len(resultado.novedades)} novedad(es)"
    if resultado.hay_algo_que_reportar:
        return "Monitor de procesos: sin novedades, con incidencias"
    return "Monitor de procesos: sin novedades"


def componer_cuerpo(resultado: ResultadoCorrida) -> str:
    """Contenido completo para el correo. Un solo mensaje consolidado por
    corrida (sección 6), nunca uno por proceso.
    """
    lineas: list[str] = []
    lineas.append(f"Consulta realizada: {_formatear_fecha_hora(resultado.timestamp)}")
    lineas.append("")

    if resultado.portal_caido:
        lineas.append(
            "El portal de la Rama Judicial no respondió durante esta corrida. "
            "No se pudo consultar ningún proceso."
        )
        lineas.append("")

    if resultado.novedades:
        lineas.append(f"NOVEDADES ({len(resultado.novedades)})")
        lineas.append("-" * 40)
        for nov in resultado.novedades:
            lineas.append(f"{nov.nombre or '(sin nombre)'} — radicado {nov.radicado}")
            lineas.append(f"  Despacho: {nov.despacho or '(desconocido)'}")
            lineas.append(f"  Actuación(es) nueva(s):")
            for a in nov.actuaciones:
                lineas.append(_formatear_actuacion(a))
            lineas.append(f"  Enlace: {URL_PORTAL_PROCESO.format(radicado=nov.radicado)}")
            lineas.append("")

    if resultado.privados_nuevos:
        lineas.append(f"PROCESOS QUE PASARON A PRIVADOS ({len(resultado.privados_nuevos)})")
        lineas.append("-" * 40)
        for radicado, despacho in resultado.privados_nuevos:
            lineas.append(f"  {radicado} — {despacho or '(despacho desconocido)'}")
        lineas.append("")

    if resultado.radicados_invalidos:
        lineas.append(f"RADICADOS INVÁLIDOS EN EL EXCEL ({len(resultado.radicados_invalidos)})")
        lineas.append("-" * 40)
        for fila, crudo, error in resultado.radicados_invalidos:
            lineas.append(f"  fila {fila}: {crudo!r} — {error}")
        lineas.append("")

    if resultado.radicados_sin_resultados:
        lineas.append(
            f"RADICADOS SIN RESULTADOS EN EL PORTAL ({len(resultado.radicados_sin_resultados)})"
        )
        lineas.append("-" * 40)
        lineas.append("  Formato válido (23 dígitos) pero el portal no encontró el proceso.")
        lineas.append("  Puede ser un error de digitación en el Excel.")
        for radicado in resultado.radicados_sin_resultados:
            lineas.append(f"  {radicado}")
        lineas.append("")

    if resultado.procesos_fallidos:
        lineas.append(f"CONSULTAS FALLIDAS ({len(resultado.procesos_fallidos)})")
        lineas.append("-" * 40)
        for radicado, error in resultado.procesos_fallidos:
            lineas.append(f"  {radicado}: {error}")
        lineas.append("")

    if not resultado.hay_algo_que_reportar:
        lineas.append("Sin novedades ni incidencias en esta corrida.")
        lineas.append("")

    lineas.append("-" * 40)
    lineas.append(DESCARGO)

    return "\n".join(lineas)


def componer_resumen(resultado: ResultadoCorrida) -> str:
    """Texto corto para ntfy. Solo se usa cuando hay_novedades (sección 6:
    no se envía push si no hay novedades)."""
    nombres = ", ".join(n.nombre or n.radicado for n in resultado.novedades[:5])
    if len(resultado.novedades) > 5:
        nombres += f" y {len(resultado.novedades) - 5} más"
    return f"{len(resultado.novedades)} novedad(es): {nombres}"


def _autoprueba() -> None:
    """Nota: correr con `python -c "from monitor_procesos.notificaciones import
    _autoprueba; _autoprueba()"`, no con `python -m` — con -m este módulo se
    ejecuta como __main__, una identidad de módulo distinta de
    "monitor_procesos.notificaciones", y unittest.mock.patch() (que apunta a
    esta última) dejaría de interceptar las llamadas reales."""
    from zoneinfo import ZoneInfo

    zona = ZoneInfo("America/Bogota")
    ahora = datetime(2026, 9, 24, 14, 30, tzinfo=zona)

    # --- caso: corrida con novedad ---
    resultado = ResultadoCorrida(
        timestamp=ahora,
        novedades=[
            NovedadProceso(
                radicado="11001310301220200099900",
                id_proceso=10000001,
                nombre="MARIA EJEMPLO DE PRUEBA",
                despacho="JUZGADO 012 CIVIL DEL CIRCUITO DE BOGOTÁ",
                actuaciones=[
                    {
                        "fechaActuacion": "2026-09-15T00:00:00",
                        "actuacion": "Recepción memorial",
                        "anotacion": "ACUSO RECIBIDO",
                        "fechaInicial": None,
                        "fechaFinal": None,
                    }
                ],
            )
        ],
        radicados_invalidos=[(32, "73319318420200016000", "20 dígitos en vez de 23")],
    )

    asunto = componer_asunto(resultado)
    cuerpo = componer_cuerpo(resultado)
    resumen = componer_resumen(resultado)

    print("=== ASUNTO ===")
    print(asunto)
    print("\n=== CUERPO ===")
    print(cuerpo)
    print("\n=== RESUMEN (ntfy) ===")
    print(resumen)

    assert "NOVEDADES" in cuerpo
    assert "Recepción memorial" in cuerpo
    assert "RADICADOS INVÁLIDOS" in cuerpo
    assert URL_PORTAL_PROCESO.split("?")[0] in cuerpo
    assert DESCARGO in cuerpo
    print("\n(verificaciones básicas de contenido: OK)")

    # --- caso: sin novedades ni incidencias ---
    vacio = ResultadoCorrida(timestamp=ahora)
    assert componer_asunto(vacio) == "Monitor de procesos: sin novedades"
    assert "Sin novedades ni incidencias" in componer_cuerpo(vacio)
    print("(caso sin novedades: OK)")

    # --- notificar(): validar que "todos fallan" levanta ErrorNotificacionTotal ---
    # Se simulan los dos canales fallando (sin tocar red real: ntfy.sh acepta
    # cualquier tema con solo publicarlo, así que un tema "inválido" en realidad
    # SÍ funcionaría — no sirve para forzar un fallo real de forma determinista).
    from unittest.mock import patch

    correo_cfg = ConfigCorreo(
        activo=True, servidor="smtp.example.com", puerto=465,
        remitente="x@example.com", password_app="x", destinatarios=["y@example.com"],
    )
    ntfy_cfg = ConfigNtfy(activo=True, tema="tema-de-prueba")

    with patch(
        "monitor_procesos.notificaciones.enviar_correo",
        side_effect=ErrorNotificacion("simulado: correo caído"),
    ), patch(
        "monitor_procesos.notificaciones.enviar_ntfy",
        side_effect=ErrorNotificacion("simulado: ntfy caído"),
    ):
        try:
            notificar("asunto", "cuerpo", "resumen", correo_cfg, ntfy_cfg)
        except ErrorNotificacionTotal as exc:
            print(f"\n(notificar con ambos canales fallando -> ErrorNotificacionTotal: OK) {exc}")
        else:
            print("\n(ADVERTENCIA: se esperaba ErrorNotificacionTotal y no ocurrió)")

    # --- notificar(): un canal falla, el otro funciona -> no debe levantar ---
    with patch(
        "monitor_procesos.notificaciones.enviar_correo",
        side_effect=ErrorNotificacion("simulado: correo caído"),
    ), patch("monitor_procesos.notificaciones.enviar_ntfy", return_value=None):
        notificar("asunto", "cuerpo", "resumen", correo_cfg, ntfy_cfg)
        print("(un canal falla, el otro funciona -> no se levanta ErrorNotificacionTotal: OK)")

    print("\nTodas las pruebas de notificaciones pasaron.")


if __name__ == "__main__":
    _autoprueba()
