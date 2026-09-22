from __future__ import annotations

import csv
import io
import ipaddress
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DB_PATH, DEFAULT_WORKBOOK, DEFAULT_COLLECT_TIME, SNAPSHOT_RETENTION_DAYS
from .topology import parse_switch_rows, read_workbook_rows


SCHEMA = """
CREATE TABLE IF NOT EXISTS switches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  ip TEXT NOT NULL UNIQUE,
  uplink_ip TEXT DEFAULT '',
  uplink_port TEXT DEFAULT '',
  snmp_community TEXT DEFAULT 'public',
  role TEXT DEFAULT 'both',
  level INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS switch_port_filters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_ip TEXT NOT NULL,
  port_name TEXT NOT NULL,
  reason TEXT DEFAULT '',
  auto_generated INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(switch_ip, port_name)
);
CREATE INDEX IF NOT EXISTS idx_port_filter_switch ON switch_port_filters(switch_ip);

CREATE TABLE IF NOT EXISTS arp_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_ip TEXT NOT NULL,
  ip TEXT NOT NULL,
  mac TEXT DEFAULT '',
  if_index INTEGER,
  if_name TEXT DEFAULT '',
  collected_at TEXT NOT NULL,
  run_id INTEGER,
  UNIQUE(switch_ip, ip)
);
CREATE INDEX IF NOT EXISTS idx_arp_ip ON arp_snapshots(ip, collected_at);
CREATE INDEX IF NOT EXISTS idx_arp_switch ON arp_snapshots(switch_ip, collected_at);

CREATE TABLE IF NOT EXISTS mac_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_ip TEXT NOT NULL,
  mac TEXT NOT NULL,
  vlan INTEGER,
  if_index INTEGER,
  port_name TEXT DEFAULT '',
  entry_type TEXT DEFAULT '',
  collected_at TEXT NOT NULL,
  run_id INTEGER,
  UNIQUE(switch_ip, mac, vlan)
);
CREATE INDEX IF NOT EXISTS idx_mac_mac ON mac_snapshots(mac, collected_at);
CREATE INDEX IF NOT EXISTS idx_mac_switch ON mac_snapshots(switch_ip, collected_at);

-- 只记录发生变化的 ARP/MAC，未变化的数据不会重复入库。
CREATE TABLE IF NOT EXISTS arp_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_ip TEXT NOT NULL,
  ip TEXT NOT NULL,
  mac TEXT DEFAULT '',
  if_index INTEGER,
  if_name TEXT DEFAULT '',
  collected_at TEXT NOT NULL,
  run_id INTEGER,
  change_type TEXT NOT NULL DEFAULT 'changed'
);
CREATE INDEX IF NOT EXISTS idx_arp_changes_ip ON arp_changes(ip, collected_at);
CREATE INDEX IF NOT EXISTS idx_arp_changes_switch ON arp_changes(switch_ip, collected_at);

CREATE TABLE IF NOT EXISTS mac_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_ip TEXT NOT NULL,
  mac TEXT NOT NULL,
  vlan INTEGER NOT NULL DEFAULT 0,
  if_index INTEGER,
  port_name TEXT DEFAULT '',
  entry_type TEXT DEFAULT '',
  collected_at TEXT NOT NULL,
  run_id INTEGER,
  change_type TEXT NOT NULL DEFAULT 'changed'
);
CREATE INDEX IF NOT EXISTS idx_mac_changes_mac ON mac_changes(mac, collected_at);
CREATE INDEX IF NOT EXISTS idx_mac_changes_switch ON mac_changes(switch_ip, collected_at);

CREATE TABLE IF NOT EXISTS collection_switch_state (
  switch_ip TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('arp', 'mac')),
  last_run_id INTEGER NOT NULL,
  last_collected_at TEXT NOT NULL,
  PRIMARY KEY (switch_ip, kind)
);

-- 长期终端库：不受快照保留天数影响，每次采集只增量更新最后发现位置。
CREATE TABLE IF NOT EXISTS terminal_inventory (
  mac TEXT PRIMARY KEY,
  ip TEXT DEFAULT '',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_arp_at TEXT DEFAULT '',
  arp_switch_ip TEXT DEFAULT '',
  arp_if_name TEXT DEFAULT '',
  last_mac_at TEXT DEFAULT '',
  switch_ip TEXT DEFAULT '',
  port_name TEXT DEFAULT '',
  vlan INTEGER
);
CREATE INDEX IF NOT EXISTS idx_terminal_inventory_ip ON terminal_inventory(ip);

CREATE TABLE IF NOT EXISTS ip_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ip TEXT NOT NULL UNIQUE,
  user_name TEXT DEFAULT '',
  department TEXT DEFAULT '',
  location TEXT DEFAULT '',
  device_type TEXT DEFAULT '',
  note TEXT DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS nav_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  category TEXT DEFAULT '常用',
  icon TEXT DEFAULT '',
  sort_order INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS remote_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  host TEXT DEFAULT '',
  protocol TEXT DEFAULT 'RDP',
  default_ports TEXT DEFAULT '3389',
  category TEXT DEFAULT '',
  note TEXT DEFAULT '',
  sort_order INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  switch_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  failed TEXT DEFAULT '',
  record_count INTEGER DEFAULT 0,
  change_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS discovery_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  enabled INTEGER NOT NULL DEFAULT 0,
  interval_minutes INTEGER NOT NULL DEFAULT 60,
  last_started_at TEXT DEFAULT '',
  last_finished_at TEXT DEFAULT '',
  next_run_at TEXT DEFAULT ''
);
"""

IP_ALIASES = ["ip", "ip地址", "ipaddr", "地址"]
SWITCH_IP_ALIASES = ["交换机ip", "switch_ip", "switchip", "管理ip"]
PORT_ALIASES = ["端口", "端口名称", "port", "port_name", "接口"]
REASON_ALIASES = ["原因", "说明", "reason", "备注", "note"]
USER_ALIASES = ["使用人", "姓名", "用户名", "user", "user_name", "使用人员", "负责人"]
DEPARTMENT_ALIASES = ["部门", "科室", "单位", "department"]
LOCATION_ALIASES = ["位置", "地点", "安装位置", "楼层", "location", "场所"]
DEVICE_ALIASES = ["设备类型", "设备", "类型", "device", "devicetype"]
NOTE_ALIASES = ["备注", "说明", "note", "注释", "用途"]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    migrated = False
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO discovery_settings (id) VALUES (1)")
        run_columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(collection_runs)").fetchall()
        ]
        if "change_count" not in run_columns:
            conn.execute("ALTER TABLE collection_runs ADD COLUMN change_count INTEGER DEFAULT 0")
        for table in ("arp_snapshots", "mac_snapshots"):
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "run_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN run_id INTEGER")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table.replace('_snapshots', '')}_run ON {table}(run_id)")
            runs = conn.execute(
                "SELECT id, started_at, finished_at FROM collection_runs ORDER BY id"
            ).fetchall()
            for run in runs:
                end_time = run["finished_at"] or now_text()
                conn.execute(
                    f"UPDATE {table} SET run_id=? WHERE run_id IS NULL AND collected_at >= ? AND collected_at <= ?",
                    (run["id"], run["started_at"], end_time),
                )
        _backfill_terminal_inventory(conn)
        migrated = _migrate_snapshots_to_incremental(conn)
        conn.commit()
    if migrated:
        _compact_database()


def _migrate_snapshots_to_incremental(conn: sqlite3.Connection) -> bool:
    """把旧的全量快照迁移为当前状态表，并保留实际发生变化的记录。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 2:
        return False

    legacy_arp = conn.execute("SELECT COUNT(*) AS c FROM arp_snapshots").fetchone()["c"]
    legacy_mac = conn.execute("SELECT COUNT(*) AS c FROM mac_snapshots").fetchone()["c"]

    if legacy_arp and not conn.execute("SELECT 1 FROM arp_changes LIMIT 1").fetchone():
        conn.execute(
            """
            INSERT INTO arp_changes
            (switch_ip, ip, mac, if_index, if_name, collected_at, run_id, change_type)
            WITH ordered AS (
              SELECT
                switch_ip, ip, mac, if_index, if_name, collected_at, run_id, id,
                LAG(id) OVER (
                  PARTITION BY switch_ip, ip ORDER BY COALESCE(run_id, 0), id
                ) AS previous_id,
                LAG(mac) OVER (
                  PARTITION BY switch_ip, ip ORDER BY COALESCE(run_id, 0), id
                ) AS previous_mac,
                LAG(if_index) OVER (
                  PARTITION BY switch_ip, ip ORDER BY COALESCE(run_id, 0), id
                ) AS previous_if_index,
                LAG(if_name) OVER (
                  PARTITION BY switch_ip, ip ORDER BY COALESCE(run_id, 0), id
                ) AS previous_if_name
              FROM arp_snapshots
            )
            SELECT
              switch_ip, ip, mac, if_index, if_name, collected_at, run_id,
              CASE WHEN previous_id IS NULL THEN 'first' ELSE 'changed' END
            FROM ordered
            WHERE previous_id IS NULL
               OR IFNULL(mac, '') != IFNULL(previous_mac, '')
               OR IFNULL(if_index, -1) != IFNULL(previous_if_index, -1)
               OR IFNULL(if_name, '') != IFNULL(previous_if_name, '')
            """
        )

    if legacy_mac and not conn.execute("SELECT 1 FROM mac_changes LIMIT 1").fetchone():
        conn.execute(
            """
            INSERT INTO mac_changes
            (switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id, change_type)
            WITH ordered AS (
              SELECT
                switch_ip, mac, IFNULL(vlan, 0) AS vlan, if_index, port_name,
                entry_type, collected_at, run_id, id,
                LAG(id) OVER (
                  PARTITION BY switch_ip, mac, IFNULL(vlan, 0)
                  ORDER BY COALESCE(run_id, 0), id
                ) AS previous_id,
                LAG(if_index) OVER (
                  PARTITION BY switch_ip, mac, IFNULL(vlan, 0)
                  ORDER BY COALESCE(run_id, 0), id
                ) AS previous_if_index,
                LAG(port_name) OVER (
                  PARTITION BY switch_ip, mac, IFNULL(vlan, 0)
                  ORDER BY COALESCE(run_id, 0), id
                ) AS previous_port_name,
                LAG(entry_type) OVER (
                  PARTITION BY switch_ip, mac, IFNULL(vlan, 0)
                  ORDER BY COALESCE(run_id, 0), id
                ) AS previous_entry_type
              FROM mac_snapshots
            )
            SELECT
              switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id,
              CASE WHEN previous_id IS NULL THEN 'first' ELSE 'changed' END
            FROM ordered
            WHERE previous_id IS NULL
               OR IFNULL(if_index, -1) != IFNULL(previous_if_index, -1)
               OR IFNULL(port_name, '') != IFNULL(previous_port_name, '')
               OR IFNULL(entry_type, '') != IFNULL(previous_entry_type, '')
            """
        )

    conn.execute("DROP TABLE IF EXISTS arp_snapshots_incremental")
    conn.execute(
        """
        CREATE TABLE arp_snapshots_incremental (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          switch_ip TEXT NOT NULL,
          ip TEXT NOT NULL,
          mac TEXT DEFAULT '',
          if_index INTEGER,
          if_name TEXT DEFAULT '',
          collected_at TEXT NOT NULL,
          run_id INTEGER,
          UNIQUE(switch_ip, ip)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO arp_snapshots_incremental
        (switch_ip, ip, mac, if_index, if_name, collected_at, run_id)
        SELECT switch_ip, ip, mac, if_index, if_name, collected_at, run_id
        FROM (
          SELECT
            switch_ip, ip, mac, if_index, if_name, collected_at, run_id,
            ROW_NUMBER() OVER (
              PARTITION BY switch_ip, ip ORDER BY COALESCE(run_id, 0) DESC, id DESC
            ) AS row_number
          FROM arp_snapshots
        )
        WHERE row_number = 1
        """
    )

    conn.execute("DROP TABLE IF EXISTS mac_snapshots_incremental")
    conn.execute(
        """
        CREATE TABLE mac_snapshots_incremental (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          switch_ip TEXT NOT NULL,
          mac TEXT NOT NULL,
          vlan INTEGER NOT NULL DEFAULT 0,
          if_index INTEGER,
          port_name TEXT DEFAULT '',
          entry_type TEXT DEFAULT '',
          collected_at TEXT NOT NULL,
          run_id INTEGER,
          UNIQUE(switch_ip, mac, vlan)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO mac_snapshots_incremental
        (switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id)
        SELECT switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id
        FROM (
          SELECT
            switch_ip, mac, IFNULL(vlan, 0) AS vlan, if_index, port_name,
            entry_type, collected_at, run_id,
            ROW_NUMBER() OVER (
              PARTITION BY switch_ip, mac, IFNULL(vlan, 0)
              ORDER BY COALESCE(run_id, 0) DESC, id DESC
            ) AS row_number
          FROM mac_snapshots
        )
        WHERE row_number = 1
        """
    )

    conn.execute("DROP TABLE arp_snapshots")
    conn.execute("ALTER TABLE arp_snapshots_incremental RENAME TO arp_snapshots")
    conn.execute("DROP TABLE mac_snapshots")
    conn.execute("ALTER TABLE mac_snapshots_incremental RENAME TO mac_snapshots")

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_arp_ip ON arp_snapshots(ip, collected_at);
        CREATE INDEX IF NOT EXISTS idx_arp_switch ON arp_snapshots(switch_ip, collected_at);
        CREATE INDEX IF NOT EXISTS idx_mac_mac ON mac_snapshots(mac, collected_at);
        CREATE INDEX IF NOT EXISTS idx_mac_switch ON mac_snapshots(switch_ip, collected_at);
        """
    )
    conn.execute("DELETE FROM collection_switch_state")
    conn.execute(
        """
        INSERT INTO collection_switch_state
        (switch_ip, kind, last_run_id, last_collected_at)
        SELECT switch_ip, 'arp', COALESCE(MAX(run_id), 0), MAX(collected_at)
        FROM arp_snapshots GROUP BY switch_ip
        """
    )
    conn.execute(
        """
        INSERT INTO collection_switch_state
        (switch_ip, kind, last_run_id, last_collected_at)
        SELECT switch_ip, 'mac', COALESCE(MAX(run_id), 0), MAX(collected_at)
        FROM mac_snapshots GROUP BY switch_ip
        """
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return True


def _compact_database() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_discovery_settings() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovery_settings WHERE id=1").fetchone()
        return row_to_dict(row) or {"enabled": 0, "interval_minutes": 60}


def update_discovery_settings(data: dict[str, Any]) -> dict[str, Any]:
    enabled = 1 if data.get("enabled") in (True, 1, "1", "true") else 0
    try:
        interval = int(data.get("interval_minutes", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("自动发现间隔必须是整数分钟") from exc
    if not 5 <= interval <= 10080:
        raise ValueError("自动发现间隔需在 5 分钟到 7 天之间")
    now = now_text()
    with connect() as conn:
        conn.execute(
            """
            UPDATE discovery_settings
            SET enabled=?, interval_minutes=?, next_run_at=?
            WHERE id=1
            """,
            (enabled, interval, now if enabled else ""),
        )
        row = conn.execute("SELECT * FROM discovery_settings WHERE id=1").fetchone()
        conn.commit()
        return row_to_dict(row) or {}


def mark_discovery_started() -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE discovery_settings SET last_started_at=?, next_run_at='' WHERE id=1", (now_text(),)
        )
        conn.commit()


def mark_discovery_finished() -> None:
    with connect() as conn:
        settings = conn.execute("SELECT enabled, interval_minutes FROM discovery_settings WHERE id=1").fetchone()
        if not settings:
            return
        finished = datetime.now()
        next_run = ""
        if settings["enabled"]:
            from datetime import timedelta
            next_run = (finished + timedelta(minutes=settings["interval_minutes"])).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE discovery_settings SET last_finished_at=?, next_run_at=? WHERE id=1",
            (finished.strftime("%Y-%m-%d %H:%M:%S"), next_run),
        )
        conn.commit()


def _backfill_terminal_inventory(conn: sqlite3.Connection) -> None:
    """首次升级时从已有快照建立累计终端库，后续不覆盖已增量收录的数据。"""
    if conn.execute("SELECT 1 FROM terminal_inventory LIMIT 1").fetchone():
        return
    arp_rows = conn.execute(
        """
        SELECT a.* FROM arp_snapshots a
        JOIN (SELECT mac, MAX(id) AS id FROM arp_snapshots WHERE mac != '' GROUP BY mac) latest
          ON a.id = latest.id
        """
    ).fetchall()
    for row in arp_rows:
        first_seen = conn.execute(
            "SELECT MIN(collected_at) AS value FROM arp_snapshots WHERE mac=?", (row["mac"],)
        ).fetchone()["value"]
        conn.execute(
            """
            INSERT INTO terminal_inventory
            (mac, ip, first_seen, last_seen, last_arp_at, arp_switch_ip, arp_if_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["mac"], row["ip"], first_seen, row["collected_at"], row["collected_at"], row["switch_ip"], row["if_name"]),
        )
    mac_rows = conn.execute(
        """
        SELECT m.* FROM mac_snapshots m
        JOIN (SELECT mac, MAX(id) AS id FROM mac_snapshots WHERE mac != '' GROUP BY mac) latest
          ON m.id = latest.id
        """
    ).fetchall()
    for row in mac_rows:
        existing = conn.execute("SELECT first_seen, last_seen FROM terminal_inventory WHERE mac=?", (row["mac"],)).fetchone()
        first_seen = existing["first_seen"] if existing else row["collected_at"]
        last_seen = max(existing["last_seen"], row["collected_at"]) if existing else row["collected_at"]
        conn.execute(
            """
            INSERT INTO terminal_inventory (mac, first_seen, last_seen, last_mac_at, switch_ip, port_name, vlan)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
              last_seen=excluded.last_seen, last_mac_at=excluded.last_mac_at,
              switch_ip=excluded.switch_ip, port_name=excluded.port_name, vlan=excluded.vlan
            """,
            (row["mac"], first_seen, last_seen, row["collected_at"], row["switch_ip"], row["port_name"], row["vlan"]),
        )


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", (value or "").lower())


def _find_column(headers: list[str], aliases: list[str]) -> int | None:
    for alias in aliases:
        key = _normalize_key(alias)
        for index, header in enumerate(headers):
            if _normalize_key(header) == key:
                return index
    return None


def parse_ip_user_rows(raw_rows: Iterable[Iterable[str]]) -> tuple[list[dict[str, str]], list[str]]:
    rows = [list(map(str, row)) for row in raw_rows]
    if not rows:
        return [], []
    headers = rows[0]
    col_ip = _find_column(headers, IP_ALIASES)
    col_user = _find_column(headers, USER_ALIASES)
    col_dep = _find_column(headers, DEPARTMENT_ALIASES)
    col_loc = _find_column(headers, LOCATION_ALIASES)
    col_dev = _find_column(headers, DEVICE_ALIASES)
    col_note = _find_column(headers, NOTE_ALIASES)
    if col_ip is None:
        raise ValueError("导入文件缺少 IP 列（支持列名：IP、IP地址、地址）")

    parsed: list[dict[str, str]] = []
    errors: list[str] = []
    for row_number, row in enumerate(rows[1:], start=2):
        ip = row[col_ip].strip() if col_ip < len(row) else ""
        if not ip:
            continue
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            errors.append(f"第 {row_number} 行 IP 无效：{ip}")
            continue
        parsed.append(
            {
                "ip": ip,
                "user_name": row[col_user].strip() if col_user is not None and col_user < len(row) else "",
                "department": row[col_dep].strip() if col_dep is not None and col_dep < len(row) else "",
                "location": row[col_loc].strip() if col_loc is not None and col_loc < len(row) else "",
                "device_type": row[col_dev].strip() if col_dev is not None and col_dev < len(row) else "",
                "note": row[col_note].strip() if col_note is not None and col_note < len(row) else "",
            }
        )
    return parsed, errors


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "gbk", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_ip_user_file(filename: str, raw: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xlsx":
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                temp.write(raw)
                temp_path = temp.name
            raw_rows = read_workbook_rows(Path(temp_path))
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)
    elif suffix in {".csv", ".txt", ""}:
        text = _decode_bytes(raw)
        raw_rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("仅支持 .csv、.txt 或 .xlsx 文件")
    return import_ip_user_rows(parse_ip_user_rows(raw_rows)[0])


def import_ip_user_csv_text(text: str) -> dict[str, Any]:
    raw_rows = list(csv.reader(io.StringIO(text)))
    return import_ip_user_rows(parse_ip_user_rows(raw_rows)[0])


def parse_port_filter_rows(raw_rows: Iterable[Iterable[str]]) -> tuple[list[dict[str, str]], list[str]]:
    rows = [list(map(str, row)) for row in raw_rows]
    if not rows:
        return [], []
    headers = rows[0]
    col_switch = _find_column(headers, SWITCH_IP_ALIASES)
    col_port = _find_column(headers, PORT_ALIASES)
    col_reason = _find_column(headers, REASON_ALIASES)
    if col_switch is None or col_port is None:
        raise ValueError("导入文件需包含交换机IP和端口列")
    parsed: list[dict[str, str]] = []
    errors: list[str] = []
    for row_number, row in enumerate(rows[1:], start=2):
        switch_ip = row[col_switch].strip() if col_switch < len(row) else ""
        port_name = row[col_port].strip() if col_port < len(row) else ""
        if not switch_ip and not port_name:
            continue
        try:
            ipaddress.ip_address(switch_ip)
        except ValueError:
            errors.append(f"第 {row_number} 行交换机 IP 无效：{switch_ip or '空'}")
            continue
        if not port_name:
            errors.append(f"第 {row_number} 行端口不能为空")
            continue
        parsed.append(
            {
                "switch_ip": switch_ip,
                "port_name": port_name,
                "reason": row[col_reason].strip() if col_reason is not None and col_reason < len(row) else "",
            }
        )
    return parsed, errors


def import_port_filter_file(filename: str, raw: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xlsx":
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                temp.write(raw)
                temp_path = temp.name
            raw_rows = read_workbook_rows(Path(temp_path))
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)
    elif suffix in {".csv", ".txt", ""}:
        raw_rows = list(csv.reader(io.StringIO(_decode_bytes(raw))))
    else:
        raise ValueError("仅支持 .csv、.txt 或 .xlsx 文件")
    rows, errors = parse_port_filter_rows(raw_rows)
    result = import_port_filter_rows(rows)
    result["skipped"] += len(errors)
    result["errors"] = errors[:20]
    return result


def import_port_filter_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    imported = 0
    updated = 0
    skipped = 0
    known_switches = {switch["ip"] for switch in list_switches()}
    for row in rows:
        if row["switch_ip"] not in known_switches:
            skipped += 1
            continue
        existing = next(
            (item for item in list_port_filters(row["switch_ip"])
             if item["port_name"] == normalize_filter_port(row["port_name"])),
            None,
        )
        upsert_port_filter(row, existing["id"] if existing else None)
        if existing:
            updated += 1
        else:
            imported += 1
    return {"imported": imported, "updated": updated, "skipped": skipped}


def import_ip_user_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    imported = 0
    updated = 0
    skipped = 0
    for row in rows:
        ip = row.get("ip", "").strip()
        if not ip:
            skipped += 1
            continue
        existing = fetch_ip_user_by_ip(ip)
        if existing:
            data = {
                key: value
                for key, value in row.items()
                if key != "ip" and str(value).strip()
            }
            data["ip"] = ip
            upsert_ip_user(data, existing["id"])
            updated += 1
        else:
            upsert_ip_user(row)
            imported += 1
    return {"imported": imported, "updated": updated, "skipped": skipped}


def fetch_ip_user_by_ip(ip: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM ip_users WHERE ip = ?", (ip,)).fetchone()
        return row_to_dict(row)


def list_ip_users(q: str = "", limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        if q:
            like = f"%{q.strip()}%"
            rows = conn.execute(
                """
                SELECT * FROM ip_users
                WHERE ip LIKE ? OR user_name LIKE ? OR department LIKE ? OR location LIKE ?
                ORDER BY ip
                LIMIT ?
                """,
                (like, like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ip_users ORDER BY ip LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(row) for row in rows]


def upsert_ip_user(data: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    fields = {
        "ip": str(data.get("ip", "")).strip(),
        "user_name": str(data.get("user_name", "")).strip(),
        "department": str(data.get("department", "")).strip(),
        "location": str(data.get("location", "")).strip(),
        "device_type": str(data.get("device_type", "")).strip(),
        "note": str(data.get("note", "")).strip(),
    }
    if not fields["ip"]:
        raise ValueError("IP 不能为空")

    with connect() as conn:
        if user_id is not None:
            conn.execute(
                """
                UPDATE ip_users SET ip=?, user_name=?, department=?, location=?, device_type=?, note=?,
                updated_at=datetime('now','localtime') WHERE id=?
                """,
                (fields["ip"], fields["user_name"], fields["department"], fields["location"],
                 fields["device_type"], fields["note"], user_id),
            )
            row = conn.execute("SELECT * FROM ip_users WHERE id=?", (user_id,)).fetchone()
        else:
            existing = conn.execute("SELECT * FROM ip_users WHERE ip=?", (fields["ip"],)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE ip_users SET user_name=?, department=?, location=?, device_type=?, note=?,
                    updated_at=datetime('now','localtime') WHERE id=?
                    """,
                    (fields["user_name"], fields["department"], fields["location"],
                     fields["device_type"], fields["note"], existing["id"]),
                )
                row = conn.execute("SELECT * FROM ip_users WHERE id=?", (existing["id"],)).fetchone()
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO ip_users (ip, user_name, department, location, device_type, note)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (fields["ip"], fields["user_name"], fields["department"], fields["location"],
                     fields["device_type"], fields["note"]),
                )
                row = conn.execute("SELECT * FROM ip_users WHERE id=?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return row_to_dict(row) or fields


def delete_ip_user(user_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM ip_users WHERE id=?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def list_nav_links() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nav_links ORDER BY enabled DESC, sort_order, id"
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def upsert_nav_link(data: dict[str, Any], link_id: int | None = None) -> dict[str, Any]:
    fields = {
        "title": str(data.get("title", "")).strip(),
        "url": str(data.get("url", "")).strip(),
        "category": str(data.get("category", "常用")).strip() or "常用",
        "icon": str(data.get("icon", "")).strip(),
        "sort_order": int(data.get("sort_order") or 0),
        "enabled": 1 if data.get("enabled", True) in (True, 1, "1", "true") else 0,
    }
    if not fields["title"] or not fields["url"]:
        raise ValueError("名称和地址不能为空")
    with connect() as conn:
        if link_id is not None:
            conn.execute(
                """
                UPDATE nav_links SET title=?, url=?, category=?, icon=?, sort_order=?, enabled=?,
                updated_at=datetime('now','localtime') WHERE id=?
                """,
                (fields["title"], fields["url"], fields["category"], fields["icon"],
                 fields["sort_order"], fields["enabled"], link_id),
            )
            row = conn.execute("SELECT * FROM nav_links WHERE id=?", (link_id,)).fetchone()
        else:
            cursor = conn.execute(
                """
                INSERT INTO nav_links (title, url, category, icon, sort_order, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fields["title"], fields["url"], fields["category"], fields["icon"],
                 fields["sort_order"], fields["enabled"]),
            )
            row = conn.execute("SELECT * FROM nav_links WHERE id=?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return row_to_dict(row) or fields


def delete_nav_link(link_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM nav_links WHERE id=?", (link_id,))
        conn.commit()
        return cursor.rowcount > 0


def seed_defaults() -> None:
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM nav_links").fetchone()["c"]
        if count == 0:
            conn.executemany(
                """
                INSERT INTO nav_links (title, url, category, icon, sort_order, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                [
                    ("Zabbix 监控", "http://10.84.85.34:3005", "运维平台", "z", 1),
                    ("医院官网", "https://www.example.com", "常用系统", "h", 2),
                    ("OA 系统", "http://oa.example.com", "常用系统", "o", 3),
                ],
            )
            conn.commit()

        count = conn.execute("SELECT COUNT(*) AS c FROM remote_targets").fetchone()["c"]
        if count == 0:
            conn.executemany(
                """
                INSERT INTO remote_targets (name, host, protocol, default_ports, category, note, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("远程桌面 RDP", "", "RDP", "3389", "默认远程", "Windows 远程桌面", 1),
                    ("SSH", "", "SSH", "22", "默认远程", "Linux/网络设备 SSH 登录", 2),
                    ("Telnet", "", "Telnet", "23", "默认远程", "网络设备 Telnet 登录", 3),
                    ("VNC", "", "VNC", "5900", "默认远程", "图形化远程控制", 4),
                    ("Web 管理", "", "Web", "80,443", "默认远程", "设备/平台 Web 管理页", 5),
                ],
            )
            conn.commit()


def sync_switches_from_workbook(workbook: Path = DEFAULT_WORKBOOK) -> dict[str, Any]:
    rows = parse_switch_rows(workbook)
    added = 0
    updated = 0
    with connect() as conn:
        for row in rows:
            existing = conn.execute("SELECT * FROM switches WHERE ip=?", (row.ip,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE switches SET name=?, uplink_ip=?, uplink_port=?, snmp_community=?, level=?,
                    updated_at=datetime('now','localtime') WHERE id=?
                    """,
                    (row.name, row.uplink_ip, row.uplink_port, row.snmp_community, row.level, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO switches (name, ip, uplink_ip, uplink_port, snmp_community, level)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (row.name, row.ip, row.uplink_ip, row.uplink_port, row.snmp_community, row.level),
                )
                added += 1
        conn.commit()
    return {"total": len(rows), "added": added, "updated": updated}


def normalize_filter_port(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def sync_auto_port_filters() -> dict[str, Any]:
    added = 0
    switches = list_switches()
    switch_by_ip = {row["ip"]: row for row in switches}
    with connect() as conn:
        for row in switches:
            uplink_ip = (row.get("uplink_ip") or "").strip()
            uplink_port = normalize_filter_port(row.get("uplink_port") or "")
            if not uplink_port:
                continue
            existing = conn.execute(
                "SELECT id FROM switch_port_filters WHERE switch_ip=? AND port_name=?",
                (row["ip"], uplink_port),
            ).fetchone()
            if existing:
                continue
            reason = "上联口（自动）"
            if uplink_ip in switch_by_ip:
                reason = "连接可管理交换机（自动）"
            conn.execute(
                """
                INSERT OR IGNORE INTO switch_port_filters
                (switch_ip, port_name, reason, auto_generated)
                VALUES (?, ?, ?, 1)
                """,
                (row["ip"], uplink_port, reason),
            )
            added += conn.total_changes > 0
        conn.commit()
    return {"added": added, "total": len(switches)}


def list_port_filters(switch_ip: str = "") -> list[dict[str, Any]]:
    with connect() as conn:
        if switch_ip:
            rows = conn.execute(
                """
                SELECT f.*, sw.name AS switch_name
                FROM switch_port_filters f LEFT JOIN switches sw ON f.switch_ip = sw.ip
                WHERE f.switch_ip = ?
                ORDER BY f.switch_ip, f.port_name
                """,
                (switch_ip,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT f.*, sw.name AS switch_name
                FROM switch_port_filters f LEFT JOIN switches sw ON f.switch_ip = sw.ip
                ORDER BY f.switch_ip, f.port_name
                """
            ).fetchall()
        return [row_to_dict(row) for row in rows]


def list_port_filters_grouped() -> list[dict[str, Any]]:
    filters = list_port_filters()
    switches = list_switches()
    groups: dict[str, dict[str, Any]] = {}
    for switch in switches:
        groups[switch["ip"]] = {
            "switch_ip": switch["ip"],
            "switch_name": switch["name"],
            "uplink_port": switch.get("uplink_port", ""),
            "port_filters": [],
        }
    for item in filters:
        group = groups.setdefault(
            item["switch_ip"],
            {
                "switch_ip": item["switch_ip"],
                "switch_name": item.get("switch_name") or item["switch_ip"],
                "uplink_port": "",
                "port_filters": [],
            },
        )
        group["port_filters"].append(item)
    return list(groups.values())


def upsert_port_filter(data: dict[str, Any], filter_id: int | None = None) -> dict[str, Any]:
    switch_ip = str(data.get("switch_ip", "")).strip()
    port_name = normalize_filter_port(data.get("port_name") or "")
    reason = str(data.get("reason", "")).strip()
    if not switch_ip or not port_name:
        raise ValueError("交换机 IP 和端口不能为空")
    with connect() as conn:
        if filter_id is not None:
            conn.execute(
                """
                UPDATE switch_port_filters
                SET switch_ip=?, port_name=?, reason=?, auto_generated=0,
                updated_at=datetime('now','localtime')
                WHERE id=?
                """,
                (switch_ip, port_name, reason, filter_id),
            )
            row = conn.execute("SELECT * FROM switch_port_filters WHERE id=?", (filter_id,)).fetchone()
        else:
            existing = conn.execute(
                "SELECT * FROM switch_port_filters WHERE switch_ip=? AND port_name=?",
                (switch_ip, port_name),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE switch_port_filters
                    SET reason=?, auto_generated=0, updated_at=datetime('now','localtime')
                    WHERE id=?
                    """,
                    (reason, existing["id"]),
                )
                row = conn.execute("SELECT * FROM switch_port_filters WHERE id=?", (existing["id"],)).fetchone()
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO switch_port_filters (switch_ip, port_name, reason, auto_generated)
                    VALUES (?, ?, ?, 0)
                    """,
                    (switch_ip, port_name, reason),
                )
                row = conn.execute("SELECT * FROM switch_port_filters WHERE id=?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return row_to_dict(row) or {"switch_ip": switch_ip, "port_name": port_name, "reason": reason}


def delete_port_filter(filter_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM switch_port_filters WHERE id=?", (filter_id,))
        conn.commit()
        return cursor.rowcount > 0


def filtered_port_map() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in list_port_filters():
        result.setdefault(row["switch_ip"], set()).add(row["port_name"])
    return result


def list_switches() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM switches ORDER BY level, name").fetchall()
        return [row_to_dict(row) for row in rows]


def update_switch(switch_id: int, data: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "snmp_community": str(data.get("snmp_community", "")).strip() or "public",
        "role": str(data.get("role", "both")).strip() or "both",
        "enabled": 1 if data.get("enabled", True) in (True, 1, "1", "true") else 0,
    }
    if fields["role"] not in {"core", "access", "both"}:
        raise ValueError("角色只能是核心、接入或全部")
    with connect() as conn:
        conn.execute(
            """
            UPDATE switches SET snmp_community=?, role=?, enabled=?, updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (fields["snmp_community"], fields["role"], fields["enabled"], switch_id),
        )
        row = conn.execute("SELECT * FROM switches WHERE id=?", (switch_id,)).fetchone()
        conn.commit()
        result = row_to_dict(row)
        if result is None:
            raise ValueError("交换机不存在")
        return result


def delete_switch(switch_id: int) -> bool:
    """删除采集对象及其端口排除规则，历史快照保留供历史查询。"""
    with connect() as conn:
        switch = conn.execute("SELECT ip FROM switches WHERE id=?", (switch_id,)).fetchone()
        if not switch:
            return False
        conn.execute("DELETE FROM switch_port_filters WHERE switch_ip=?", (switch["ip"],))
        cursor = conn.execute("DELETE FROM switches WHERE id=?", (switch_id,))
        conn.commit()
        return cursor.rowcount > 0


def get_switch_name_map() -> dict[str, str]:
    return {row["ip"]: row["name"] for row in list_switches()}


def list_remote_targets() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM remote_targets ORDER BY enabled DESC, sort_order, id"
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def upsert_remote_target(data: dict[str, Any], target_id: int | None = None) -> dict[str, Any]:
    fields = {
        "name": str(data.get("name", "")).strip(),
        "host": str(data.get("host", "")).strip(),
        "protocol": str(data.get("protocol", "RDP")).strip() or "RDP",
        "default_ports": str(data.get("default_ports", "3389")).strip() or "3389",
        "category": str(data.get("category", "")).strip(),
        "note": str(data.get("note", "")).strip(),
        "sort_order": int(data.get("sort_order") or 0),
        "enabled": 1 if data.get("enabled", True) in (True, 1, "1", "true") else 0,
    }
    if not fields["name"]:
        raise ValueError("名称不能为空")
    with connect() as conn:
        if target_id is not None:
            conn.execute(
                """
                UPDATE remote_targets SET name=?, host=?, protocol=?, default_ports=?, category=?,
                note=?, sort_order=?, enabled=?, updated_at=datetime('now','localtime') WHERE id=?
                """,
                (fields["name"], fields["host"], fields["protocol"], fields["default_ports"],
                 fields["category"], fields["note"], fields["sort_order"], fields["enabled"], target_id),
            )
            row = conn.execute("SELECT * FROM remote_targets WHERE id=?", (target_id,)).fetchone()
        else:
            cursor = conn.execute(
                """
                INSERT INTO remote_targets (name, host, protocol, default_ports, category, note, sort_order, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fields["name"], fields["host"], fields["protocol"], fields["default_ports"],
                 fields["category"], fields["note"], fields["sort_order"], fields["enabled"]),
            )
            row = conn.execute("SELECT * FROM remote_targets WHERE id=?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return row_to_dict(row) or fields


def delete_remote_target(target_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM remote_targets WHERE id=?", (target_id,))
        conn.commit()
        return cursor.rowcount > 0


def save_arp_snapshots(
    switch_ip: str, rows: list[dict[str, Any]], collected_at: str, run_id: int | None = None
) -> int:
    incoming: dict[str, dict[str, Any]] = {}
    for row in rows:
        ip = str(row.get("ip", "")).strip()
        if not ip:
            continue
        incoming[ip] = {
            "ip": ip,
            "mac": str(row.get("mac", "") or ""),
            "if_index": row.get("if_index"),
            "if_name": str(row.get("if_name", "") or ""),
        }

    with connect() as conn:
        existing = {
            row["ip"]: row
            for row in conn.execute(
                """
                SELECT ip, mac, if_index, if_name
                FROM arp_snapshots WHERE switch_ip=?
                """,
                (switch_ip,),
            ).fetchall()
        }
        inserts: list[tuple[Any, ...]] = []
        updates: list[tuple[Any, ...]] = []
        changes: list[tuple[Any, ...]] = []
        for ip, row in incoming.items():
            current = existing.pop(ip, None)
            if current is None:
                inserts.append(
                    (
                        switch_ip,
                        ip,
                        row["mac"],
                        row["if_index"],
                        row["if_name"],
                        collected_at,
                        run_id,
                    )
                )
                changes.append(
                    (switch_ip, ip, row["mac"], row["if_index"], row["if_name"], collected_at, run_id, "first")
                )
                continue
            if (
                row["mac"] != (current["mac"] or "")
                or row["if_index"] != current["if_index"]
                or row["if_name"] != (current["if_name"] or "")
            ):
                updates.append(
                    (
                        row["mac"],
                        row["if_index"],
                        row["if_name"],
                        collected_at,
                        run_id,
                        switch_ip,
                        ip,
                    )
                )
                changes.append(
                    (switch_ip, ip, row["mac"], row["if_index"], row["if_name"], collected_at, run_id, "changed")
                )

        removed = list(existing)
        if inserts:
            conn.executemany(
                """
                INSERT INTO arp_snapshots
                (switch_ip, ip, mac, if_index, if_name, collected_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                inserts,
            )
        if updates:
            conn.executemany(
                """
                UPDATE arp_snapshots
                SET mac=?, if_index=?, if_name=?, collected_at=?, run_id=?
                WHERE switch_ip=? AND ip=?
                """,
                updates,
            )
        if removed:
            conn.executemany(
                "DELETE FROM arp_snapshots WHERE switch_ip=? AND ip=?",
                [(switch_ip, ip) for ip in removed],
            )
            changes.extend(
                (switch_ip, ip, "", None, "", collected_at, run_id, "removed")
                for ip in removed
            )
        if changes:
            conn.executemany(
                """
                INSERT INTO arp_changes
                (switch_ip, ip, mac, if_index, if_name, collected_at, run_id, change_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                changes,
            )
        conn.execute(
            """
            INSERT INTO collection_switch_state
            (switch_ip, kind, last_run_id, last_collected_at)
            VALUES (?, 'arp', ?, ?)
            ON CONFLICT(switch_ip, kind) DO UPDATE SET
              last_run_id=excluded.last_run_id,
              last_collected_at=excluded.last_collected_at
            """,
            (switch_ip, run_id or 0, collected_at),
        )
        conn.executemany(
            """
            INSERT INTO terminal_inventory
            (mac, ip, first_seen, last_seen, last_arp_at, arp_switch_ip, arp_if_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
              ip=excluded.ip, last_seen=excluded.last_seen, last_arp_at=excluded.last_arp_at,
              arp_switch_ip=excluded.arp_switch_ip, arp_if_name=excluded.arp_if_name
            """,
            [
                (
                    row["mac"], row["ip"], collected_at, collected_at, collected_at,
                    switch_ip, row.get("if_name", ""),
                )
                for row in rows
                if row.get("mac")
            ],
        )
        conn.commit()
        return len(inserts) + len(updates) + len(removed)


def save_mac_snapshots(
    switch_ip: str, rows: list[dict[str, Any]], collected_at: str, run_id: int | None = None
) -> int:
    incoming: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        mac = str(row.get("mac", "")).strip()
        if not mac:
            continue
        vlan = int(row.get("vlan") or 0)
        incoming[(mac, vlan)] = {
            "mac": mac,
            "vlan": vlan,
            "if_index": row.get("if_index"),
            "port_name": str(row.get("port_name", "") or ""),
            "entry_type": str(row.get("entry_type", "") or ""),
        }

    with connect() as conn:
        existing = {
            (row["mac"], int(row["vlan"] or 0)): row
            for row in conn.execute(
                """
                SELECT mac, vlan, if_index, port_name, entry_type
                FROM mac_snapshots WHERE switch_ip=?
                """,
                (switch_ip,),
            ).fetchall()
        }
        inserts: list[tuple[Any, ...]] = []
        updates: list[tuple[Any, ...]] = []
        changes: list[tuple[Any, ...]] = []
        for key, row in incoming.items():
            current = existing.pop(key, None)
            mac, vlan = key
            if current is None:
                inserts.append(
                    (
                        switch_ip,
                        mac,
                        vlan,
                        row["if_index"],
                        row["port_name"],
                        row["entry_type"],
                        collected_at,
                        run_id,
                    )
                )
                changes.append(
                    (
                        switch_ip,
                        mac,
                        vlan,
                        row["if_index"],
                        row["port_name"],
                        row["entry_type"],
                        collected_at,
                        run_id,
                        "first",
                    )
                )
                continue
            if (
                row["if_index"] != current["if_index"]
                or row["port_name"] != (current["port_name"] or "")
                or row["entry_type"] != (current["entry_type"] or "")
            ):
                updates.append(
                    (
                        row["if_index"],
                        row["port_name"],
                        row["entry_type"],
                        collected_at,
                        run_id,
                        switch_ip,
                        mac,
                        vlan,
                    )
                )
                changes.append(
                    (
                        switch_ip,
                        mac,
                        vlan,
                        row["if_index"],
                        row["port_name"],
                        row["entry_type"],
                        collected_at,
                        run_id,
                        "changed",
                    )
                )

        removed = list(existing)
        if inserts:
            conn.executemany(
                """
                INSERT INTO mac_snapshots
                (switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                inserts,
            )
        if updates:
            conn.executemany(
                """
                UPDATE mac_snapshots
                SET if_index=?, port_name=?, entry_type=?, collected_at=?, run_id=?
                WHERE switch_ip=? AND mac=? AND vlan=?
                """,
                updates,
            )
        if removed:
            conn.executemany(
                "DELETE FROM mac_snapshots WHERE switch_ip=? AND mac=? AND vlan=?",
                [(switch_ip, mac, vlan) for mac, vlan in removed],
            )
            changes.extend(
                (switch_ip, mac, vlan, None, "", "", collected_at, run_id, "removed")
                for mac, vlan in removed
            )
        if changes:
            conn.executemany(
                """
                INSERT INTO mac_changes
                (switch_ip, mac, vlan, if_index, port_name, entry_type, collected_at, run_id, change_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                changes,
            )
        conn.execute(
            """
            INSERT INTO collection_switch_state
            (switch_ip, kind, last_run_id, last_collected_at)
            VALUES (?, 'mac', ?, ?)
            ON CONFLICT(switch_ip, kind) DO UPDATE SET
              last_run_id=excluded.last_run_id,
              last_collected_at=excluded.last_collected_at
            """,
            (switch_ip, run_id or 0, collected_at),
        )
        conn.executemany(
            """
            INSERT INTO terminal_inventory (mac, first_seen, last_seen, last_mac_at, switch_ip, port_name, vlan)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
              last_seen=excluded.last_seen, last_mac_at=excluded.last_mac_at,
              switch_ip=excluded.switch_ip, port_name=excluded.port_name, vlan=excluded.vlan
            """,
            [
                (
                    row["mac"], collected_at, collected_at, collected_at, switch_ip,
                    row.get("port_name", ""), row.get("vlan"),
                )
                for row in rows
                if row.get("mac")
            ],
        )
        conn.commit()
        return len(inserts) + len(updates) + len(removed)


def prune_snapshots() -> None:
    days = f"-{SNAPSHOT_RETENTION_DAYS} days"
    with connect() as conn:
        conn.execute(
            "DELETE FROM arp_changes WHERE collected_at < datetime('now', 'localtime', ?)", (days,)
        )
        conn.execute(
            "DELETE FROM mac_changes WHERE collected_at < datetime('now', 'localtime', ?)", (days,)
        )
        conn.commit()


def start_collection_run(kind: str) -> int:
    with connect() as conn:
        # 页面仅展示本次采集摘要，过往运行详情进入日志文件。
        conn.execute("DELETE FROM collection_runs")
        cursor = conn.execute(
            "INSERT INTO collection_runs (kind, started_at, status) VALUES (?, ?, 'running')",
            (kind, now_text()),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_collection_run(
    run_id: int,
    switch_count: int,
    success_count: int,
    failed: list[str],
    record_count: int,
    change_count: int,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE collection_runs SET finished_at=?, switch_count=?, success_count=?, failed=?,
            record_count=?, change_count=?, status=? WHERE id=?
            """,
            (
                now_text(),
                switch_count,
                success_count,
                "\n".join(failed),
                record_count,
                change_count,
                "ok" if not failed else "partial",
                run_id,
            ),
        )
        conn.commit()


def list_collection_runs(kind: str = "", limit: int = 30) -> list[dict[str, Any]]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM collection_runs WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [row_to_dict(row) for row in rows]


def latest_arp_by_ip() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        latest_run = conn.execute(
            "SELECT MAX(last_run_id) AS r FROM collection_switch_state WHERE kind='arp'"
        ).fetchone()["r"]
        rows = conn.execute(
            """
            SELECT arp.* FROM arp_snapshots arp
            JOIN collection_switch_state state
              ON state.switch_ip = arp.switch_ip AND state.kind = 'arp'
            JOIN (
              SELECT ip, MAX(arp.id) AS max_id
              FROM arp_snapshots arp
              JOIN collection_switch_state state
                ON state.switch_ip = arp.switch_ip AND state.kind = 'arp'
              WHERE state.last_run_id = ?
              GROUP BY ip
            ) latest ON arp.id = latest.max_id
            WHERE arp.mac != ''
            """,
            (latest_run or 0,),
        ).fetchall()
        return {row["ip"]: row_to_dict(row) for row in rows}


def latest_mac_rows() -> list[dict[str, Any]]:
    with connect() as conn:
        latest_run = conn.execute(
            "SELECT MAX(last_run_id) AS r FROM collection_switch_state WHERE kind='mac'"
        ).fetchone()["r"]
        rows = conn.execute(
            """
            SELECT mac.* FROM mac_snapshots mac
            JOIN collection_switch_state state
              ON state.switch_ip = mac.switch_ip AND state.kind = 'mac'
            WHERE state.last_run_id = ? AND mac.mac != ''
            """,
            (latest_run or 0,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def arp_history(ip: str, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT change.*, sw.name AS switch_name
            FROM arp_changes change LEFT JOIN switches sw ON change.switch_ip = sw.ip
            WHERE change.ip = ? ORDER BY change.id DESC LIMIT ?
            """,
            (ip, limit),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def mac_history(mac: str, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT change.*, sw.name AS switch_name
            FROM mac_changes change LEFT JOIN switches sw ON change.switch_ip = sw.ip
            WHERE change.mac = ? ORDER BY change.id DESC LIMIT ?
            """,
            (mac, limit),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def build_inventory_locate_items(q: str = "", filter_by: str = "all", limit: int = 500) -> dict[str, Any]:
    """返回累计终端库；终端不在本次快照内也不会从列表消失。"""
    with connect() as conn:
        inventory = [row_to_dict(row) for row in conn.execute(
            "SELECT * FROM terminal_inventory ORDER BY last_seen DESC, mac"
        ).fetchall()]
    users = {user["ip"]: user for user in list_ip_users()}
    switch_names = get_switch_name_map()
    current_arp = latest_arp_by_ip()
    online_macs = {row.get("mac") for row in current_arp.values() if row.get("mac")}
    query = q.strip().lower()
    items: list[dict[str, Any]] = []

    for terminal in inventory:
        ip = terminal.get("ip") or ""
        user = users.pop(ip, None) if ip else None
        item = {
            "ip": ip,
            "mac": terminal["mac"],
            "matched": terminal["mac"] in online_macs,
            "user_id": (user or {}).get("id"),
            "user_name": (user or {}).get("user_name", ""),
            "department": (user or {}).get("department", ""),
            "location": (user or {}).get("location", ""),
            "device_type": (user or {}).get("device_type", ""),
            "note": (user or {}).get("note", ""),
            "arpSwitchIp": terminal.get("arp_switch_ip", ""),
            "arpSwitchName": switch_names.get(terminal.get("arp_switch_ip", ""), ""),
            "arpCollectedAt": terminal.get("last_arp_at", ""),
            "firstSeen": terminal.get("first_seen", ""),
            "lastSeen": terminal.get("last_seen", ""),
            "macMatches": [{
                "switch_ip": terminal.get("switch_ip", ""),
                "switch_name": switch_names.get(terminal.get("switch_ip", ""), terminal.get("switch_ip", "")),
                "port_name": terminal.get("port_name", ""),
                "vlan": terminal.get("vlan"),
                "collected_at": terminal.get("last_mac_at", ""),
            }] if terminal.get("switch_ip") else [],
            "portName": terminal.get("port_name", ""),
            "switchIp": terminal.get("switch_ip", ""),
            "switchName": switch_names.get(terminal.get("switch_ip", ""), ""),
            "vlan": terminal.get("vlan"),
        }
        if query and query not in " ".join(str(value).lower() for value in item.values() if not isinstance(value, (dict, list))):
            continue
        if filter_by == "online" and not item["matched"]:
            continue
        if filter_by == "registered" and not item["user_name"]:
            continue
        if filter_by == "unregistered" and item["user_name"]:
            continue
        items.append(item)

    # 手工台账中暂未采集到 MAC 的 IP 也要保留。
    for ip, user in users.items():
        if filter_by == "online" or (filter_by == "unregistered" and user.get("user_name")):
            continue
        item = {
            "ip": ip, "mac": "", "matched": False, "user_id": user["id"],
            "user_name": user.get("user_name", ""), "department": user.get("department", ""),
            "location": user.get("location", ""), "device_type": user.get("device_type", ""),
            "note": user.get("note", ""), "arpSwitchIp": "", "arpSwitchName": "",
            "arpCollectedAt": "", "firstSeen": "", "lastSeen": "", "macMatches": [],
            "portName": "", "switchIp": "", "switchName": "", "vlan": None,
        }
        if not query or query in " ".join(str(value).lower() for value in item.values() if not isinstance(value, (dict, list))):
            items.append(item)

    items.sort(key=lambda item: (item.get("lastSeen") or "", item.get("ip") or "", item["mac"]), reverse=True)
    return {"items": items[:limit], "total": len(items), "mode": "inventory"}


def build_locate_items(
    q: str = "",
    filter_by: str = "all",
    limit: int = 500,
    mode: str = "inventory",
) -> dict[str, Any]:
    if mode == "inventory":
        return build_inventory_locate_items(q=q, filter_by=filter_by, limit=limit)
    if mode != "current":
        raise ValueError("定位模式只能是 inventory 或 current")
    arp_map = latest_arp_by_ip()
    mac_rows = latest_mac_rows()
    users = {user["ip"]: user for user in list_ip_users()}
    switch_names = get_switch_name_map()
    switch_roles = {row["ip"]: row["role"] for row in list_switches()}
    switch_uplinks = {
        row["ip"]: (row.get("uplink_port") or "", row.get("uplink_ip") or "")
        for row in list_switches()
    }
    keys = set(arp_map) | set(users)
    query = q.strip().lower()
    items: list[dict[str, Any]] = []

    for ip in keys:
        arp = arp_map.get(ip)
        user = users.get(ip)
        mac = (arp or {}).get("mac") or ""
        matches = [row for row in mac_rows if row["mac"] == mac] if mac else []
        if matches:
            non_core = [row for row in matches if switch_roles.get(row["switch_ip"]) != "core"]
            primary_matches = non_core or matches

            def match_rank(row: dict[str, Any]) -> tuple[int, int]:
                role = switch_roles.get(row["switch_ip"], "both")
                uplink_port, uplink_ip = switch_uplinks.get(row["switch_ip"], ("", ""))
                is_root = not uplink_ip
                same_uplink = bool(uplink_port) and (row.get("port_name") or "") == uplink_port
                return (
                    0 if role == "core" or is_root else 1,
                    0 if same_uplink else 1,
                )

            primary = max(
                primary_matches,
                key=lambda row: (match_rank(row), row.get("collected_at") or ""),
            )
        else:
            primary = None

        item = {
            "ip": ip,
            "mac": mac,
            "matched": bool(arp),
            "user_id": (user or {}).get("id"),
            "user_name": (user or {}).get("user_name", ""),
            "department": (user or {}).get("department", ""),
            "location": (user or {}).get("location", ""),
            "device_type": (user or {}).get("device_type", ""),
            "note": (user or {}).get("note", ""),
            "arpSwitchIp": (arp or {}).get("switch_ip", ""),
            "arpSwitchName": switch_names.get((arp or {}).get("switch_ip", ""), ""),
            "arpCollectedAt": (arp or {}).get("collected_at", ""),
            "macMatches": [
                {
                    "switch_ip": row["switch_ip"],
                    "switch_name": switch_names.get(row["switch_ip"], row["switch_ip"]),
                    "port_name": row.get("port_name", ""),
                    "vlan": row.get("vlan"),
                    "collected_at": row.get("collected_at", ""),
                }
                for row in matches
            ],
            "portName": (primary or {}).get("port_name", ""),
            "switchIp": (primary or {}).get("switch_ip", ""),
            "switchName": switch_names.get((primary or {}).get("switch_ip", ""), ""),
            "vlan": (primary or {}).get("vlan"),
        }

        if query:
            haystack = " ".join(
                str(value).lower()
                for value in [
                    ip,
                    mac,
                    item["user_name"],
                    item["department"],
                    item["location"],
                    item["switchName"],
                    item["portName"],
                ]
            )
            if query not in haystack:
                continue

        if filter_by == "online" and not item["matched"]:
            continue
        if filter_by == "registered" and not item["user_name"]:
            continue
        if filter_by == "unregistered" and item["user_name"]:
            continue

        items.append(item)

    items.sort(
        key=lambda item: (
            tuple(int(part) for part in item["ip"].split("."))
            if re.fullmatch(r"\d+(?:\.\d+){3}", item["ip"])
            else (999, 0, 0, 0)
        )
    )
    return {"items": items[:limit], "total": len(items), "mode": "current"}


def locate_stats() -> dict[str, Any]:
    with connect() as conn:
        arp_count = conn.execute("SELECT COUNT(DISTINCT ip) AS c FROM arp_snapshots").fetchone()["c"]
        mac_count = conn.execute("SELECT COUNT(DISTINCT mac) AS c FROM mac_snapshots").fetchone()["c"]
        user_count = conn.execute("SELECT COUNT(*) AS c FROM ip_users").fetchone()["c"]
        inventory_count = conn.execute("SELECT COUNT(*) AS c FROM terminal_inventory").fetchone()["c"]
        last_run = conn.execute(
            """
            SELECT started_at FROM collection_runs WHERE status != 'running'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    arp_map = latest_arp_by_ip()
    online = len(arp_map)
    registered = 0
    unregistered = 0
    for ip, arp in arp_map.items():
        if fetch_ip_user_by_ip(ip):
            registered += 1
        else:
            unregistered += 1
    return {
        "totalHosts": arp_count,
        "inventoryCount": inventory_count,
        "online": online,
        "macCount": mac_count,
        "registered": registered,
        "unregistered": unregistered,
        "userCount": user_count,
        "lastCollection": last_run["started_at"] if last_run else "",
    }


def collect_status() -> dict[str, Any]:
    runs = list_collection_runs(limit=3)
    running = any(run["status"] == "running" for run in runs)
    stats = locate_stats()
    discovery = get_discovery_settings()
    return {
        "running": running,
        "schedule": f"每 {discovery['interval_minutes']} 分钟" if discovery.get("enabled") else "已关闭",
        "discovery": discovery,
        "lastRuns": runs,
        "stats": stats,
    }


def snapshot_stats() -> dict[str, Any]:
    with connect() as conn:
        arp_rows = conn.execute("SELECT COUNT(*) AS c FROM arp_snapshots").fetchone()["c"]
        mac_rows = conn.execute("SELECT COUNT(*) AS c FROM mac_snapshots").fetchone()["c"]
        arp_changes = conn.execute("SELECT COUNT(*) AS c FROM arp_changes").fetchone()["c"]
        mac_changes = conn.execute("SELECT COUNT(*) AS c FROM mac_changes").fetchone()["c"]
        return {
            "arpRows": arp_rows,
            "macRows": mac_rows,
            "arpChanges": arp_changes,
            "macChanges": mac_changes,
        }
