"""Local launcher for the fast-only photo selector."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
APP = ROOT / "app.py"


def info(msg: str) -> None:
    print(f"[片刻] {msg}", flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(cmd: list[str], *, cwd: Path = ROOT) -> None:
    info(" ".join(cmd))
    subprocess.check_call(cmd, cwd=str(cwd))


def ensure_venv() -> Path:
    py = venv_python()
    if not py.exists():
        info("创建本地 Python 环境 .venv")
        run([sys.executable, "-m", "venv", str(VENV)])
    return py


def ensure_deps(py: Path) -> None:
    mirror = os.environ.get("PIANKE_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
    info("安装/检查极速模式依赖")
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "-i", mirror])
    run([str(py), "-m", "pip", "install", "-r", str(REQ), "-i", mirror])


def main() -> int:
    os.chdir(ROOT)
    py = ensure_venv()
    ensure_deps(py)
    port = os.environ.get("PIC_SELECTER_PORT", "5057")
    info(f"启动应用：http://localhost:{port}")
    return subprocess.call([str(py), str(APP), "--port", port])


if __name__ == "__main__":
    raise SystemExit(main())
