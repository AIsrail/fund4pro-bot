"""Loads .env into the process environment, then execs bot.py.

The bundled bot.py reads config purely via os.environ (no python-dotenv
dependency), so this tiny loader keeps the deploy self-contained without
requiring `export $(cat .env | xargs)` in a POSIX shell (fragile on Windows).
"""
import os
import runpy
import sys

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


if __name__ == "__main__":
    load_env(ENV_PATH)
    sys.argv = ["bot.py"]
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"), run_name="__main__")
