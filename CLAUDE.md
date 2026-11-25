# Reglas del proyecto Gestor de Registros WinCC

- Python 3.12 en `.venv`. Ejecutar siempre con `.venv\Scripts\python.exe`.
- **Dependencias**: toda dependencia nueva se instala con `scripts\agregar_dependencia.ps1 <paquete>`, que la
  fija con su version en `requirements.txt` y regenera `requirements-lock.txt`. Nunca `pip install` suelto.
- Codigo y textos de la interfaz en espanol. Tiempos internos = epoch ms en hora LOCAL de planta (como WinCC).
- La BD se elige solo por la URL de `config/config.yaml`; usar unicamente tipos/consultas SQLAlchemy genericos
  (nada especifico de SQLite) para poder migrar a SQL Server / PostgreSQL.
- La ingesta nunca debe perder ni duplicar datos: cualquier cambio en `app/ingest.py` debe mantener verdes
  `tests/test_ingest.py`, `tests/test_operacion.py` y `tests/test_propiedades.py`.
- **Pruebas**: antes de dar un cambio por terminado, `.\scripts\probar.ps1` (o `-Rapido`) debe acabar en
  "TODO EN VERDE": todas las pruebas pasan, sin avisos (pytest.ini los convierte en error) y cada modulo
  critico (ingest, wincc_csv, reports, auth) con cobertura >= 90 %. Codigo nuevo => pruebas nuevas.
  Endpoint nuevo => declararlo en la matriz de `tests/test_permisos_audit.py`.
- Todo cambio de configuracion hecho por usuarios (limites, variables, usuarios) se registra con `app.audit.record`.
- **Documentacion**: la que leen las personas va en HTML autocontenido en `docs/`. Los `.md` (README.md,
  CLAUDE.md) se conservan como contexto para la IA; al cambiar uno, actualizar tambien su copia
  `docs/README.html` / `docs/CLAUDE.html`.
