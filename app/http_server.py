from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .collector import run_collection
from .config import BASE_DIR
from .db import (
    arp_history,
    build_locate_items,
    collect_status,
    delete_ip_user,
    delete_nav_link,
    delete_port_filter,
    delete_remote_target,
    delete_switch,
    get_discovery_settings,
    import_port_filter_file,
    import_ip_user_csv_text,
    import_ip_user_file,
    import_ip_user_rows,
    list_collection_runs,
    list_ip_users,
    list_nav_links,
    list_port_filters_grouped,
    list_remote_targets,
    list_switches,
    locate_stats,
    mac_history,
    seed_defaults,
    snapshot_stats,
    sync_auto_port_filters,
    sync_switches_from_workbook,
    update_switch,
    update_discovery_settings,
    upsert_port_filter,
    upsert_ip_user,
    upsert_nav_link,
    upsert_remote_target,
)
from .port_scan import scan_ports
from .topology import load_topology, load_zabbix_diagnostics, test_zabbix_connection


class NetOpsHandler(SimpleHTTPRequestHandler):
    server_version = "NetOpsServer/0.2"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self.send_json({"ok": True})
        elif path == "/api/topology":
            self._guard(lambda: load_topology())
        elif path == "/api/zabbix/test":
            self._guard(lambda: test_zabbix_connection())
        elif path == "/api/zabbix/diagnostics":
            self._guard(lambda: load_zabbix_diagnostics())
        elif path == "/api/nav-links":
            self.send_json({"items": list_nav_links()})
        elif path == "/api/ip-users":
            self.send_json({"items": list_ip_users(q=query.get("q", [""])[0], limit=int(query.get("limit", ["500"])[0]))})
        elif path == "/api/ip-users/export":
            self.send_json({"items": list_ip_users(limit=100000)})
        elif path == "/api/locate":
            self._guard(
                lambda: build_locate_items(
                    q=query.get("q", [""])[0],
                    filter_by=query.get("filter", ["all"])[0],
                    limit=int(query.get("limit", ["500"])[0]),
                    mode=query.get("mode", ["inventory"])[0],
                )
            )
        elif path == "/api/locate/stats":
            self.send_json(locate_stats())
        elif path == "/api/locate/export":
            self._guard(lambda: build_locate_items(limit=100000, mode=query.get("mode", ["inventory"])[0]))
        elif path == "/api/arp-history":
            self.send_json(
                {"items": arp_history(query.get("ip", [""])[0], limit=int(query.get("limit", ["200"])[0]))}
            )
        elif path == "/api/mac-history":
            self.send_json(
                {"items": mac_history(query.get("mac", [""])[0], limit=int(query.get("limit", ["200"])[0]))}
            )
        elif path == "/api/remote-targets":
            self.send_json({"items": list_remote_targets()})
        elif path == "/api/switches":
            self.send_json({"items": list_switches()})
        elif path == "/api/collect/status":
            self.send_json(collect_status())
        elif path == "/api/discovery-settings":
            self.send_json(get_discovery_settings())
        elif path == "/api/collect/runs":
            self.send_json(
                {"items": list_collection_runs(kind=query.get("kind", [""])[0], limit=1)}
            )
        elif path == "/api/snapshot/stats":
            self.send_json(snapshot_stats())
        elif path == "/api/port-filters":
            self.send_json({"groups": list_port_filters_grouped()})
        elif path == "/api/port-filters/export":
            self.send_json({"items": [item for group in list_port_filters_grouped() for item in group["port_filters"]]})
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/nav-links":
            self._guard(lambda: upsert_nav_link(self.read_json()))
        elif path == "/api/ip-users":
            self._guard(lambda: upsert_ip_user(self.read_json()))
        elif path == "/api/ip-users/import":
            self._guard(self._import_ip_users)
        elif path == "/api/port-filters/import":
            self._guard(self._import_port_filters)
        elif path == "/api/remote-targets":
            self._guard(lambda: upsert_remote_target(self.read_json()))
        elif path == "/api/port-test":
            self._guard(self._run_port_test)
        elif path == "/api/collect/now":
            kind = query.get("kind", ["all"])[0]
            if kind not in {"all", "arp", "mac"}:
                self.send_json({"error": "采集类型无效"}, HTTPStatus.BAD_REQUEST)
                return
            threading.Thread(target=run_collection, args=(kind,), daemon=True).start()
            self.send_json({"started": True, "kind": kind})
        elif path == "/api/discovery-settings":
            self._guard(lambda: update_discovery_settings(self.read_json()))
        elif path == "/api/switches/sync":
            self._guard(lambda: sync_switches_from_workbook())
        elif path == "/api/port-filters":
            self._guard(lambda: upsert_port_filter(self.read_json()))
        elif path == "/api/port-filters/sync-auto":
            self._guard(lambda: sync_auto_port_filters())
        else:
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        match = re.fullmatch(r"/api/nav-links/(\d+)", path)
        if match:
            self._guard(lambda: upsert_nav_link(self.read_json(), int(match.group(1))))
            return
        match = re.fullmatch(r"/api/ip-users/(\d+)", path)
        if match:
            self._guard(lambda: upsert_ip_user(self.read_json(), int(match.group(1))))
            return
        match = re.fullmatch(r"/api/remote-targets/(\d+)", path)
        if match:
            self._guard(lambda: upsert_remote_target(self.read_json(), int(match.group(1))))
            return
        match = re.fullmatch(r"/api/switches/(\d+)", path)
        if match:
            self._guard(lambda: update_switch(int(match.group(1)), self.read_json()))
            return
        match = re.fullmatch(r"/api/port-filters/(\d+)", path)
        if match:
            self._guard(lambda: upsert_port_filter(self.read_json(), int(match.group(1))))
            return
        self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        match = re.fullmatch(r"/api/nav-links/(\d+)", path)
        if match:
            self._guard(lambda: {"deleted": delete_nav_link(int(match.group(1)))})
            return
        match = re.fullmatch(r"/api/ip-users/(\d+)", path)
        if match:
            self._guard(lambda: {"deleted": delete_ip_user(int(match.group(1)))})
            return
        match = re.fullmatch(r"/api/remote-targets/(\d+)", path)
        if match:
            self._guard(lambda: {"deleted": delete_remote_target(int(match.group(1)))})
            return
        match = re.fullmatch(r"/api/port-filters/(\d+)", path)
        if match:
            self._guard(lambda: {"deleted": delete_port_filter(int(match.group(1)))})
            return
        match = re.fullmatch(r"/api/switches/(\d+)", path)
        if match:
            self._guard(lambda: {"deleted": delete_switch(int(match.group(1)))})
            return
        self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _import_ip_users(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if content_type.startswith("application/json"):
            data = json.loads(raw.decode("utf-8") or "{}")
            if data.get("rows"):
                return import_ip_user_rows(data["rows"])
            if data.get("csv"):
                return import_ip_user_csv_text(data["csv"])
            raise ValueError("JSON 导入需要 rows 或 csv 字段")
        filename = (
            self.headers.get("X-Filename")
            or self.headers.get("X-File-Name")
            or self.headers.get("Content-Disposition", "")
        )
        if filename.startswith("attachment;") or "filename=" in filename:
            match = re.search(r'filename="?([^";]+)"?', filename)
            filename = match.group(1) if match else "upload.csv"
        return import_ip_user_file(filename or "upload.csv", raw)

    def _import_port_filters(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        filename = self.headers.get("X-Filename") or self.headers.get("X-File-Name") or "upload.csv"
        return import_port_filter_file(filename, raw)

    def _run_port_test(self) -> dict[str, Any]:
        data = self.read_json()
        return scan_ports(
            host=str(data.get("host", "")),
            ports=data.get("ports") or [],
            target=str(data.get("target", "")),
        )

    def _guard(self, action: Callable[[], Any]) -> None:
        try:
            payload = action()
            if isinstance(payload, tuple):
                status, body = payload
            else:
                status, body = HTTPStatus.OK, payload
            self.send_json(body, status)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        clean_path = parsed.path.lstrip("/")
        if not clean_path:
            clean_path = "index.html"
        target = (BASE_DIR / clean_path).resolve()
        if target.is_dir():
            target = target / "index.html"
        if BASE_DIR.resolve() not in target.parents and target != BASE_DIR.resolve():
            return str(BASE_DIR / "index.html")
        return str(target)

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "application/javascript"
        if path.endswith(".json"):
            return "application/json"
        if path.endswith(".svg"):
            return "image/svg+xml"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    seed_defaults()
    return ThreadingHTTPServer((host, port), NetOpsHandler)
