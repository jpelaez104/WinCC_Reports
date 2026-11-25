"""Crea una cuenta (normalmente el primer administrador / desarrollador).

Uso:
  .venv\\Scripts\\python scripts\\crear_usuario.py --usuario dev.juan --rol desarrollador
  (la contrasena se pide por consola, no queda en el historial)
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usuario", required=True)
    ap.add_argument("--rol", default="desarrollador", choices=list(auth.ROLE_LEVEL))
    ap.add_argument("--origen", default="desarrollador", choices=["desarrollador", "wincc"])
    ap.add_argument("--nombre", default=None)
    args = ap.parse_args()
    init_db()
    pw = getpass.getpass("Contrasena: ")
    if pw != getpass.getpass("Repita la contrasena: "):
        sys.exit("Las contrasenas no coinciden")
    try:
        u = auth.create_user(args.usuario, pw, args.rol, args.origen, actor="consola", display_name=args.nombre,
                             must_change=False)
    except auth.AuthError as e:
        sys.exit(f"Error: {e}")
    print(f"Usuario creado: {u['username']} (rol {u['role']}, origen {u['source']})")


if __name__ == "__main__":
    main()
