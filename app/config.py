from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKBOOK = BASE_DIR / "交换机连线信息xlsx.xlsx"
DB_PATH = BASE_DIR / "network_data.db"
LOG_DIR = BASE_DIR / "logs"
COLLECTION_LOG_PATH = LOG_DIR / "collection.log"

DEFAULT_ZABBIX_URL = "http://10.84.85.34:3005"
ZABBIX_TIMEOUT_SECONDS = 15

DEFAULT_COLLECT_TIME = os.getenv("COLLECT_TIME", "02:30")
SNMP_TIMEOUT = int(os.getenv("SNMP_TIMEOUT", "3"))
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "0"))
SNMP_PORT = int(os.getenv("SNMP_PORT", "161"))
SNMP_BACKEND = os.getenv("SNMP_BACKEND", "auto")

PORT_TEST_TIMEOUT = float(os.getenv("PORT_TEST_TIMEOUT", "1.5"))
PORT_TEST_MAX_PORTS = int(os.getenv("PORT_TEST_MAX_PORTS", "200"))
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "90"))
