from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from .config import DEFAULT_COLLECT_TIME, SNMP_BACKEND, SNMP_PORT, SNMP_RETRIES, SNMP_TIMEOUT
from .db import (
    finish_collection_run,
    filtered_port_map,
    get_discovery_settings,
    list_collection_runs,
    list_switches,
    mark_discovery_finished,
    mark_discovery_started,
    now_text,
    prune_snapshots,
    save_arp_snapshots,
    save_mac_snapshots,
    start_collection_run,
)


try:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        bulk_walk_cmd,
    )

    PYSNMP_AVAILABLE = True
except Exception:  # noqa: BLE001
    PYSNMP_AVAILABLE = False


OID_ARP = "1.3.6.1.2.1.4.22"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
# 部分设备的 ifDescr 会返回 NULL0，而 ifName 才是 BAGG66。
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_FDB_Q_BRIDGE = "1.3.6.1.2.1.17.7.1.2.2"
OID_FDB_CLASSIC = "1.3.6.1.2.1.17.4.3"
OID_BASE_PORT_IF = "1.3.6.1.2.1.17.1.4.1.1"

LINE_RE = re.compile(r"^([\d.]+)\s*=\s*(.+)$")

_collector_lock = threading.Lock()
logger = logging.getLogger("netops.collection")


def _resolve_backend() -> str:
    value = SNMP_BACKEND.strip().lower()
    if value == "pysnmp":
        if PYSNMP_AVAILABLE:
            return "pysnmp"
        raise RuntimeError("SNMP_BACKEND=pysnmp 但未安装 pysnmp")
    if value == "cli":
        if shutil.which("snmpwalk"):
            return "cli"
        raise RuntimeError("SNMP_BACKEND=cli 但系统未安装 snmpwalk")
    if PYSNMP_AVAILABLE:
        return "pysnmp"
    if shutil.which("snmpwalk"):
        return "cli"
    raise RuntimeError("未找到可用的 SNMP 采集后端（pysnmp 或 snmpwalk）")


def parse_snmpwalk(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = LINE_RE.match(line)
        if not match:
            index += 1
            continue
        oid, raw = match.group(1), match.group(2).strip()
        if raw.lower().startswith("hex-string:"):
            parts = [raw.split(":", 1)[1].strip()]
            cursor = index + 1
            while cursor < len(lines) and not LINE_RE.match(lines[cursor].strip()):
                parts.append(lines[cursor].strip())
                cursor += 1
            raw = " ".join(parts)
            index = cursor
        else:
            index += 1
        entries.append((oid, raw))
    return entries


def parse_mac(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if ":" in text and len(text.replace(":", "")) == 12:
        return text.lower()
    match = re.search(r"([0-9a-fA-F]{2}(?:[-: ]?[0-9a-fA-F]{2}){5})", text)
    if match:
        digits = re.sub(r"[^0-9a-fA-F]", "", match.group(1))
        if len(digits) == 12:
            return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).lower()
    return ""


def parse_int(raw: Any) -> int | None:
    match = re.search(r"-?\d+", str(raw or ""))
    return int(match.group(0)) if match else None


def parse_str(raw: Any) -> str:
    text = re.sub(r"^STRING:\s*", "", str(raw or ""), flags=re.IGNORECASE).strip()
    return text.strip('"')


def _ip_from_suffix(suffix: str) -> str | None:
    parts = suffix.split(".")
    if len(parts) < 4:
        return None
    ip_parts = parts[-4:]
    if all(part.isdigit() and 0 <= int(part) <= 255 for part in ip_parts):
        return ".".join(ip_parts)
    return None


async def _walk_pysnmp(host: str, community: str, oid: str) -> list[tuple[str, str]]:
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (host, SNMP_PORT),
        timeout=SNMP_TIMEOUT,
        retries=SNMP_RETRIES,
    )
    rows: list[tuple[str, str]] = []
    async for error_indication, error_status, error_index, var_binds in bulk_walk_cmd(
        engine,
        CommunityData(community, mpModel=1),
        target,
        ContextData(),
        0,
        30,
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if error_indication:
            raise RuntimeError(str(error_indication))
        if error_status:
            raise RuntimeError(f"SNMP 错误: {error_status.prettyPrint()}")
        for var_bind in var_binds:
            name, value = var_bind
            rows.append((str(name), value.prettyPrint()))
    return rows


async def _walk_many_pysnmp(
    host: str, community: str, oids: list[str]
) -> list[list[tuple[str, str]] | Exception]:
    async def one(oid: str):
        return await _walk_pysnmp(host, community, oid)

    return list(await asyncio.gather(*(one(oid) for oid in oids), return_exceptions=True))


def _walk_cli(host: str, community: str, oid: str) -> list[tuple[str, str]]:
    if not shutil.which("snmpwalk"):
        raise RuntimeError("系统未安装 snmpwalk")
    command = [
        "snmpwalk",
        "-On",
        "-v2c",
        "-c",
        community,
        "-t",
        str(SNMP_TIMEOUT),
        "-r",
        str(SNMP_RETRIES),
        host,
        oid,
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=max(60, SNMP_TIMEOUT * 20),
    )
    if process.returncode != 0:
        message = (process.stderr or process.stdout or "").strip()
        raise RuntimeError(message or f"snmpwalk 退出码 {process.returncode}")
    return parse_snmpwalk(process.stdout)


def walk_oids_tolerant(
    host: str, community: str, oids: list[str]
) -> dict[str, list[tuple[str, str]] | Exception]:
    backend = _resolve_backend()
    if backend == "pysnmp":
        results = asyncio.run(_walk_many_pysnmp(host, community, oids))
        return dict(zip(oids, results))

    result_map: dict[str, list[tuple[str, str]] | Exception] = {}
    for oid in oids:
        try:
            result_map[oid] = _walk_cli(host, community, oid)
        except Exception as exc:  # noqa: BLE001
            result_map[oid] = exc
    return result_map


def collect_switch_arp(host: str, community: str) -> list[dict[str, Any]]:
    data = walk_oids_tolerant(host, community, [OID_ARP, OID_IF_DESCR])
    arp_entries = data.get(OID_ARP)
    if isinstance(arp_entries, Exception):
        raise RuntimeError(f"ARP 表采集失败: {arp_entries}")

    if_names: dict[int, str] = {}
    if not isinstance(data.get(OID_IF_DESCR), Exception):
        for oid, raw in data[OID_IF_DESCR]:  # type: ignore[union-attr]
            suffix = oid[len(OID_IF_DESCR) + 1 :]
            if suffix.isdigit():
                if_names[int(suffix)] = parse_str(raw)

    by_ip: dict[str, dict[str, Any]] = {}
    for oid, raw in arp_entries:
        if oid.startswith(OID_ARP + ".1.2."):
            ip = _ip_from_suffix(oid[len(OID_ARP + ".1.2.") :])
            if ip:
                mac = parse_mac(raw)
                if mac:
                    by_ip.setdefault(ip, {})["mac"] = mac
        elif oid.startswith(OID_ARP + ".1.1."):
            ip = _ip_from_suffix(oid[len(OID_ARP + ".1.1.") :])
            if ip:
                by_ip.setdefault(ip, {})["if_index"] = parse_int(raw)

    rows: list[dict[str, Any]] = []
    for ip, entry in by_ip.items():
        mac = entry.get("mac") or ""
        if not mac:
            continue
        if_index = entry.get("if_index")
        rows.append(
            {
                "ip": ip,
                "mac": mac,
                "if_index": if_index,
                "if_name": if_names.get(if_index, "") if if_index is not None else "",
            }
        )
    return rows


def collect_switch_fdb(host: str, community: str) -> list[dict[str, Any]]:
    data = walk_oids_tolerant(
        host, community, [OID_FDB_Q_BRIDGE, OID_BASE_PORT_IF, OID_IF_DESCR, OID_IF_NAME]
    )
    fdb_entries = data.get(OID_FDB_Q_BRIDGE)
    fdb_kind = "q"
    if isinstance(fdb_entries, Exception) or not fdb_entries:
        classic = walk_oids_tolerant(host, community, [OID_FDB_CLASSIC])
        fdb_entries = classic.get(OID_FDB_CLASSIC)
        fdb_kind = "classic"
    if isinstance(fdb_entries, Exception):
        raise RuntimeError(f"MAC 地址表采集失败: {fdb_entries}")
    if not fdb_entries:
        return []

    port_if: dict[int, int] = {}
    if not isinstance(data.get(OID_BASE_PORT_IF), Exception):
        for oid, raw in data[OID_BASE_PORT_IF]:  # type: ignore[union-attr]
            suffix = oid[len(OID_BASE_PORT_IF) + 1 :]
            if suffix.isdigit():
                value = parse_int(raw)
                if value is not None:
                    port_if[int(suffix)] = value

    if_descriptions: dict[int, str] = {}
    if_names: dict[int, str] = {}
    for oid_root, target in ((OID_IF_DESCR, if_descriptions), (OID_IF_NAME, if_names)):
        if isinstance(data.get(oid_root), Exception):
            continue
        for oid, raw in data[oid_root]:  # type: ignore[union-attr]
            suffix = oid[len(oid_root) + 1 :]
            if suffix.isdigit():
                target[int(suffix)] = parse_str(raw)

    filtered_ports = filtered_port_map().get(host, set())
    rows_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    status_entries: dict[tuple[str, int], str] = {}
    for oid, raw in fdb_entries:
        port_prefix = f"{OID_FDB_Q_BRIDGE if fdb_kind == 'q' else OID_FDB_CLASSIC}.1.2."
        status_prefix = f"{OID_FDB_Q_BRIDGE if fdb_kind == 'q' else OID_FDB_CLASSIC}.1.3."
        if oid.startswith(port_prefix):
            suffix = oid[len(port_prefix) :]
            parts = suffix.split(".")
            if fdb_kind == "q":
                if len(parts) < 7 or not all(part.isdigit() for part in parts[:7]):
                    continue
                vlan = int(parts[0])
                mac_parts = parts[1:7]
            else:
                if len(parts) < 6 or not all(part.isdigit() for part in parts[:6]):
                    continue
                vlan = 0
                mac_parts = parts[:6]
            mac_digits = "".join(f"{int(part):02x}" for part in mac_parts)
            mac = ":".join(mac_digits[i : i + 2] for i in range(0, 12, 2))
            port = parse_int(raw)
            if_index = port_if.get(port) if port is not None else None
            if port:
                fallback_port = f"端口{port}"
            else:
                fallback_port = "未知端口"
            existing = rows_by_key.get((mac, vlan), {})
            interface_names = (
                {
                    value.upper()
                    for value in (if_descriptions.get(if_index, ""), if_names.get(if_index, ""))
                    if value
                }
                if if_index is not None
                else set()
            )
            if interface_names & filtered_ports:
                continue
            if fallback_port.upper() in filtered_ports:
                continue
            rows_by_key[(mac, vlan)] = {
                "mac": mac,
                "vlan": vlan if fdb_kind == "q" else None,
                "if_index": if_index,
                "port_name": (
                    # NULL0 时优先使用 ifName，例如 BAGG66。
                    if_names.get(if_index) or if_descriptions.get(if_index, fallback_port)
                    if if_index is not None
                    else fallback_port
                ),
                "entry_type": existing.get("entry_type", ""),
            }
        elif oid.startswith(status_prefix):
            suffix = oid[len(status_prefix) :]
            parts = suffix.split(".")
            if fdb_kind == "q":
                if len(parts) < 7 or not all(part.isdigit() for part in parts[:7]):
                    continue
                vlan = int(parts[0])
                mac_parts = parts[1:7]
            else:
                if len(parts) < 6 or not all(part.isdigit() for part in parts[:6]):
                    continue
                vlan = 0
                mac_parts = parts[:6]
            mac_digits = "".join(f"{int(part):02x}" for part in mac_parts)
            mac = ":".join(mac_digits[i : i + 2] for i in range(0, 12, 2))
            status_entries[(mac, vlan)] = parse_str(raw)

    for key, status in status_entries.items():
        if key in rows_by_key:
            rows_by_key[key]["entry_type"] = status

    return list(rows_by_key.values())


def collect_switch_data(switch: dict[str, Any], kind: str) -> dict[str, Any]:
    host = switch["ip"]
    community = switch.get("snmp_community") or "public"
    role = switch.get("role") or "both"
    arp_rows: list[dict[str, Any]] = []
    mac_rows: list[dict[str, Any]] = []

    do_arp = kind in {"all", "arp"} and role in {"both", "core"}
    do_mac = kind in {"all", "mac"} and role in {"both", "access"}

    if do_arp:
        arp_rows = collect_switch_arp(host, community)
    if do_mac:
        mac_rows = collect_switch_fdb(host, community)

    return {
        "switch": switch.get("name", host),
        "ip": host,
        "arp": len(arp_rows),
        "mac": len(mac_rows),
        "collect_arp": do_arp,
        "collect_mac": do_mac,
        "arp_rows": arp_rows,
        "mac_rows": mac_rows,
    }


def run_collection(kind: str = "all") -> dict[str, Any]:
    if kind not in {"all", "arp", "mac"}:
        raise ValueError("采集类型只能是 all/arp/mac")
    if not _collector_lock.acquire(blocking=False):
        return {"started": False, "reason": "已有采集任务在运行"}

    run_id: int | None = None
    try:
        switches = [switch for switch in list_switches() if switch.get("enabled")]
        run_id = start_collection_run(kind)
        failed: list[str] = []
        record_count = 0
        change_count = 0
        success_count = 0
        workers = min(8, max(1, len(switches)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(collect_switch_data, switch, kind): switch for switch in switches
            }
            for future in as_completed(futures):
                switch = futures[future]
                try:
                    result = future.result()
                    collected_at = now_text()
                    arp_rows = result.get("arp_rows") or []
                    mac_rows = result.get("mac_rows") or []
                    record_count += len(arp_rows) + len(mac_rows)
                    if result.get("collect_arp"):
                        change_count += save_arp_snapshots(
                            result["ip"], arp_rows, collected_at, run_id
                        )
                    if result.get("collect_mac"):
                        change_count += save_mac_snapshots(
                            result["ip"], mac_rows, collected_at, run_id
                        )
                    success_count += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{switch.get('name')}({switch.get('ip')}): {exc}")

        prune_snapshots()
        finish_collection_run(
            run_id, len(switches), success_count, failed, record_count, change_count
        )
        result = {
            "started": True,
            "runId": run_id,
            "switchCount": len(switches),
            "successCount": success_count,
            "recordCount": record_count,
            "changeCount": change_count,
            "failed": failed,
        }
        logger.info(
            "采集完成：类型=%s，交换机=%s，成功=%s，记录=%s，变化=%s，失败=%s",
            kind, len(switches), success_count, record_count, change_count, "; ".join(failed) or "无",
        )
        return result
    except Exception as exc:  # noqa: BLE001
        if run_id is not None:
            finish_collection_run(run_id, 0, 0, [str(exc)], 0, 0)
        logger.exception("采集异常：类型=%s，错误=%s", kind, exc)
        return {"started": False, "error": str(exc)}
    finally:
        _collector_lock.release()


class DiscoveryScheduler(threading.Thread):
    """根据页面保存的开关和间隔执行自动全量发现。"""

    def __init__(self, poll_interval: int = 15) -> None:
        super().__init__(daemon=True, name="auto-discovery")
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            self._stop_event.wait(self.poll_interval)

    def _tick(self) -> None:
        settings = get_discovery_settings()
        if not settings.get("enabled"):
            return
        next_run = settings.get("next_run_at") or ""
        if next_run and next_run > now_text():
            return
        mark_discovery_started()
        logger.info("自动发现开始：间隔=%s 分钟", settings.get("interval_minutes"))
        try:
            run_collection("all")
        finally:
            mark_discovery_finished()


# 保留旧名称，避免已有脚本导入失败；新的服务入口使用 DiscoveryScheduler。
DailyScheduler = DiscoveryScheduler
