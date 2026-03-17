#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    inventory = subprocess.Popen([sys.executable, "inventory_server.py"], cwd=BASE_DIR)
    ai = subprocess.Popen([sys.executable, "ai_server.py"], cwd=BASE_DIR)

    print("HistorIA démarré.")
    print("- Inventory: http://127.0.0.1:4100")
    print("- AI/UI:     http://127.0.0.1:4200/indexe2.0.html")
    print("Ctrl+C pour arrêter les deux serveurs.")

    try:
        inventory.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in (ai, inventory):
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in (ai, inventory):
            if process.poll() is None:
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
