# Gestor de Registros WinCC

Visor e informes auditados de los registros CSV de **WinCC Runtime Advanced** (TIA Portal):
variables de proceso, analizador de red, alarmas y AuditTrail. Pensado para abrirse desde una
pestaña **Informes** de WinCC en el mismo PC.

## Estructura

```
GestorRegistros/
  app/                 código Python
    wincc_csv.py         lector de los CSV de WinCC (cp1252, coma decimal, $RT_OFF$, checksum)
    ingest.py            ingesta en 2 etapas con cola persistente (sin pérdida ni duplicados)
    models.py, db.py     modelo SQL (SQLAlchemy) — SQLite en desarrollo, cambiable por URL
    analytics.py         re-muestreo, min/max/media/mediana, anomalías
    limits.py            límites HH/H/L/LL: detección desde FileAlarms + configuración persistente
    auth.py              usuarios WinCC + desarrolladores, contraseñas, sesiones, bloqueo
    audit.py             audit trail propio encadenado por SHA-256
    reports.py           informes firmados: datos congelados, firmas, versiones, verificación
    report_pdf.py        maquetación del PDF (gráficas, estadística, valores tabulados, firmas)
    report_xlsx.py       el mismo informe en Excel (.xlsx)
    pades.py             sellos digitales: PAdES del PDF, .p7s del Excel, certificado
    api.py, main.py      API REST, tareas programadas, servidor web
  static/              interfaz web (HTML + JS + ECharts, sin compilación)
  config/config.yaml   proyectos, rutas, BD, horarios, retención
  scripts/             utilidades (ingesta manual, crear usuario, dependencias, arranque con Windows)
  tests/               pruebas automáticas (pytest)
  docs/                documentación en HTML (README.html, CLAUDE.html, Guia_Gestor_Registros.html)
  reportes_pruebas/    (generado por probar.ps1) evidencia de cada ejecución de pruebas y cobertura
  data/                (generado) BD SQLite, cola, estado, respaldos — no versionar
```

## Puesta en marcha (desarrollo)

```powershell
cd GestorRegistros
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-lock.txt
.venv\Scripts\python scripts\crear_usuario.py --usuario dev.nombre --rol desarrollador
.venv\Scripts\python run.py
```

Abrir <http://127.0.0.1:8080>. Al arrancar se vacía la cola y se leen los CSV nuevos.

Pruebas (167): `.\scripts\probar.ps1` ejecuta todo con cobertura y guarda la evidencia en
`reportes_pruebas\` (falla si una prueba falla o si ingest/wincc_csv/reports/auth bajan del 90 % de cobertura).
`-Rapido` omite la prueba de volumen de 3 meses. Solo pruebas: `.venv\Scripts\python -m pytest -q -m "not lento"`.
Detalle de cada prueba en `docs/Guia_Gestor_Registros.html`, apartado 9.

## Dependencias

- `requirements.txt`: dependencias **directas** con versión fijada.
- `requirements-lock.txt`: árbol completo (`pip freeze`) para instalación idéntica.
- Para añadir una: `.\scripts\agregar_dependencia.ps1 paquete` (instala, fija versión y regenera el lock).

## Proyectos

Cada proyecto WinCC se declara en `config/config.yaml` con su carpeta raíz y las subcarpetas de
`registros`, `analizador`, `alarmas` y `audit`. Se leen también los ficheros `.csv.keep*` que WinCC
crea al reiniciar.

## Ingesta sin pérdida de datos

WinCC guarda como máximo 500.000 registros por fichero. El gestor copia los datos a la base de datos
(retención configurable, por defecto 92 días ≈ 3 meses):

1. **Recolección**: copia cada CSV, toma las filas nuevas y escribe un **lote** en `data/queue/pending`
   (escritura atómica + fsync). Solo después avanza la marca de lectura (`data/state/read_state.json`).
2. **Carga**: cada lote entra a la BD en una transacción; la inserción es idempotente (lo ya cargado se
   ignora). Si la BD falla, el lote queda en cola y se reintenta.
3. Si WinCC reinicia el fichero (cambia la primera fila), se relee completo; los duplicados se descartan.

Horarios (`config.yaml → ingest`): lectura incremental cada 15 min y una **lectura diaria garantizada a
las 12:00** que además aplica la retención. Con 1 registro/min y 5 variables un fichero tarda ~69 días en
llenarse, así que aunque solo corriera la diaria no se perderían datos.

## Límites de alarma

`FileAlarms` dice qué niveles existen por tag (p. ej. `TT-001 Alarma HH`) pero **no el valor numérico**.
En la pestaña *Límites de alarma*:

- asocie cada variable a su tag (automático si el nombre empieza por el tag, p. ej. `TT-601.Proc_Var`);
- pulse **Sugerir**: estima el límite con el valor registrado al activarse cada alarma;
- revise/ajuste LL, L, H, HH y el rango del instrumento y pulse **Guardar**.

Se guardan **una sola vez** en la BD y en `data/limits_backup/<proyecto>.json`; si la BD se recrea se
restauran automáticamente. Cada cambio queda en el audit trail (antes/después, usuario, fecha).

Anomalías detectadas: alarma (HH/LL) y pre-alarma (H/L) con duración y pico, fuera de rango del
instrumento, dato inválido de WinCC (Validity ≠ 1), huecos (indica si fue por `$RT_OFF$`), valor
congelado y valores atípicos (z robusto MAD). Umbrales en `config.yaml → analysis`.

## Informes firmados (PDF y Excel)

Nunca se generan automáticamente: solo al pulsar **Generar reporte** en la pestaña *Análisis*, con el
rango, el muestreo y las variables seleccionados en ese momento.

Contenido del PDF (A4 apaisado):

1. Identificación (proyecto, periodo, muestreo, versión, hash SHA-256 de los datos) y resumen estadístico.
2. Una página por variable: gráfica (media, banda mín–máx, límites y zonas de alarma), estadística y
   eventos/anomalías.
3. **Valores tabulados** de todas las variables graficadas: valor medio de cada intervalo (el mismo que se
   grafica), celdas en rojo (fuera de HH/LL) o ámbar (fuera de H/L). Con pocas variables los bloques de
   filas se ponen lado a lado para ahorrar páginas.
4. Audit trail WinCC del periodo.
5. Firmas electrónicas: nombre, usuario, rol, significado y fecha/hora.

Firmas (usuario + contraseña re-ingresada + significado, registradas en el audit trail):

| Significado | Rol mínimo | Regla |
|---|---|---|
| Elaborado | operador | al generar el informe |
| Revisado | supervisor | otro usuario |
| Aprobado | supervisor | otro usuario, después de la revisión |

Los datos se **congelan** al generar (`data/reports/<id>/datos.json.gz`) y cada firma emite una nueva
versión con los mismos datos, **en PDF y en Excel** (`report.xlsx: true`). El PDF lleva un **sello digital
PAdES**. El Excel (.xlsx, mismo contenido: identificación, estadística, valores tabulados, gráficas nativas,
eventos, audit WinCC y firmas, con las hojas protegidas) va acompañado de un **sello .p7s** (firma CMS separada
con el mismo certificado). Por defecto el gestor crea su propio certificado en `data/certs` (para producción,
configure un certificado corporativo en `config.yaml → report.cert_pfx`). Se registra el SHA-256 de cada
.pdf, .xlsx y .p7s. En la pestaña *Informes*, **Verificar archivo** dice si una copia es auténtica, de qué
formato y versión. Los informes anteriores a la exportación Excel la generan al descargarla, a partir de los
mismos datos congelados.

Límite: `report.max_table_rows` (20.000 filas por defecto; 7 días a 1 min ≈ 10.080 filas ≈ 450 páginas
con 10 variables).

## Usuarios

- **WinCC**: se detectan en el AuditTrail (inicios de sesión) y se crean **inactivos**. WinCC no expone
  sus contraseñas, así que un administrador asigna una contraseña inicial y el usuario la cambia al entrar.
  El rol por defecto sale del grupo WinCC (Administrador → admin, Supervisor → supervisor, Operador → operador).
- **Desarrolladores**: cuentas propias (`origen = desarrollador`), separadas y trazables.
- Roles: lector < operador < supervisor (edita límites) < admin/desarrollador (usuarios, ingesta).
- Bloqueo tras 5 intentos fallidos; sesiones de 8 h.

## Integración con WinCC (pestaña Informes)

En la pantalla *Informes* de WinCC RT Advanced, un botón con la función de sistema **IniciarPrograma**
(StartProgram):

- Programa: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- Parámetros: `--app=http://127.0.0.1:8080`

Se abre como ventana propia sin barra de navegación. (El control *Navegador HTML* de RT Advanced usa el
motor de Internet Explorer y no es compatible con la interfaz.)

Para que el gestor arranque con Windows: `scripts\instalar_tarea_inicio.ps1` (como administrador).
Sin internet en planta: ejecutar antes `scripts\descargar_vendor.ps1` para guardar ECharts en local.

## Cambiar de motor SQL

Cambiar `database.url` en `config.yaml` (ejemplos de SQL Server y PostgreSQL en el propio fichero) e
instalar el driver con `agregar_dependencia.ps1` (`pyodbc` o `psycopg[binary]`). Las tablas se crean
solas al arrancar. Los límites se restauran desde `data/limits_backup`.

## Pendiente (siguientes fases)

- Migraciones de esquema con Alembic antes de pasar a producción.
- Paso del usuario activo de WinCC al abrir la pestaña (inicio de sesión único).
