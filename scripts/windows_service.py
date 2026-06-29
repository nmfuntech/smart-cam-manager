"""Gestione servizio Windows (NSSM), firewall e verifiche post-install."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from scripts.runtime_paths import installed_data_home, runtime_python

SERVICE_NAME = "BLACKFRAME"
DEFAULT_NSSM_DIR = Path(r"C:\Tools\nssm")
NSSM_DOWNLOAD_URL = "https://nssm.cc/release/nssm-2.24.zip"
NSSM_ZIP_EXE = "nssm-2.24/win64/nssm.exe"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def python_executable(root: Path | None = None) -> Path:
    root = root or project_root()
    return runtime_python(root)


def data_home(root: Path | None = None) -> Path:
    root = root or project_root()
    home = installed_data_home()
    if home is not None:
        return home
    return root


def service_log_path(root: Path | None = None) -> Path:
    return data_home(root) / "blackframe.log"


def find_nssm(candidates: list[Path] | None = None) -> Path | None:
    in_path = shutil.which("nssm")
    if in_path:
        return Path(in_path)
    extra = candidates or [
        DEFAULT_NSSM_DIR / "nssm.exe",
        Path(r"C:\nssm\nssm.exe"),
    ]
    for path in extra:
        if path.is_file():
            return path
    return None


def download_nssm(target_dir: Path = DEFAULT_NSSM_DIR) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "nssm.exe"
    if dest.is_file():
        return dest
    tmp_zip = target_dir / "nssm.zip"
    urllib.request.urlretrieve(NSSM_DOWNLOAD_URL, tmp_zip)  # noqa: S310
    with zipfile.ZipFile(tmp_zip) as archive:
        with archive.open(NSSM_ZIP_EXE) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    tmp_zip.unlink(missing_ok=True)
    return dest


def sc_query(service_name: str = SERVICE_NAME) -> dict[str, str]:
    proc = subprocess.run(
        ["sc", "query", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    result: dict[str, str] = {"exists": "false", "state": "unknown"}
    if proc.returncode != 0:
        return result
    result["exists"] = "true"
    for line in proc.stdout.splitlines():
        if "STATE" in line:
            upper = line.upper()
            if "RUNNING" in upper:
                result["state"] = "running"
            elif "STOPPED" in upper:
                result["state"] = "stopped"
            elif "START_PENDING" in upper:
                result["state"] = "start_pending"
            elif "STOP_PENDING" in upper:
                result["state"] = "stop_pending"
    return result


def list_port_listeners(port: int) -> list[dict[str, int | str]]:
    proc = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        check=False,
    )
    listeners: list[dict[str, int | str]] = []
    pattern = re.compile(rf"TCP\s+(\S+):{port}\s+\S+\s+LISTENING\s+(\d+)")
    for line in proc.stdout.splitlines():
        match = pattern.search(line)
        if match:
            listeners.append({"address": match.group(1), "pid": int(match.group(2))})
    return listeners


def health_check(host: str = "127.0.0.1", port: int = 8000, timeout: float = 5.0) -> bool:
    import urllib.error

    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            data = json.loads(body)
            return data.get("status") == "ok"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def run_nssm(nssm: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(nssm), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def install_nssm_service(
    root: Path | None = None,
    nssm: Path | None = None,
    service_name: str = SERVICE_NAME,
    port: int | None = None,
) -> dict[str, str]:
    root = (root or project_root()).resolve()
    nssm = nssm or find_nssm() or download_nssm()
    py = python_executable(root)
    script = "deploy\\serve_waitress.py"
    log_path = service_log_path(root)
    data_dir = data_home(root)

    status = sc_query(service_name)
    if status.get("exists") == "true":
        run_nssm(nssm, "stop", service_name)
        run_nssm(nssm, "remove", service_name, "confirm")

    steps = [
        ("install", service_name, str(py), script),
        ("set", service_name, "AppDirectory", str(root)),
        ("set", service_name, "DisplayName", "BLACKFRAME Camera Manager"),
        ("set", service_name, "Description", "Tapo camera monitoring with motion detection"),
        ("set", service_name, "Start", "SERVICE_AUTO_START"),
        ("set", service_name, "AppStdout", str(log_path)),
        ("set", service_name, "AppStderr", str(log_path)),
        ("set", service_name, "AppRotateFiles", "1"),
        ("set", service_name, "AppRotateBytes", "10485760"),
        ("set", service_name, "AppEnvironmentExtra", f"BLACKFRAME_HOME={data_dir}"),
    ]
    for step in steps:
        proc = run_nssm(nssm, *step)
        if proc.returncode != 0:
            raise RuntimeError(
                f"nssm {' '.join(step)} fallito: {(proc.stderr or proc.stdout).strip()}"
            )

    start = run_nssm(nssm, "start", service_name)
    if start.returncode != 0:
        raise RuntimeError(f"nssm start fallito: {(start.stderr or start.stdout).strip()}")

    check_port = port or int(os.getenv("APP_PORT", "8000"))
    if not health_check(port=check_port):
        return {
            "service": service_name,
            "nssm": str(nssm),
            "status": "started",
            "health": "pending",
        }
    return {
        "service": service_name,
        "nssm": str(nssm),
        "status": "running",
        "health": "ok",
    }


def install_task_scheduler(
    root: Path | None = None,
    task_name: str = SERVICE_NAME,
) -> dict[str, str]:
    root = (root or project_root()).resolve()
    bat = root / "start_blackframe.bat"
    if not bat.is_file():
        raise FileNotFoundError(f"Script di avvio mancante: {bat}")
    proc = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/TR",
            str(bat),
            "/SC",
            "ONSTART",
            "/RL",
            "HIGHEST",
            "/F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    return {"task": task_name, "status": "created", "script": str(bat)}


def open_firewall_port(port: int, rule_name: str = SERVICE_NAME) -> bool:
    ps = (
        f"$r = Get-NetFirewallRule -DisplayName '{rule_name}' -ErrorAction SilentlyContinue; "
        f"if (-not $r) {{ New-NetFirewallRule -DisplayName '{rule_name}' "
        f"-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow -Profile Private }}"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestione servizio BLACKFRAME su Windows")
    parser.add_argument("--root", default=str(project_root()))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Stato servizio e listener porta")
    p_install = sub.add_parser("install-nssm", help="Registra servizio NSSM")
    p_install.add_argument("--nssm", default="")
    p_install.add_argument("--port", type=int, default=None)
    sub.add_parser("install-task", help="Registra attività pianificata all'avvio")
    p_fw = sub.add_parser("open-firewall", help="Apre porta TCP nel firewall")
    p_fw.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8000")))
    p_health = sub.add_parser("health", help="Verifica endpoint /health")
    p_health.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8000")))

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.command == "status":
            status = sc_query()
            port = int(os.getenv("APP_PORT", "8000"))
            listeners = list_port_listeners(port)
            print(json.dumps({"service": status, "listeners": listeners}, indent=2))
            return 0
        if args.command == "install-nssm":
            nssm = Path(args.nssm) if args.nssm else None
            result = install_nssm_service(root=root, nssm=nssm, port=args.port)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "install-task":
            result = install_task_scheduler(root=root)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "open-firewall":
            ok = open_firewall_port(args.port)
            print(json.dumps({"port": args.port, "ok": ok}, indent=2))
            return 0 if ok else 1
        if args.command == "health":
            ok = health_check(port=args.port)
            print(json.dumps({"ok": ok, "port": args.port}, indent=2))
            return 0 if ok else 1
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
