# Monitor de procesos judiciales — Rama Judicial (CPNU)

Especificación para construir un script que vigile procesos judiciales colombianos
y notifique cuando registren nuevas actuaciones.

---

## 1. Objetivo

Un script que corre 2–3 veces al día de forma desatendida, lee una hoja de Excel con
números de radicación, consulta cada uno en el portal de la Rama Judicial, detecta si
hubo actuaciones nuevas desde la corrida anterior, y notifica únicamente las novedades.

**Usuario final:** un abogado. No es técnico. Solo debe interactuar con el Excel y con
las notificaciones que recibe.

### Fuera de alcance

- Interfaz gráfica.
- Descarga de documentos adjuntos de las actuaciones (posible fase 2).
- Escritura sobre el archivo Excel. **El script nunca modifica la hoja de entrada.**

---

## 2. Entorno de ejecución

| Elemento | Decisión |
|---|---|
| Lenguaje | Python 3.11+ |
| Sistema | Linux sin entorno gráfico (Debian/Ubuntu Server) en un portátil dedicado |
| Programación | Timer de systemd (**no cron**) con `Persistent=true` |
| Zona horaria | `America/Bogota` — obligatorio, afecta horarios y comparación de fechas |
| Dependencias | `requests`, `openpyxl`. El resto, librería estándar |

Notas de despliegue (fuera del script, pero parte del montaje):

- Configurar `logind.conf` para que el portátil **no se suspenda al cerrar la tapa**.
- El timer debe usar `OnBootSec` además de los horarios, para recuperar corridas
  perdidas si el equipo estuvo apagado.

---

## 3. API del portal (verificada)

> **Nota sobre los ejemplos de esta sección:** las respuestas mostradas abajo
> se verificaron contra la API real durante el desarrollo. Los valores de
> `idProceso`, `idConexion`, `llaveProceso` y `sujetosProcesales` que
> aparecían originalmente correspondían a un caso real de un cliente y se
> reemplazaron por datos ficticios antes de publicar este documento. Lo que
> está "CONFIRMADO"/"verificado" es la **forma** de la respuesta, no los
> valores concretos que se ven acá.

Base: `https://consultaprocesos.ramajudicial.gov.co:448`

> ⚠️ **El puerto 448 es obligatorio.** Sin él la petición falla.
> Es una API no documentada y no oficial: puede cambiar sin aviso.

### 3.1 Consulta por número de radicación — CONFIRMADO

```
GET /api/v2/Procesos/Consulta/NumeroRadicacion?numero={23_digitos}&SoloActivos=false&pagina=1
```

Respuesta real verificada:

```json
{
  "tipoConsulta": "NumeroRadicacion",
  "procesos": [
    {
      "idProceso": 10000001,
      "idConexion": 999,
      "llaveProceso": "11001310301220200099900",
      "fechaProceso": "2019-11-12T00:00:00",
      "fechaUltimaActuacion": "2026-08-03T00:00:00",
      "despacho": "JUZGADO 012 CIVIL DEL CIRCUITO DE BOGOTÁ ",
      "departamento": "BOGOTÁ",
      "sujetosProcesales": "Demandante: MARIA EJEMPLO DE PRUEBA | Demandado: ...",
      "esPrivado": false,
      "cantFilas": -1
    }
  ],
  "parametros": { "...": null },
  "paginacion": {
    "cantidadRegistros": 1, "registrosPagina": 20,
    "cantidadPaginas": 1, "pagina": 1, "paginas": null
  }
}
```

Puntos críticos:

- **Usar `SoloActivos=false`.** La interfaz web manda `true` por defecto, pero eso
  oculta procesos inactivos o archivados, que pueden reactivarse con una actuación
  nueva. Traer todo y filtrar después, nunca antes.
- **`procesos` es una lista y puede traer más de un elemento.** Un mismo radicado
  puede existir en varios despachos o instancias. Recorrer la lista completa; no
  asumir `procesos[0]`.
- **`fechaUltimaActuacion` no tiene hora** — siempre `T00:00:00`. Ver sección 5.
- `esPrivado: true` significa que el proceso existe pero no expone actuaciones.
  **Confirmado en producción:** cuando esto pasa, `fechaProceso`,
  `fechaUltimaActuacion` e `idConexion` también vienen en `null` (no solo las
  actuaciones), y `sujetosProcesales` viene como el literal
  `"--- [ PROCESO PRIVADO ] ---"`. Cualquier código que toque
  `fechaUltimaActuacion` tiene que aceptar `None`, no solo el string
  `"...T00:00:00"` — un `proceso["esPrivado"] and proceso["fechaUltimaActuacion"] is None`
  sucedió en la primera corrida real contra los 32 radicados del usuario y
  tumbó el proceso completo hasta que se corrigió.
- Los campos de texto vienen **con espacios sobrantes al final**. Aplicar `.strip()`.
- `sujetosProcesales` es un string con partes separadas por `|`. Sirve para
  identificar el proceso en la notificación sin depender del Excel.

### 3.2 Actuaciones — CONFIRMADO

```
GET /api/v2/Proceso/Actuaciones/{idProceso}?pagina=1
```

Verificado con `idProceso = 10000001` el 2026-09-24. Respuesta real (recortada a
la primera actuación de la lista):

```json
{
  "actuaciones": [
    {
      "idRegActuacion": 1826026922,
      "llaveProceso": "11001310301220200099900",
      "consActuacion": 120,
      "fechaActuacion": "2026-09-15T00:00:00",
      "actuacion": "Recepción memorial",
      "anotacion": "ACUSO RECIBIDO ",
      "fechaInicial": null,
      "fechaFinal": null,
      "fechaRegistro": "2026-09-15T00:00:00",
      "codRegla": "00                              ",
      "conDocumentos": false,
      "cant": 120
    }
  ],
  "paginacion": {
    "cantidadRegistros": 120,
    "registrosPagina": 40,
    "cantidadPaginas": 3,
    "pagina": 1,
    "paginas": null
  }
}
```

Puntos confirmados:

- La lista viene en `actuaciones`, orden **descendente**: la actuación más
  reciente primero (`consActuacion` más alto = más reciente).
- **La cantidad total de actuaciones está en `paginacion.cantidadRegistros`.**
  Confirmado que la página 1 no trae todas: con `registrosPagina=40` y 120
  actuaciones en total, la página 1 solo trae 40 elementos. Nunca usar
  `len(actuaciones)` como si fuera el total — hay que leer
  `paginacion.cantidadRegistros`.
- Cada actuación individual también trae `cant`, que repite
  `paginacion.cantidadRegistros`. Es redundante pero sirve como chequeo
  cruzado; de todas formas usar `paginacion.cantidadRegistros` como fuente de
  verdad para el nivel 2 de detección (sección 5).
- **`anotacion` puede venir `null`** (no siempre es string). Validar antes de
  aplicar `.strip()`, igual que con los demás campos de texto — algunos traen
  espacios sobrantes (ej. `codRegla`).
- Campos por actuación relevantes para la notificación: `fechaActuacion`,
  `actuacion` (tipo/nombre), `anotacion`, `fechaInicial`, `fechaFinal`.
  `idRegActuacion` es un identificador único de actuación, útil si más
  adelante se necesita deduplicar o enlazar actuaciones puntuales.
- Con 120 actuaciones y 40 por página no hace falta paginar para el caso de
  uso actual: la actuación más reciente siempre está en la página 1 (va
  primero), y para el nivel 2 de detección solo se necesita el número de
  `paginacion.cantidadRegistros`, no el contenido completo de todas las
  páginas.
- No requiere autenticación; mismo puerto 448 y mismo dominio que el resto de
  la API.

### 3.3 Buenas maneras con el servidor

Es un servicio público y gratuito de una entidad del Estado. El script debe
comportarse como un usuario razonable, no como un raspador agresivo:

- Pausa de ~1 segundo entre peticiones.
- Timeout de 30 s por petición.
- Reintentos: 3 intentos con espera creciente (2s, 8s, 30s).
- `User-Agent` identificable, no el de `requests` por defecto.
- Una sola corrida a la vez (ver lockfile, sección 8).

### 3.4 El 404 como respuesta "blanda" — CONFIRMADO

Hallazgo al construir el cliente, no documentado en ningún lado: esta API usa
**HTTP 404 como forma normal de decir "sin resultados"**, no solo para rutas
que no existen. Confirmado en tres casos:

```
GET .../NumeroRadicacion?numero=00000000000000000000000&...
→ 404 {"StatusCode":404,"Message":"Error de prueba."}

GET .../Proceso/Actuaciones/1?pagina=1
→ 404 {"StatusCode":404,"Message":"No se encontraron Actuaciones para el Proceso: 1"}

GET .../NumeroRadicacion?numero=123&...
→ 404 {"StatusCode":404,"Message":"El parametro \"NumeroRadicacion\" ha de contener 23 digitos."}
```

Implicación para el cliente: **un 404 de esta API no es un fallo transitorio
de red y no debe entrar al ciclo de reintentos** (sección 3.3). Reintentarlo
con backoff (2s, 8s, 30s) es pura pérdida de tiempo — la respuesta es
determinista, va a volver a dar 404 las cuatro veces. Con muchos radicados
inválidos en el Excel, ese tiempo perdido se multiplica y puede hacer que la
corrida no termine en la ventana esperada.

Tratamiento:

- En la consulta por radicado, un 404 se interpreta como **"sin resultados"**
  y se comporta igual que si `procesos` viniera como lista vacía — no es un
  error, es información (radicado no existe o typo en el Excel, ver sección
  7).
- En la consulta de actuaciones, un `idProceso` normalmente viene de una
  consulta por radicado exitosa; un 404 ahí es anómalo (el proceso
  desapareció entre una consulta y otra) y sí debe tratarse como fallo a
  reportar, pero tampoco vale la pena reintentarlo con backoff — el
  resultado no va a cambiar en 30 segundos.
- Sí se siguen reintentando con backoff los fallos de red/timeout y los 5xx
  del servidor, que esos sí pueden ser transitorios.

### 3.5 Un Azure Application Gateway delante de la API, con 403 intermitentes — CONFIRMADO

Al correr el cliente contra los 32 radicados reales del usuario en una sola
corrida seguida, empezaron a aparecer respuestas `403 Forbidden` — no del
formato JSON `{"StatusCode":...}` de la propia API, sino HTML genérico con
`Microsoft-Azure-Application-Gateway/v2` en el cuerpo. A diferencia del 404
"blando" (sección 3.4), este 403 **no es la API diciendo algo** — es una capa
de infraestructura (WAF / gateway) delante de ella bloqueando la petición.

Patrón observado: casi no aparece al principio de una corrida, y se vuelve
más frecuente a medida que se acumulan peticiones seguidas (con la pausa de
~1s de la sección 3.3, empezó a aparecer después de las primeras ~10
peticiones y para el final de una corrida de ~70 peticiones estaba fallando
la mayoría de las veces en el primer intento). Eso apunta a un límite de tasa
o una regla anti-bot del gateway, no a un bloqueo permanente: las mismas
peticiones repetidas de forma aislada (sin la ráfaga previa) funcionan sin
problema.

**Decisión: el cliente reintenta el 403 con el mismo backoff que los 5xx**
(sección 3, `api_cliente.py`), a diferencia de otros 4xx que se consideran
deterministas y no se reintentan. Con esto una corrida completa de 32
radicados terminó sin errores, aunque más lenta de lo esperado (algunos
minutos en vez de bajo un minuto) por las esperas de reintento.

**Nota abierta para el usuario, no aplicada unilateralmente:** si en
producción esto se vuelve frecuente, subir `pausa_entre_peticiones` en el
TOML (sección 8) por encima de 1.0s probablemente reduce cuántas veces se
dispara el 403 en primer lugar, a costa de corridas un poco más largas — es
un ajuste operativo, no un cambio de código.

---

## 4. Entrada: archivo Excel

El script **lee** el archivo; nunca lo escribe. Motivo: si el usuario lo tiene abierto
en Excel, escribir causaría fallos o corrupción.

Columnas esperadas (nombres configurables, detectados por encabezado):

| Columna | Contenido | Obligatoria |
|---|---|---|
| Radicado | Número de 23 dígitos | Sí |
| Nombre | Persona relacionada con el caso | No |

Normalización del radicado antes de consultar:

- Quitar espacios, guiones, puntos y cualquier carácter no numérico.
- Validar que queden exactamente 23 dígitos.
- Si no cumple, **no consultar**: registrar el error y reportarlo en la notificación
  como radicado inválido, indicando la fila. No abortar la corrida entera.
- Ignorar filas vacías.
- Si hay radicados duplicados, consultar una sola vez (pero conservar cada fila para
  la notificación: dos filas con el mismo radicado pueden tener nombres distintos).

### Confirmado contra el Excel real del usuario

El archivo real (`procesos.xlsx`) no usa los nombres de columna del ejemplo — tiene
`NUMERO DE PROCESO` (radicado) y columnas separadas `demandante`/`demandado` en vez de
una sola `Nombre`. Decisión: usar **`demandante`** como columna de nombre.

El archivo real también trae columnas que no tienen nada que ver con este script —
`ultima Actuacion`, `fecha de actuacion`, `estado Anterior`, `terminos`, `telegram 1`,
`telegram 2`, `correo`, `corre2`, `observacion` — y una segunda hoja `festivos` con
fechas sueltas. **Decisión confirmada con el usuario: son restos de otro flujo de
trabajo, no de este proyecto.** El lector de Excel solo mira la hoja y las dos columnas
configuradas (`hoja`, `columna_radicado`, `columna_nombre` en el TOML); cualquier otra
columna u hoja se ignora sin más. Esto no cambia si el usuario reordena o agrega
columnas — el lector busca por nombre de encabezado, no por posición.

**Hallazgo de robustez, no anticipado en la versión original de esta sección:** si una
celda de radicado quedó con formato numérico en vez de texto, Excel ya pudo truncar el
valor por precisión antes de que el script lo vea (un `double` solo representa
~15-17 dígitos significativos, y el radicado tiene 23). El lector detecta este caso
por el *tipo* de la celda (`int`/`float` en vez de `str`) y lo reporta como radicado
inválido en vez de normalizarlo — normalizarlo a ciegas podría producir, por
casualidad, otros 23 dígitos que parezcan válidos pero correspondan a un proceso
equivocado.

---

## 5. Detección de cambios (núcleo del sistema)

### El problema

`fechaUltimaActuacion` solo tiene precisión de día. Si un despacho registra dos
actuaciones el mismo día y el script corre entre ambas, la fecha no cambia en la
segunda corrida y **esa actuación se pierde para siempre**. Con tres corridas
diarias esa ventana es real.

### La solución

Detección en dos niveles:

1. **Nivel barato (todos los procesos, cada corrida):** comparar
   `fechaUltimaActuacion` contra la guardada.
   - Si cambió → hay novedad. Traer actuaciones y notificar.

2. **Nivel de refuerzo (solo procesos recientemente activos):** si la fecha **no**
   cambió pero `fechaUltimaActuacion` está dentro de la ventana de vigilancia
   (configurable, por defecto 7 días), traer las actuaciones y comparar la
   **cantidad total** contra la guardada.
   - Si el conteo aumentó → hay novedad aunque la fecha sea la misma.

Como pocos procesos están activos en una ventana dada, el costo adicional en
peticiones es bajo.

### Estado persistente

SQLite en archivo local, **separado del Excel**. Tabla por proceso:

| Campo | Notas |
|---|---|
| `id_proceso` | Clave primaria |
| `llave_proceso` | Radicado de 23 dígitos |
| `id_conexion` | Algunos endpoints lo requieren |
| `despacho` | |
| `sujetos_procesales` | |
| `es_privado` | |
| `fecha_ultima_actuacion` | Base de la comparación nivel 1 |
| `cantidad_actuaciones` | Base de la comparación nivel 2 |
| `ultima_revision_ok` | Timestamp de la última consulta exitosa |

Conviene además una tabla de historial de corridas (timestamp, procesos
consultados, novedades, errores) para poder responder después "¿por qué no me
llegó tal aviso?".

### Primera ejecución

En la corrida inicial no hay con qué comparar y **todos los procesos parecerían
tener novedad**. La primera ejecución debe:

- Construir el estado base.
- **No enviar alertas de novedad.**
- Enviar un único mensaje de confirmación: cuántos procesos quedaron registrados
  y cuáles fallaron.

Lo mismo aplica a un radicado nuevo agregado al Excel: se registra en silencio y
se empieza a vigilar desde la corrida siguiente.

### Cómo se identifican las actuaciones nuevas (implementación)

Los dos niveles deciden *que* hay novedad, pero una vez que se dispara cualquiera
de los dos (nivel 1 por fecha, nivel 2 por conteo) hay que traer las actuaciones y
decidir *cuáles* de ellas son las nuevas, para ponerlas en la notificación.

**No se usa la fecha para esto — se usa la diferencia de conteo.** La API entrega
las actuaciones en orden descendente (la más reciente primero, sección 3.2). Si
`cantidad_actual - cantidad_guardada = N`, las N primeras del listado son las
nuevas. Esto funciona igual para el nivel 1 (la fecha cambió) que para el nivel 2
(mismo día, pero apareció una actuación más) — es precisamente el mecanismo por
`conteo` el que resuelve el problema descrito arriba, y no depende en absoluto de
comparar fechas, así que no hereda la ambigüedad que las motivó.

Si nunca hubo un conteo guardado para ese proceso (no debería pasar: todo proceso
se siembra con su conteo la primera vez que se ve — ver "Primera ejecución"
arriba), se asume 0 en vez de fallar: mejor reportar de más que quedarse en
silencio (principio de la sección 7).

### Proceso privado: "reportarlo una vez, no en cada corrida"

No hace falta una columna aparte para esto. `es_privado` ya se guarda en el
estado; se reporta cuando pasa de `false`/desconocido a `true` (una transición
más, del mismo tipo que las de nivel 1/nivel 2), y no se vuelve a reportar
mientras siga en `true`. Un proceso privado no expone actuaciones (sección 3.1),
así que para estos nunca se llama al endpoint de actuaciones.

---

## 6. Notificaciones

Arquitectura: una función `notificar(asunto, cuerpo, resumen)` con los canales
detrás. Los canales deben ser intercambiables y activables por configuración.

### Canal 1 — Correo (Gmail SMTP)

Lleva el **contenido completo**. Es el canal de registro consultable.

- `smtplib` con SSL, `smtp.gmail.com:465`.
- Requiere **contraseña de aplicación**, que a su vez requiere verificación en dos
  pasos activa en la cuenta. Si es una cuenta de Workspace, el administrador puede
  tenerlas deshabilitadas.
- **La cuenta remitente debe ser una cuenta nueva creada solo para esto**, no la
  personal ni la profesional del abogado. Así la credencial guardada en el portátil
  no da acceso a correo sensible si el equipo se pierde.

### Canal 2 — ntfy (push al teléfono)

Lleva el **aviso corto**. Un `POST` con el texto del mensaje a
`https://ntfy.sh/{tema}`.

- El nombre del tema debe ser una cadena aleatoria larga, no adivinable
  (ej. `k7x2m9qw4rt8pz`), no algo como `procesos-juzgado`.
- Sin autenticación ni cuenta. El usuario solo instala la app y se suscribe al tema.

### Formato de la notificación

**Un solo mensaje consolidado por corrida**, no uno por proceso. Con tres corridas
diarias, mensajes individuales se vuelven ruido y se dejan de leer.

El correo debe incluir, por cada proceso con novedad:

- Nombre de la persona (del Excel) y radicado.
- Despacho.
- La actuación nueva: tipo, fecha, anotación.
- Fechas de inicio y fin de término, si existen.
- Enlace al proceso en el portal — **CONFIRMADO**:
  ```
  https://consultaprocesos.ramajudicial.gov.co/Procesos/NumeroRadicacion?numero={radicado}&SoloActivos=false
  ```
  Sin el puerto 448 — ese puerto es solo para la API (sección 3); la página web
  para humanos corre en el puerto normal (443).

Motivo: un aviso que solo diga "hay una actualización" obliga a entrar al portal
igual, y no ahorra nada.

Debe incluir además, siempre y de forma visible:

- **Fecha y hora de la consulta.**
- Un descargo: los datos provienen de la réplica del portal, que tiene rezago, y
  esto no sustituye la verificación oficial. Relevante porque de esto dependen
  términos procesales.

### Si no hay novedades

No enviar push. Enviar correo solo si está activada la opción de "resumen diario"
(recomendado: un correo al día confirmando que el sistema corrió bien). El silencio
total es peligroso: es indistinguible de un script caído.

---

## 7. Manejo de errores

**Regla central: un fallo nunca debe producir silencio.** Si el script no pudo
consultar un proceso, el usuario tiene que enterarse, porque "sin noticias" no es
lo mismo que "sin novedades" cuando hay términos de por medio.

| Situación | Comportamiento |
|---|---|
| Un proceso falla | Registrar, continuar con los demás, reportarlo en la notificación |
| El portal está caído (todos fallan) | Notificar explícitamente "no se pudo consultar" |
| Radicado sin resultados | Reportarlo; puede ser error de digitación en el Excel |
| Proceso privado | Reportarlo una vez, no en cada corrida |
| Falla el envío de correo | Intentar por ntfy y dejarlo en el log |
| Fallan ambos canales | Log y código de salida distinto de cero |
| Excel no encontrado o ilegible | Abortar y notificar |
| El JSON no tiene la forma esperada | Abortar con mensaje claro: probablemente cambió la API |

**No actualizar el estado guardado de un proceso cuya consulta falló.** Si se
guardara, la novedad quedaría enmascarada en la corrida siguiente.

---

## 8. Configuración, registro y operación

### Configuración

Archivo TOML separado del código (`tomllib` es librería estándar en 3.11+):

```toml
[excel]
ruta = "/home/usuario/procesos.xlsx"
hoja = "Hoja 1"
columna_radicado = "NUMERO DE PROCESO"
columna_nombre = "demandante"

[deteccion]
ventana_vigilancia_dias = 7

[correo]
activo = true
servidor = "smtp.gmail.com"
puerto = 465
remitente = "..."
password_app = "..."     # contraseña de aplicación, NO la de la cuenta
destinatarios = ["..."]
resumen_diario = true    # un correo al día aunque no haya novedades ni fallos

[ntfy]
activo = true
tema = "..."

[red]
pausa_entre_peticiones = 1.0
timeout = 30
reintentos = 3

[estado]
ruta_db = "/home/usuario/monitor_procesos_estado.sqlite3"

[operacion]
ruta_log = "/home/usuario/monitor_procesos.log"
ruta_lockfile = "/home/usuario/monitor_procesos.lock"
```

`resumen_diario` (sección 6, "Si no hay novedades") y las secciones `[estado]`/
`[operacion]` no estaban en la primera versión de este TOML de ejemplo, pese a
que otras secciones del spec ya las daban por hechas — se completan acá.

El archivo contiene una credencial: permisos `600`, y fuera de control de versiones.

### Registro

- Log propio, legible, con rotación: qué se consultó, qué cambió, qué falló, qué se
  envió. Es la única forma de responder después por qué no llegó un aviso.
- `journalctl` recoge la salida estándar automáticamente vía systemd.

### Concurrencia

Lockfile para impedir dos corridas simultáneas. Si una corrida se atrasa y el timer
dispara la siguiente, la segunda debe salir sin hacer nada.

---

## 9. Criterios de aceptación

1. La primera corrida construye el estado y **no** genera alertas de novedad.
2. Una corrida sin cambios no envía push.
3. Un cambio en `fechaUltimaActuacion` genera notificación con el detalle de la
   actuación.
4. Una segunda actuación **en el mismo día** se detecta vía conteo (probar
   manipulando el conteo guardado en la base).
5. Un radicado inválido en el Excel no interrumpe la corrida y se reporta.
6. El portal caído produce una notificación explícita de fallo, no silencio.
7. El Excel no se modifica en ninguna circunstancia.
8. Un radicado agregado al Excel se incorpora sin intervención manual.
9. El script corre sin errores desde el timer de systemd, sin sesión interactiva.
10. Un fallo en la consulta de un proceso no actualiza su estado guardado.

---

## 10. Orden de trabajo sugerido

1. **Verificar el endpoint de actuaciones** con `idProceso = 10000001` y documentar
   la respuesta real en la sección 3.2.
2. Cliente de la API con reintentos y pausas, probado contra el radicado conocido.
3. Lectura y normalización del Excel.
4. Estado en SQLite y lógica de comparación de dos niveles.
5. Notificaciones: correo primero, ntfy después.
6. Manejo de errores y logging.
7. Empaquetado y montaje del servicio systemd.

Conviene un modo `--dry-run` que consulte y muestre en pantalla sin enviar nada ni
tocar el estado. Hace mucho más rápidas las pruebas.

---

## 11. Advertencias

- **API no oficial y no documentada.** Puede cambiar o desaparecer sin aviso. El
  script debe fallar de forma ruidosa y comprensible cuando eso pase, no en silencio.
- **El portal se cae con frecuencia** y tiene rezago de replicación de datos. Esto no
  es una fuente en tiempo real y no debe presentarse como tal al usuario.
- **Esto no sustituye la verificación oficial de términos.** Es una ayuda para no
  revisar cien radicados a mano, no una garantía procesal. El descargo en cada
  notificación no es un formalismo.
- Si en algún momento se considera mover la ejecución a un servidor en la nube:
  verificar antes que el portal no bloquee tráfico desde IPs de centros de datos
  extranjeros, cosa que ocurre con algunos portales públicos colombianos.

---

## 12. Despliegue (systemd)

Archivos en `systemd/` del repo: `monitor-procesos.service`,
`monitor-procesos.timer`, `logind-no-suspender-tapa.conf`. Los tres están
validados con `systemd-analyze verify` (sin errores) y las tres corridas
diarias del timer con `systemd-analyze calendar` — ninguno de estos comandos
instala ni activa nada, solo comprueban la sintaxis.

### 1. Entorno en el portátil dedicado

Requiere Python 3.11+ (sección 2, por `tomllib`). **Confirmado**: el portátil
real de despliegue corre Ubuntu 26.04.1 LTS con Python 3.14.4 de fábrica —
por encima del mínimo, `python3 -m venv` funciona directo con el Python del
sistema, sin instalar ninguna versión aparte.

```bash
cd /ruta/donde/quede/el/proyecto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Configuración

```bash
cp config.ejemplo.toml config.toml
# editar config.toml: rutas reales, credenciales de correo, tema de ntfy
chmod 600 config.toml
```

### 3. Zona horaria del sistema

Los horarios de `OnCalendar` del timer se evalúan en la zona horaria del
*sistema*, no en la que usa el script internamente (que ya está fija en
`America/Bogota` en el código — sección 2). Si el sistema no está en esa
zona, el timer dispara a la hora equivocada:

```bash
sudo timedatectl set-timezone America/Bogota
```

### 4. Instalar las unidades de systemd

Ajustar antes en `monitor-procesos.service` las rutas (`User=`,
`WorkingDirectory=`, `ExecStart=`) a donde quede el proyecto en el equipo
real, si es distinto de lo que trae el archivo. Si `ruta_db`, `ruta_log` o
`ruta_lockfile` del TOML apuntan fuera del directorio del proyecto, agregar
esas rutas a `ReadWritePaths=` en el `.service` — el endurecimiento
(`ProtectHome=read-only`, etc.) bloquea escrituras fuera de las rutas
listadas ahí.

```bash
sudo cp systemd/monitor-procesos.service systemd/monitor-procesos.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monitor-procesos.timer
```

### 5. Que no se suspenda al cerrar la tapa

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
sudo cp systemd/logind-no-suspender-tapa.conf /etc/systemd/logind.conf.d/
sudo systemctl restart systemd-logind
```

`systemctl restart systemd-logind` puede cerrar sesiones gráficas activas en
algunos entornos de escritorio — más seguro hacerlo justo después de instalar,
antes de dejar el equipo en producción, o directamente reiniciar el equipo
una vez.

### 6. Verificar

```bash
systemctl list-timers monitor-procesos.timer     # próxima corrida programada
sudo systemctl start monitor-procesos.service    # corrida manual, ya mismo
journalctl -u monitor-procesos.service -f        # seguir el log en vivo
```

Antes de dejarlo desatendido, probar con `--dry-run` (no envía nada ni toca el
estado — sección 10) corriendo el mismo `ExecStart` del `.service` a mano, y
luego una corrida real para confirmar el mensaje de "sistema registrado" de la
primera ejecución (sección 5, criterio de aceptación #1).

---

## 13. Verificación en sitio (para mostrarle al cliente)

Comandos simples, sin comillas anidadas ni caracteres raros — pensados para
tipear a mano frente al cliente sin margen de error de transcripción. Todos
se corren desde `/opt/monitor-procesos` salvo que se indique lo contrario.

### 1. El sistema está activo y programado

```bash
systemctl status monitor-procesos.timer
```

Buscar: `Active: active (running)` y un `Trigger:` con una fecha futura (no
`n/a` — si sale eso, probar `sudo systemctl restart monitor-procesos.timer`).

### 2. Arranca solo, sin que nadie tenga que hacer nada — ni tras un apagón

```bash
systemctl is-enabled monitor-procesos.timer
```

Debe decir `enabled`. Esto es lo que garantiza que, si el equipo se apaga y
se prende de nuevo, el monitoreo se reanuda solo.

### 3. Corrida en vivo, delante del cliente

```bash
sudo systemctl start monitor-procesos.service
journalctl -u monitor-procesos.service -n 20 --no-pager
```

Muestra la corrida recién disparada: qué radicados consultó y qué decidió
para cada uno (`PROCESO_NUEVO`, `SIN_CAMBIO`, `NOVEDAD_NIVEL1`, etc.).

### 4. Historial completo de la aplicación

```bash
tail -50 monitor_procesos.log
```

### 5. El Excel compartido está funcionando

```bash
systemctl status smbd
ls -la /srv/procesos-compartido/
```

(La prueba real es que el cliente lo abra y edite desde su propio Windows —
esto solo confirma que el servicio está corriendo del lado del servidor.)

### 6. No se suspende al cerrar la tapa

```bash
cat /etc/systemd/logind.conf.d/logind-no-suspender-tapa.conf
```

Debe mostrar las tres líneas `HandleLidSwitch*=ignore`.

### 7. Zona horaria correcta

```bash
timedatectl
```

Debe decir `Time zone: America/Bogota` y `System clock synchronized: yes`.

### 8. Próxima corrida programada

```bash
systemctl list-timers monitor-procesos.timer
```

### Si algo no coincide

- `journalctl -u monitor-procesos.service -n 50 --no-pager` — log detallado
  de la última corrida, incluye errores.
- El correo (`[correo]` en `config.toml`) queda desactivado hasta tener la
  cuenta de Gmail dedicada (sección 6) — mientras tanto, `ntfy` es el único
  canal activo, y eso es esperado, no un error.
