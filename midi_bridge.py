#!/usr/bin/env python3
"""Safe MIDI backend helpers for CoreMIDI/rtmidi instability cases."""

from __future__ import annotations

import json
import subprocess
from typing import Any


def _run_python_snippet(code: str, timeout: float = 3.0) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def safe_list_output_ports(timeout: float = 3.0) -> list[str]:
    code = (
        "import json, mido\n"
        "try:\n"
        "    print(json.dumps(mido.get_output_names()))\n"
        "except Exception:\n"
        "    print('[]')\n"
    )
    try:
        rc, out, _err = _run_python_snippet(code, timeout=timeout)
    except Exception:
        return []
    if rc != 0:
        return []
    try:
        parsed = json.loads(out or "[]")
        if isinstance(parsed, list):
            return [str(p) for p in parsed]
    except Exception:
        pass
    return []


def resolve_output_port_name(requested_name: str, ports: list[str] | None = None) -> str:
    req = requested_name.strip()
    if not req:
        return requested_name

    names = ports if ports is not None else safe_list_output_ports()
    if not names:
        return requested_name

    if req in names:
        return req

    req_l = req.lower()
    lower_map = {p.lower(): p for p in names}
    if req_l in lower_map:
        return lower_map[req_l]

    suffix_hits = [p for p in names if p.lower().endswith(req_l)]
    if len(suffix_hits) == 1:
        return suffix_hits[0]

    contains_hits = [p for p in names if req_l in p.lower()]
    if len(contains_hits) == 1:
        return contains_hits[0]

    return requested_name


def probe_output_port(port_name: str, timeout: float = 3.0) -> tuple[bool, str]:
    ports = safe_list_output_ports(timeout=timeout)
    resolved = resolve_output_port_name(port_name, ports=ports)
    safe_name = json.dumps(resolved)
    code = (
        "import mido\n"
        f"name = {safe_name}\n"
        "try:\n"
        "    p = mido.open_output(name)\n"
        "    p.close()\n"
        "    print('OK')\n"
        "except Exception as e:\n"
        "    print(f'ERR:{e}')\n"
    )
    try:
        rc, out, err = _run_python_snippet(code, timeout=timeout)
    except Exception as e:
        return False, str(e)
    if rc != 0:
        return False, err or out or "probe process failed"
    if out.startswith("OK"):
        if resolved != port_name:
            return True, f"ok (resolved '{port_name}' -> '{resolved}')"
        return True, "ok"
    if out.startswith("ERR:"):
        hint = f" available ports: {', '.join(ports)}" if ports else ""
        return False, out[4:] + hint
    return False, out or "unknown probe result"


def midi_health(port_name: str | None = None) -> dict[str, Any]:
    ports = safe_list_output_ports()
    out: dict[str, Any] = {
        "ports": ports,
        "port_count": len(ports),
        "requested_port": port_name,
    }
    if port_name:
        ok, detail = probe_output_port(port_name)
        out["requested_port_ok"] = ok
        out["requested_port_detail"] = detail
    return out
