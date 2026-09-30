"""Carga de configuración — sección 8 del spec.

Archivo TOML separado del código. Contiene una credencial (password_app), así
que en producción debe tener permisos 600 y estar fuera de control de
versiones — eso es responsabilidad del despliegue, no de este módulo.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from monitor_procesos.api_cliente import ConfigRed
from monitor_procesos.notificaciones import ConfigCorreo, ConfigNtfy


class ConfigError(Exception):
    """TOML ausente, ilegible, o le falta una clave requerida.

    Debe abortar la corrida — sin configuración válida no hay forma
    confiable de notificar el problema (sección 7).
    """


@dataclass(frozen=True)
class ConfigExcel:
    ruta: str
    hoja: str
    columna_radicado: str
    columna_nombre: str | None


@dataclass(frozen=True)
class ConfigDeteccion:
    ventana_vigilancia_dias: int


@dataclass(frozen=True)
class ConfigEstado:
    ruta_db: str


@dataclass(frozen=True)
class ConfigOperacion:
    ruta_log: str
    ruta_lockfile: str


@dataclass(frozen=True)
class Config:
    excel: ConfigExcel
    deteccion: ConfigDeteccion
    correo: ConfigCorreo
    resumen_diario: bool
    ntfy: ConfigNtfy
    red: ConfigRed
    estado: ConfigEstado
    operacion: ConfigOperacion


def _requerido(d: dict, clave: str, seccion: str):
    if clave not in d:
        raise ConfigError(f"falta la clave {clave!r} en la sección [{seccion}] del TOML")
    return d[clave]


def _seccion(datos: dict, nombre: str) -> dict:
    if nombre not in datos:
        raise ConfigError(f"falta la sección [{nombre}] en el TOML")
    return datos[nombre]


def cargar_config(ruta: str) -> Config:
    path = Path(ruta)
    try:
        with path.open("rb") as f:
            datos = tomllib.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"no se encontró el archivo de configuración: {ruta}") from exc
    except OSError as exc:
        raise ConfigError(f"no se pudo leer el archivo de configuración {ruta}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML inválido en {ruta}: {exc}") from exc

    excel_d = _seccion(datos, "excel")
    deteccion_d = datos.get("deteccion", {})
    correo_d = _seccion(datos, "correo")
    ntfy_d = _seccion(datos, "ntfy")
    red_d = datos.get("red", {})
    estado_d = _seccion(datos, "estado")
    operacion_d = _seccion(datos, "operacion")

    excel = ConfigExcel(
        ruta=_requerido(excel_d, "ruta", "excel"),
        hoja=_requerido(excel_d, "hoja", "excel"),
        columna_radicado=_requerido(excel_d, "columna_radicado", "excel"),
        columna_nombre=excel_d.get("columna_nombre"),
    )
    deteccion = ConfigDeteccion(
        ventana_vigilancia_dias=deteccion_d.get("ventana_vigilancia_dias", 7)
    )
    correo = ConfigCorreo(
        activo=correo_d.get("activo", False),
        servidor=correo_d.get("servidor", "smtp.gmail.com"),
        puerto=correo_d.get("puerto", 465),
        remitente=correo_d.get("remitente", ""),
        password_app=correo_d.get("password_app", ""),
        destinatarios=correo_d.get("destinatarios", []),
    )
    resumen_diario = bool(correo_d.get("resumen_diario", True))
    ntfy = ConfigNtfy(
        activo=ntfy_d.get("activo", False),
        tema=ntfy_d.get("tema", ""),
    )
    red = ConfigRed(
        pausa_entre_peticiones=red_d.get("pausa_entre_peticiones", 1.0),
        timeout=red_d.get("timeout", 30.0),
        reintentos=red_d.get("reintentos", 3),
    )
    estado = ConfigEstado(ruta_db=_requerido(estado_d, "ruta_db", "estado"))
    operacion = ConfigOperacion(
        ruta_log=_requerido(operacion_d, "ruta_log", "operacion"),
        ruta_lockfile=_requerido(operacion_d, "ruta_lockfile", "operacion"),
    )

    return Config(
        excel=excel,
        deteccion=deteccion,
        correo=correo,
        resumen_diario=resumen_diario,
        ntfy=ntfy,
        red=red,
        estado=estado,
        operacion=operacion,
    )


if __name__ == "__main__":
    import tempfile

    # 1. archivo inexistente -> ConfigError
    try:
        cargar_config("/no/existe/config.toml")
    except ConfigError as exc:
        print(f"1. archivo inexistente -> ConfigError: OK ({exc})")
    else:
        raise SystemExit("1. FALLÓ: se esperaba ConfigError")

    # 2. TOML sin la sección [estado] -> ConfigError con mensaje claro
    incompleto = """
[excel]
ruta = "x.xlsx"
hoja = "Hoja 1"
columna_radicado = "NUMERO DE PROCESO"

[correo]
activo = false

[ntfy]
activo = false
"""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(incompleto)
        ruta_incompleta = f.name
    try:
        cargar_config(ruta_incompleta)
    except ConfigError as exc:
        assert "estado" in str(exc)
        print(f"2. falta sección [estado] -> ConfigError: OK ({exc})")
    else:
        raise SystemExit("2. FALLÓ: se esperaba ConfigError")

    # 3. TOML completo y válido, con valores por defecto aplicados donde falten
    completo = """
[excel]
ruta = "procesos.xlsx"
hoja = "Hoja 1"
columna_radicado = "NUMERO DE PROCESO"
columna_nombre = "demandante"

[correo]
activo = true
remitente = "bot@example.com"
password_app = "clave-app"
destinatarios = ["abogado@example.com"]

[ntfy]
activo = true
tema = "k7x2m9qw4rt8pz"

[estado]
ruta_db = "/tmp/estado.sqlite3"

[operacion]
ruta_log = "/tmp/monitor.log"
ruta_lockfile = "/tmp/monitor.lock"
"""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(completo)
        ruta_completa = f.name

    config = cargar_config(ruta_completa)
    assert config.excel.hoja == "Hoja 1"
    assert config.deteccion.ventana_vigilancia_dias == 7  # default
    assert config.correo.servidor == "smtp.gmail.com"  # default
    assert config.resumen_diario is True  # default
    assert config.red.reintentos == 3  # default
    assert config.estado.ruta_db == "/tmp/estado.sqlite3"
    print("3. TOML válido con defaults aplicados: OK")

    print("\nTodas las pruebas de config pasaron.")
