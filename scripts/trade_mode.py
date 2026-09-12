#!/usr/bin/env python3
"""Read account mode or recover to sim. Enable live only through the workbench."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
MODE_FILE = DSH_HOME / "trading-account-mode"


def read_mode():
    try:
        mode = MODE_FILE.read_text().strip()
    except FileNotFoundError:
        return "sim"
    if mode not in ("sim", "live"):
        raise ValueError("Invalid account mode; repair trading-account-mode before continuing")
    return mode


def recover_sim():
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    lock = DSH_HOME / "trading-workbench.lock"
    with lock.open("x") as handle:
        handle.write(str(os.getpid()))
    temporary = None
    try:
        if any(DSH_HOME.glob("trading-call-*.active")):
            raise ValueError("Account calls are in flight; use the workbench after they finish")
        with tempfile.NamedTemporaryFile(mode="w", dir=DSH_HOME, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write("sim\n")
        os.replace(temporary, MODE_FILE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("sim", "live"))
    args = parser.parse_args()
    try:
        if args.mode == "live":
            raise ValueError("Enable live in the Harness workbench with explicit user confirmation")
        if args.mode == "sim":
            recover_sim()
        print(read_mode())
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
