from pathlib import Path
import os, signal
p=Path(__file__).resolve().parent/'.server.pid'
if p.exists():
    try: os.kill(int(p.read_text().strip()), signal.SIGTERM)
    except Exception: pass
    try: p.unlink()
    except Exception: pass
