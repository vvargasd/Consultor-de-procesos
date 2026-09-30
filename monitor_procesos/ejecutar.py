"""Runner — ata todas las piezas y aplica la tabla de manejo de errores de
la sección 7 del spec. Punto de entrada del servicio systemd.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from monitor_procesos import estado_db
from monitor_procesos.api_cliente import ClienteAPI, ErrorConsulta, NoEncontrado, RespuestaInesperada
from monitor_procesos.concurrencia import BloqueadoError, adquirir_lock
from monitor_procesos.config import Config, ConfigError, cargar_config
from monitor_procesos.deteccion import Accion, actuaciones_nuevas, decidir_accion, solo_fecha
from monitor_procesos.excel_lector import ExcelError, leer_radicados, radicados_a_consultar
from monitor_procesos.notificaciones import (
    ErrorNotificacionTotal,
    NovedadProceso,
    ResultadoCorrida,
    componer_asunto,
    componer_cuerpo,
    componer_resumen,
    notificar,
)

ZONA_BOGOTA = ZoneInfo("America/Bogota")


def configurar_logging(ruta_log: str) -> logging.Logger:
    logger = logging.getLogger("monitor_procesos")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formato = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    archivo = logging.handlers.RotatingFileHandler(
        ruta_log, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    archivo.setFormatter(formato)
    logger.addHandler(archivo)

    # journalctl recoge stdout automáticamente vía systemd (sección 8)
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato)
    logger.addHandler(consola)

    return logger


def _enviar_o_registrar_fallo(
    logger: logging.Logger,
    asunto: str,
    cuerpo: str,
    config: Config,
    enviar_push: bool,
) -> None:
    try:
        notificar(asunto, cuerpo, asunto, config.correo, config.ntfy, enviar_push=enviar_push)
    except ErrorNotificacionTotal as exc:
        logger.error("Fallaron todos los canales de notificación: %s", exc)


def _procesar_decision(
    cliente: ClienteAPI,
    conn,
    proceso: dict,
    estado_previo: estado_db.EstadoProceso | None,
    decision,
    nombre: str | None,
    resultado: ResultadoCorrida,
    dry_run: bool,
    ahora: datetime,
    logger: logging.Logger,
) -> None:
    id_proceso = proceso["idProceso"]

    def _guardar(cantidad_actuaciones: int | None) -> None:
        if dry_run:
            return
        # fechaUltimaActuacion viene en null para procesos privados (visto en
        # producción: idProceso con esPrivado=true trae fechaProceso,
        # fechaUltimaActuacion e idConexion en null y sujetosProcesales como
        # el literal "--- [ PROCESO PRIVADO ] ---"). No estaba en el spec
        # original — solo decía que un privado "no expone actuaciones", no
        # que la fecha misma viniera nula.
        fecha_cruda = proceso["fechaUltimaActuacion"]
        nuevo = estado_db.EstadoProceso(
            id_proceso=id_proceso,
            llave_proceso=proceso["llaveProceso"],
            id_conexion=proceso["idConexion"],
            despacho=proceso["despacho"],
            sujetos_procesales=proceso["sujetosProcesales"],
            es_privado=proceso["esPrivado"],
            fecha_ultima_actuacion=solo_fecha(fecha_cruda) if fecha_cruda else None,
            cantidad_actuaciones=cantidad_actuaciones,
            ultima_revision_ok=ahora,
        )
        estado_db.guardar_proceso(conn, nuevo)

    if decision.accion is Accion.SIN_CAMBIO:
        # La consulta sí fue exitosa (solo que no hay novedad): refrescar
        # despacho/sujetos/ultima_revision_ok, conservando el conteo ya
        # guardado (no se volvió a pedir el endpoint de actuaciones).
        _guardar(estado_previo.cantidad_actuaciones if estado_previo else None)
        return

    if decision.accion is Accion.PRIVADO_NUEVO:
        resultado.privados_nuevos.append((proceso["llaveProceso"], proceso["despacho"]))
        _guardar(None)
        return

    # PROCESO_NUEVO, NOVEDAD_NIVEL1, NOVEDAD_NIVEL2: puede requerir traer
    # actuaciones. Un proceso privado nunca las expone (sección 3.1).
    cantidad_actual: int | None = None
    actuaciones_api: list[dict] = []
    if not proceso["esPrivado"]:
        try:
            cantidad_actual, actuaciones_api = cliente.consultar_actuaciones(id_proceso)
        except (ErrorConsulta, NoEncontrado) as exc:
            logger.error("Fallo trayendo actuaciones de idProceso=%s: %s", id_proceso, exc)
            resultado.procesos_fallidos.append((proceso["llaveProceso"], f"actuaciones: {exc}"))
            return  # sección 7: no se guarda estado de una consulta fallida

    if decision.accion is Accion.PROCESO_NUEVO:
        _guardar(cantidad_actual)
        return  # sin alerta — sección 5, "Primera ejecución"

    # NOVEDAD_NIVEL1 / NOVEDAD_NIVEL2
    cantidad_guardada = estado_previo.cantidad_actuaciones if estado_previo else None
    nuevas = actuaciones_nuevas(actuaciones_api, cantidad_guardada, cantidad_actual)

    if nuevas:
        resultado.novedades.append(
            NovedadProceso(
                radicado=proceso["llaveProceso"],
                id_proceso=id_proceso,
                nombre=nombre,
                despacho=proceso["despacho"],
                actuaciones=nuevas,
            )
        )
    elif decision.accion is Accion.NOVEDAD_NIVEL1:
        logger.warning(
            "idProceso=%s: la fecha cambió pero el conteo de actuaciones no "
            "aumentó (guardado=%s, actual=%s) — revisar manualmente.",
            id_proceso,
            cantidad_guardada,
            cantidad_actual,
        )

    _guardar(cantidad_actual)


def ejecutar(config: Config, ahora: datetime, dry_run: bool, logger: logging.Logger) -> int:
    resultado = ResultadoCorrida(timestamp=ahora)

    try:
        filas = leer_radicados(
            config.excel.ruta,
            config.excel.hoja,
            config.excel.columna_radicado,
            config.excel.columna_nombre,
        )
    except ExcelError as exc:
        logger.error("No se pudo leer el Excel: %s", exc)
        _enviar_o_registrar_fallo(
            logger,
            "Monitor de procesos: no se pudo leer el Excel de entrada",
            f"No se pudo leer el archivo de entrada.\n\n{exc}",
            config,
            enviar_push=True,
        )
        return 1

    for f in filas:
        if not f.valido:
            resultado.radicados_invalidos.append((f.fila, f.radicado_crudo, f.error))

    nombres_por_radicado: dict[str, list[str]] = {}
    for f in filas:
        if f.valido and f.nombre:
            nombres_por_radicado.setdefault(f.radicado, []).append(f.nombre)

    radicados = radicados_a_consultar(filas)
    cliente = ClienteAPI(config.red)
    conn = estado_db.conectar(config.estado.ruta_db)

    intentados = 0
    fallidos = 0

    try:
        for radicado in radicados:
            intentados += 1
            try:
                procesos = cliente.consultar_radicado(radicado)
            except ErrorConsulta as exc:
                logger.error("Fallo consultando radicado %s: %s", radicado, exc)
                resultado.procesos_fallidos.append((radicado, str(exc)))
                fallidos += 1
                continue
            except RespuestaInesperada as exc:
                # sección 7: la forma del JSON cambió — abortar TODA la
                # corrida, no solo este radicado, con mensaje claro.
                logger.error(
                    "Respuesta inesperada de la API para radicado %s: %s. "
                    "Probablemente cambió la API sin aviso.",
                    radicado,
                    exc,
                )
                asunto = "Monitor de procesos: la API del portal cambió o falló"
                cuerpo = (
                    f"La API del portal respondió con una forma inesperada al "
                    f"consultar el radicado {radicado}.\n\n{exc}\n\n"
                    "Es una API no oficial y no documentada: probablemente "
                    "cambió sin aviso. Revisar monitor-procesos-spec.md, "
                    "sección 3, antes de reintentar."
                )
                _enviar_o_registrar_fallo(logger, asunto, cuerpo, config, enviar_push=True)
                return 1

            if not procesos:
                resultado.radicados_sin_resultados.append(radicado)
                continue

            nombre = ", ".join(dict.fromkeys(nombres_por_radicado.get(radicado, []))) or None

            for proceso in procesos:
                estado_previo = estado_db.obtener_proceso(conn, proceso["idProceso"])
                decision = decidir_accion(
                    proceso, estado_previo, config.deteccion.ventana_vigilancia_dias, ahora
                )
                logger.info(
                    "radicado=%s idProceso=%s -> %s (%s)",
                    radicado,
                    proceso["idProceso"],
                    decision.accion.name,
                    decision.razon,
                )
                _procesar_decision(
                    cliente,
                    conn,
                    proceso,
                    estado_previo,
                    decision,
                    nombre,
                    resultado,
                    dry_run,
                    ahora,
                    logger,
                )

        if intentados > 0 and fallidos == intentados:
            resultado.portal_caido = True
            logger.error("Los %d radicados consultados fallaron: el portal parece estar caído.", intentados)

        if not dry_run:
            estado_db.registrar_corrida(
                conn,
                ahora,
                procesos_consultados=len(radicados),
                novedades=len(resultado.novedades),
                errores=len(resultado.procesos_fallidos),
            )
    finally:
        conn.close()

    asunto = componer_asunto(resultado)
    cuerpo = componer_cuerpo(resultado)
    resumen = componer_resumen(resultado)

    if dry_run:
        print(f"[dry-run] no se envía nada ni se toca el estado.\n\nASUNTO: {asunto}\n\n{cuerpo}")
        return 0

    debe_notificar = resultado.hay_algo_que_reportar or config.resumen_diario
    if debe_notificar:
        try:
            notificar(
                asunto, cuerpo, resumen, config.correo, config.ntfy, enviar_push=resultado.hay_novedades
            )
        except ErrorNotificacionTotal as exc:
            logger.error("Fallaron todos los canales de notificación: %s", exc)
            return 1
    else:
        logger.info("Sin novedades ni incidencias; resumen diario desactivado: no se notifica.")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor de procesos judiciales — Rama Judicial")
    parser.add_argument("--config", default="config.toml", help="ruta al archivo de configuración TOML")
    parser.add_argument(
        "--dry-run", action="store_true", help="consulta y muestra en pantalla sin enviar ni tocar el estado"
    )
    args = parser.parse_args(argv)

    try:
        config = cargar_config(args.config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        logging.error("Error de configuración: %s", exc)
        return 1

    logger = configurar_logging(config.operacion.ruta_log)

    try:
        lock = adquirir_lock(config.operacion.ruta_lockfile)
    except BloqueadoError as exc:
        logger.info("%s — saliendo sin hacer nada.", exc)
        return 0

    try:
        ahora = datetime.now(ZONA_BOGOTA)
        return ejecutar(config, ahora, args.dry_run, logger)
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
