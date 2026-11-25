"""Ejecuta una ingesta completa desde consola (util para pruebas o el Programador de tareas).

Uso:  .venv\\Scripts\\python scripts\\ingestar.py [--purgar]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, ingest  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purgar", action="store_true", help="aplicar retencion (borrar muestras antiguas)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    res = ingest.run_ingest(do_purge=args.purgar, reason="consola")
    res["usuarios_wincc"] = auth.sync_wincc_users()
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
