from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .config import DEFAULT_WORKBOOK, DEFAULT_ZABBIX_URL, ZABBIX_TIMEOUT_SECONDS


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass(frozen=True)
class SwitchRow:
    name: str
    ip: str
    uplink_ip: str
    uplink_port: str
    snmp_community: str
    level: int | None = None
    status: str = "up"


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []

    root = ElementTree.fromstring(xml)
    values: list[str] = []
    for item in root.findall("main:si", NS):
        parts = [node.text or "" for node in item.findall(".//main:t", NS)]
        values.append("".join(parts))
    return values


def read_first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rel_id = workbook.find("main:sheets/main:sheet", NS).attrib[
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ]
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("rel:Relationship", NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"]
            return "xl/" + target.lstrip("/")
    raise ValueError("Cannot find first worksheet in workbook.")


def read_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", NS)).strip()

    value_node = cell.find("main:v", NS)
    if value_node is None or value_node.text is None:
        return ""

    value = value_node.text
    if cell_type == "s":
        return shared_strings[int(value)].strip()
    return value.strip()


def read_workbook_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        raise FileNotFoundError(f"Workbook not found: {path}")

    with zipfile.ZipFile(path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet_path = read_first_sheet_path(archive)
        sheet = ElementTree.fromstring(archive.read(sheet_path))

    rows: list[list[str]] = []
    for row in sheet.findall(".//main:sheetData/main:row", NS):
        values: list[str] = []
        for cell in row.findall("main:c", NS):
            index = column_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            values[index] = read_cell_value(cell, shared_strings)
        if any(value.strip() for value in values):
            rows.append(values)
    return rows


def get_value(row: list[str], headers: dict[str, int], aliases: list[str]) -> str:
    for alias in aliases:
        index = headers.get(normalize_header(alias))
        if index is not None and index < len(row):
            return row[index].strip()
    return ""


def parse_level(value: str) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    if not match:
        return None
    return max(int(match.group(0)), 1)


def parse_switch_rows(path: Path) -> list[SwitchRow]:
    rows = read_workbook_rows(path)
    if not rows:
        return []

    headers = {normalize_header(value): index for index, value in enumerate(rows[0])}
    parsed: list[SwitchRow] = []

    for row in rows[1:]:
        name = get_value(row, headers, ["交换机名称", "名称", "switch_name"])
        ip = get_value(row, headers, ["交换机ip", "交换机IP", "ip", "switch_ip"])
        if not name or not ip:
            continue

        status = get_value(row, headers, ["状态", "链路状态", "status"]) or "up"
        parsed.append(
            SwitchRow(
                name=name,
                ip=ip,
                uplink_ip=get_value(row, headers, ["上联交换机ip", "上联交换机IP", "uplink_ip"]),
                uplink_port=get_value(
                    row,
                    headers,
                    ["连接上联交换机的端口", "上联端口", "uplink_port"],
                ),
                snmp_community=get_value(row, headers, ["SNMP账号", "snmp", "snmp_community"]),
                level=parse_level(
                    get_value(row, headers, ["等级", "层级", "级别", "level", "tier"])
                ),
                status="down" if status.lower() in {"down", "断开", "故障", "0"} else "up",
            )
        )

    return parsed


def infer_levels(rows: list[SwitchRow]) -> dict[str, int]:
    row_by_ip = {row.ip: row for row in rows}
    levels: dict[str, int] = {
        row.ip: row.level for row in rows if row.level is not None
    }

    def resolve(row: SwitchRow, visiting: set[str] | None = None) -> int:
        if row.ip in levels:
            return levels[row.ip]
        visiting = visiting or set()
        if row.ip in visiting:
            levels[row.ip] = 1
            return 1
        visiting.add(row.ip)
        if not row.uplink_ip or row.uplink_ip not in row_by_ip:
            levels[row.ip] = 1
            return 1
        levels[row.ip] = resolve(row_by_ip[row.uplink_ip], visiting) + 1
        return levels[row.ip]

    for row in rows:
        resolve(row)
    return levels


def build_topology(rows: list[SwitchRow]) -> dict[str, Any]:
    switches = []
    links = []
    levels = infer_levels(rows)
    rows_by_level: dict[int, list[SwitchRow]] = {}
    for row in rows:
        rows_by_level.setdefault(levels[row.ip], []).append(row)

    node_width = 144
    node_height = 70
    horizontal_gap = 160
    vertical_gap = 190
    margin_x = 120
    margin_y = 100
    max_level_count = max((len(items) for items in rows_by_level.values()), default=1)
    canvas_width = max(
        1200,
        margin_x * 2 + max_level_count * node_width + (max_level_count - 1) * horizontal_gap,
    )
    canvas_height = max(
        760,
        margin_y * 2 + max(len(rows_by_level) - 1, 0) * vertical_gap + node_height,
    )
    positions: dict[str, tuple[int, int]] = {}

    for level in sorted(rows_by_level):
        level_rows = rows_by_level[level]
        total_width = len(level_rows) * node_width + max(len(level_rows) - 1, 0) * horizontal_gap
        start_x = (canvas_width - total_width) / 2 + node_width / 2
        y = margin_y + (level - 1) * vertical_gap
        for index, row in enumerate(level_rows):
            x = start_x + index * (node_width + horizontal_gap)
            positions[row.ip] = (round(x), round(y))

    for index, row in enumerate(rows):
        x, y = positions[row.ip]
        switches.append(
            {
                "id": row.ip,
                "name": row.name,
                "ip": row.ip,
                "snmpCommunity": row.snmp_community,
                "level": levels[row.ip],
                "x": round(x),
                "y": round(y),
            }
        )

        if row.uplink_ip:
            links.append(
                {
                    "id": f"{row.ip}->{row.uplink_ip}:{row.uplink_port or 'uplink'}",
                    "source": row.ip,
                    "target": row.uplink_ip,
                    "sourcePort": row.uplink_port,
                    "status": row.status,
                    "bandwidth": "未知",
                }
            )

    return {
        "switches": switches,
        "links": links,
        "canvas": {"width": round(canvas_width), "height": round(canvas_height)},
    }


class ZabbixClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = self.normalize_url(url)
        self.token = token
        self.request_id = 0

    @staticmethod
    def normalize_url(url: str) -> str:
        value = url.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            value = f"http://{value}"
        if not value.endswith("/api_jsonrpc.php"):
            value = f"{value}/api_jsonrpc.php"
        return value

    def call(self, method: str, params: dict[str, Any] | list[Any] | None = None) -> Any:
        self.request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self.request_id,
        }
        if method != "apiinfo.version":
            payload["auth"] = self.token

        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json-rpc"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=ZABBIX_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Zabbix 请求失败 [method={method}]: {exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"Zabbix 网络错误 [method={method}]: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Zabbix 响应解析失败 [method={method}]: {exc}") from exc

        if "error" in body:
            code = body["error"].get("code", "")
            message = body["error"].get("data") or body["error"].get("message") or "未知错误"
            raise RuntimeError(f"Zabbix API 错误 [method={method}, code={code}]: {message}")
        return body.get("result")

    def version(self) -> str:
        return str(self.call("apiinfo.version"))

    def get_hosts_by_ip(self, ips: list[str]) -> dict[str, dict[str, Any]]:
        if not ips:
            return {}
        interfaces = self.call(
            "hostinterface.get",
            {
                "output": ["interfaceid", "hostid", "ip", "type", "available", "error"],
                "filter": {"ip": ips},
            },
        )
        if not interfaces:
            return {}

        host_ids = list({intf["hostid"] for intf in interfaces})
        hosts = self.call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status", "available", "snmp_available"],
                "hostids": host_ids,
                "selectInterfaces": ["ip", "type", "available", "error"],
            },
        )
        host_map = {host["hostid"]: host for host in hosts}

        by_ip: dict[str, dict[str, Any]] = {}
        for intf in interfaces:
            ip = intf.get("ip", "")
            hostid = intf.get("hostid")
            if ip in ips and ip not in by_ip and hostid in host_map:
                by_ip[ip] = host_map[hostid]
        return by_ip

    def get_port_items(self, host_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not host_ids:
            return {}

        items = self.call(
            "item.get",
            {
                "output": [
                    "itemid",
                    "hostid",
                    "name",
                    "key_",
                    "lastvalue",
                    "lastclock",
                    "value_type",
                    "units",
                ],
                "hostids": host_ids,
                "monitored": True,
                "searchByAny": True,
                "search": {
                    "key_": "ifOperStatus",
                    "name": "Operational status",
                },
                "sortfield": "name",
            },
        )
        by_host: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_host.setdefault(item["hostid"], []).append(item)
        return by_host


def get_host_items(client: ZabbixClient, host_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not host_ids:
        return {}

    items = client.call(
        "item.get",
        {
            "output": ["itemid", "hostid", "name", "key_", "lastvalue", "lastclock", "units"],
            "hostids": host_ids,
            "monitored": True,
            "search": {
                "key_": [
                    "ifOperStatus", "ifAdminStatus",
                    "ifHighSpeed", "ifSpeed",
                    "ifInOctets", "ifOutOctets",
                ],
            },
            "searchByAny": True,
            "sortfield": "name",
            "limit": 5000,
        },
    )
    by_host: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_host.setdefault(item["hostid"], []).append(item)
    return by_host


def normalize_port(value: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9/]+", "", value or "").lower()
    replacements = {
        "tengigabitethernet": "te",
        "xgigabitethernet": "te",
        "xge": "te",
        "fortygigabitethernet": "fo",
        "hundredgigabitethernet": "hu",
        "gigabitethernet": "gi",
        "fastethernet": "fa",
        "ethernet": "eth",
    }
    for old, new in replacements.items():
        compact = compact.replace(old, new)
    return compact


def item_matches_port(item: dict[str, Any], port: str) -> bool:
    expected = normalize_port(port)
    if not expected:
        return False

    haystack = normalize_port(f"{item.get('name', '')} {item.get('key_', '')}")
    if expected in haystack or haystack in expected:
        return True

    parts = expected.split("/")
    if len(parts) == 4 and parts[2] == "0":
        alt = f"{parts[0]}/{parts[2]}/{parts[3]}"
        if alt in haystack or haystack in alt:
            return True

    return False


def item_is_status(item: dict[str, Any]) -> bool:
    text = f"{item.get('name', '')} {item.get('key_', '')}".lower()
    return "ifoperstatus" in text or "operational status" in text


def item_is_speed(item: dict[str, Any]) -> bool:
    text = f"{item.get('name', '')} {item.get('key_', '')}".lower()
    return any(marker in text for marker in ["ifhighspeed", "ifspeed", "speed", "速率", "带宽"])


def find_port_item(items: list[dict[str, Any]], port: str, item_filter) -> dict[str, Any] | None:
    return next(
        (item for item in items if item_matches_port(item, port) and item_filter(item)),
        None,
    )


def format_bandwidth(value: str, units: str = "") -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "未知"

    if numeric <= 0:
        return "未知"

    normalized_units = units.lower()
    bits_per_second = numeric
    if normalized_units in {"bps", "b/s", "bit/s", ""}:
        bits_per_second = numeric
    elif normalized_units in {"kbps", "kbit/s"}:
        bits_per_second = numeric * 1_000
    elif normalized_units in {"mbps", "mbit/s"}:
        bits_per_second = numeric * 1_000_000
    elif normalized_units in {"gbps", "gbit/s"}:
        bits_per_second = numeric * 1_000_000_000

    for threshold, label in [
        (1_000_000_000, "G"),
        (1_000_000, "M"),
        (1_000, "K"),
    ]:
        if bits_per_second >= threshold:
            amount = bits_per_second / threshold
            text = f"{amount:.1f}".rstrip("0").rstrip(".")
            return f"{text}{label}"
    return f"{int(bits_per_second)}bps"


def zabbix_value_is_down(value: str) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"2", "down", "lowerlayerdown", "notpresent", "testing", "unknown", "0"}:
        return True
    if normalized in {"1", "up"}:
        return False
    return None


def load_zabbix_client() -> ZabbixClient | None:
    token = os.getenv("ZABBIX_TOKEN", "").strip()
    if not token:
        return None
    return ZabbixClient(os.getenv("ZABBIX_URL", DEFAULT_ZABBIX_URL), token)


def enrich_topology_from_zabbix(
    topology: dict[str, Any],
    rows: list[SwitchRow],
    client: ZabbixClient | None,
) -> dict[str, Any]:
    if client is None:
        topology["meta"]["collector"] = "static-xlsx"
        topology["meta"]["zabbix"] = {"enabled": False, "reason": "ZABBIX_TOKEN 未设置"}
        return topology

    try:
        hosts_by_ip = client.get_hosts_by_ip([row.ip for row in rows])
        items_by_host = get_host_items(client, [host["hostid"] for host in hosts_by_ip.values()])
        rows_by_ip = {row.ip: row for row in rows}

        for switch in topology["switches"]:
            host = hosts_by_ip.get(switch["ip"])
            if not host:
                switch["zabbix"] = {"matched": False}
                continue

            switch["zabbix"] = {
                "matched": True,
                "hostid": host["hostid"],
                "host": host.get("host"),
                "name": host.get("name"),
            }

        for link in topology["links"]:
            row = rows_by_ip.get(link["source"])
            host = hosts_by_ip.get(link["source"])
            if not row or not host:
                link["status"] = "down"
                link["statusSource"] = "zabbix-unreachable"
                link["zabbix"] = {"matched": False, "reason": "交换机IP在Zabbix中未找到"}
                continue

            host_items = items_by_host.get(host["hostid"], [])
            status_item = find_port_item(host_items, row.uplink_port, item_is_status)
            speed_item = find_port_item(host_items, row.uplink_port, item_is_speed)

            if speed_item:
                link["bandwidth"] = format_bandwidth(
                    speed_item.get("lastvalue", ""),
                    speed_item.get("units", ""),
                )

            if not status_item:
                link["status"] = "down"
                link["statusSource"] = "zabbix-unreachable"
                link["zabbix"] = {
                    "matched": False,
                    "reason": "上联端口在Zabbix中无对应监控项",
                    "speedItem": speed_item.get("itemid") if speed_item else None,
                }
                continue

            is_down = zabbix_value_is_down(status_item.get("lastvalue", ""))
            if is_down is not None:
                link["status"] = "down" if is_down else "up"
            link["statusSource"] = "zabbix"
            link["zabbix"] = {
                "matched": True,
                "statusItem": {
                    "itemid": status_item["itemid"],
                    "name": status_item.get("name"),
                    "key": status_item.get("key_"),
                    "lastvalue": status_item.get("lastvalue"),
                    "lastclock": status_item.get("lastclock"),
                },
                "speedItem": {
                    "itemid": speed_item["itemid"],
                    "name": speed_item.get("name"),
                    "key": speed_item.get("key_"),
                    "lastvalue": speed_item.get("lastvalue"),
                    "units": speed_item.get("units"),
                    "lastclock": speed_item.get("lastclock"),
                }
                if speed_item
                else None,
            }

        topology["meta"]["collector"] = "xlsx+zabbix"
        topology["meta"]["zabbix"] = {
            "enabled": True,
            "url": client.url.replace("/api_jsonrpc.php", ""),
            "matchedHosts": len(hosts_by_ip),
        }
    except Exception as exc:  # noqa: BLE001
        topology["meta"]["collector"] = "static-xlsx"
        topology["meta"]["zabbix"] = {"enabled": False, "error": str(exc)}
    return topology


def load_topology(workbook: Path = DEFAULT_WORKBOOK) -> dict[str, Any]:
    rows = parse_switch_rows(workbook)
    topology = build_topology(rows)
    topology["meta"] = {
        "source": workbook.name,
        "switchCount": len(topology["switches"]),
        "linkCount": len(topology["links"]),
        "collector": "static-xlsx",
    }
    return enrich_topology_from_zabbix(topology, rows, load_zabbix_client())


def test_zabbix_connection(workbook: Path = DEFAULT_WORKBOOK) -> dict[str, Any]:
    client = load_zabbix_client()
    if client is None:
        return {"enabled": False, "reason": "ZABBIX_TOKEN 未设置"}

    result: dict[str, Any] = {
        "enabled": True,
        "url": client.url,
        "tokenPreview": client.token[:8] + "..." if len(client.token) > 8 else "(空?)",
    }

    try:
        version = client.version()
        result["version"] = version
    except Exception as exc:
        result["versionError"] = str(exc)
        return result

    rows = parse_switch_rows(workbook)
    ips = [row.ip for row in rows]
    try:
        hosts_by_ip = client.get_hosts_by_ip(ips)
        result["matchedHostCount"] = len(hosts_by_ip)
        result["matchingRate"] = f"{len(hosts_by_ip)}/{len(ips)}"
        if len(hosts_by_ip) == 0:
            result["hint"] = "未找到任何匹配的 IP，请检查 Zabbix 中是否已添加这些交换机，或接口 IP 是否与表格一致"
    except Exception as exc:
        result["hostSearchError"] = str(exc)

    return result


def load_zabbix_diagnostics(workbook: Path = DEFAULT_WORKBOOK) -> dict[str, Any]:
    rows = parse_switch_rows(workbook)
    client = load_zabbix_client()
    if client is None:
        return {"enabled": False, "reason": "ZABBIX_TOKEN 未设置"}

    try:
        version = client.version()
    except Exception as exc:
        return {"enabled": False, "error": f"Zabbix 连通性测试失败: {exc}", "url": client.url}

    try:
        hosts_by_ip = client.get_hosts_by_ip([row.ip for row in rows])
    except Exception as exc:
        return {"enabled": False, "error": f"查询 Zabbix 主机失败: {exc}", "url": client.url, "version": version}

    if not hosts_by_ip:
        return {
            "enabled": False,
            "error": "Zabbix 中未找到表格中任何交换机的 IP 匹配",
            "url": client.url,
            "version": version,
            "switchCount": len(rows),
            "matchedHostCount": 0,
        }

    items_by_host = get_host_items(client, [host["hostid"] for host in hosts_by_ip.values()])
    details = []
    for row in rows:
        host = hosts_by_ip.get(row.ip)
        host_items = items_by_host.get(host["hostid"], []) if host else []
        status_items = [
            {
                "itemid": item["itemid"],
                "name": item.get("name"),
                "key": item.get("key_"),
                "lastvalue": item.get("lastvalue"),
            }
            for item in host_items
            if item_matches_port(item, row.uplink_port) and item_is_status(item)
        ]
        speed_items = [
            {
                "itemid": item["itemid"],
                "name": item.get("name"),
                "key": item.get("key_"),
                "lastvalue": item.get("lastvalue"),
                "units": item.get("units"),
                "bandwidth": format_bandwidth(item.get("lastvalue", ""), item.get("units", "")),
            }
            for item in host_items
            if item_matches_port(item, row.uplink_port) and item_is_speed(item)
        ]
        details.append(
            {
                "switch": row.name,
                "ip": row.ip,
                "uplinkPort": row.uplink_port,
                "hostMatched": bool(host),
                "host": host.get("name") if host else None,
                "statusItems": status_items[:10],
                "speedItems": speed_items[:10],
                "candidateItemCount": len(host_items),
            }
        )

    return {
        "enabled": True,
        "version": version,
        "url": client.url.replace("/api_jsonrpc.php", ""),
        "switchCount": len(rows),
        "matchedHostCount": len(hosts_by_ip),
        "details": details,
    }
