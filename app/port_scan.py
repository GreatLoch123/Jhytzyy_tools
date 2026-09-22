from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import PORT_TEST_MAX_PORTS, PORT_TEST_TIMEOUT
from .db import now_text


def _check_port(host: str, port: int, timeout: float) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"port": port, "result": "open", "detail": "端口可达"}
    except (socket.timeout, TimeoutError):
        return {"port": port, "result": "timeout", "detail": "连接超时"}
    except ConnectionRefusedError:
        return {"port": port, "result": "closed", "detail": "连接被拒绝"}
    except socket.gaierror as exc:
        return {"port": port, "result": "error", "detail": f"域名解析失败: {exc}"}
    except OSError as exc:
        return {"port": port, "result": "error", "detail": str(exc)}


def scan_ports(host: str, ports: list[int], target: str = "") -> dict[str, Any]:
    if not host or any(char.isspace() for char in host):
        raise ValueError("目标地址不能为空")

    normalized: list[int] = []
    for port in ports:
        try:
            value = int(port)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 65535:
            normalized.append(value)
    normalized = sorted(set(normalized))
    if not normalized:
        raise ValueError("端口无效")
    if len(normalized) > PORT_TEST_MAX_PORTS:
        raise ValueError(f"单次最多检测 {PORT_TEST_MAX_PORTS} 个端口")

    results: list[dict[str, Any]] = []
    workers = min(50, len(normalized))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_check_port, host, port, PORT_TEST_TIMEOUT): port
            for port in normalized
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: item["port"])
    tested_at = now_text()
    payload = {
        "host": host,
        "target": target,
        "results": results,
        "tested_at": tested_at,
    }
    return payload
