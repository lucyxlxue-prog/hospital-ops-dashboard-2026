#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hospital supply chain multi-user reporting dashboard.

Run:
  python server.py --host 0.0.0.0 --port 8910
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import uuid
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = APP_DIR / "public"
DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", APP_DIR / "data" / "hospital_supply_chain.db")).resolve()
DATA_DIR = DB_PATH.parent
CURRENT_YEAR = datetime.now().year


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contracts (
              id TEXT PRIMARY KEY,
              hospital_name TEXT NOT NULL,
              project_name TEXT NOT NULL,
              contract_amount REAL NOT NULL DEFAULT 0,
              key_terms TEXT NOT NULL DEFAULT '',
              categories TEXT NOT NULL DEFAULT '[]',
              owner TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(hospital_name, project_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_revenue (
              id TEXT PRIMARY KEY,
              contract_id TEXT NOT NULL,
              week TEXT NOT NULL,
              amount REAL NOT NULL DEFAULT 0,
              submitted_by TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(contract_id, week),
              FOREIGN KEY(contract_id) REFERENCES contracts(id)
            )
            """
        )
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        if count == 0:
            seed_data(conn)


def seed_data(conn: sqlite3.Connection) -> None:
    created = now_iso()
    contracts = [
        {
            "id": "c_beijing_xiehe",
            "hospital_name": "北京协和医院",
            "project_name": "高值耗材 SPD 运营项目",
            "contract_amount": 6800,
            "key_terms": "独家配送；回款周期 60 天；年度保底量 1800 万；库存周转不低于 8 次",
            "categories": ["高值耗材", "低值耗材", "设备维保"],
            "owner": "华北运营组",
        },
        {
            "id": "c_shanghai_ruijin",
            "hospital_name": "上海瑞金医院",
            "project_name": "耗材与试剂联合供应项目",
            "contract_amount": 5200,
            "key_terms": "SPD 托管仓；回款周期 45 天；试剂冷链全程追踪；月度对账",
            "categories": ["高值耗材", "低值耗材", "试剂"],
            "owner": "华东运营组",
        },
        {
            "id": "c_guangzhou_zhongshan",
            "hospital_name": "广州中山一院",
            "project_name": "药械联动供应链项目",
            "contract_amount": 4700,
            "key_terms": "药品院内配送；回款周期 75 天；临采响应 24 小时；年度服务费封顶",
            "categories": ["药品", "高值耗材", "低值耗材"],
            "owner": "华南运营组",
        },
    ]
    revenue_rows = [
        ("c_beijing_xiehe", "2026-W21", 118),
        ("c_beijing_xiehe", "2026-W22", 126),
        ("c_beijing_xiehe", "2026-W23", 132),
        ("c_beijing_xiehe", "2026-W24", 141),
        ("c_beijing_xiehe", "2026-W25", 147),
        ("c_shanghai_ruijin", "2026-W21", 96),
        ("c_shanghai_ruijin", "2026-W22", 104),
        ("c_shanghai_ruijin", "2026-W23", 116),
        ("c_shanghai_ruijin", "2026-W24", 122),
        ("c_shanghai_ruijin", "2026-W25", 129),
        ("c_guangzhou_zhongshan", "2026-W21", 82),
        ("c_guangzhou_zhongshan", "2026-W22", 91),
        ("c_guangzhou_zhongshan", "2026-W23", 94),
        ("c_guangzhou_zhongshan", "2026-W24", 98),
        ("c_guangzhou_zhongshan", "2026-W25", 101),
    ]

    for item in contracts:
        conn.execute(
            """
            INSERT INTO contracts
            (id, hospital_name, project_name, contract_amount, key_terms, categories, owner, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["hospital_name"],
                item["project_name"],
                item["contract_amount"],
                item["key_terms"],
                json.dumps(item["categories"], ensure_ascii=False),
                item["owner"],
                "seed",
                created,
                created,
            ),
        )

    for contract_id, week, amount in revenue_rows:
        conn.execute(
            """
            INSERT INTO weekly_revenue
            (id, contract_id, week, amount, submitted_by, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (make_id("r"), contract_id, week, amount, "示例数据", "", created, created),
        )
    conn.commit()


def row_to_contract(row: sqlite3.Row) -> dict:
    categories = []
    try:
        categories = json.loads(row["categories"] or "[]")
    except json.JSONDecodeError:
        categories = split_categories(row["categories"])
    return {
        "id": row["id"],
        "hospitalName": row["hospital_name"],
        "projectName": row["project_name"],
        "contractAmount": row["contract_amount"],
        "keyTerms": row["key_terms"],
        "categories": categories,
        "owner": row["owner"],
        "source": row["source"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_to_revenue(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "contractId": row["contract_id"],
        "week": row["week"],
        "amount": row["amount"],
        "submittedBy": row["submitted_by"],
        "note": row["note"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def parse_json_body(handler: SimpleHTTPRequestHandler) -> object:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def parse_amount_to_wan(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").strip()
    if not text:
        return 0.0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿元|亿|万元|万|元)?", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2) or "万元"
    if unit in ("亿元", "亿"):
        return number * 10000
    if unit == "元":
        return number / 10000
    return number


def split_categories(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [
        item.strip()
        for item in re.split(r"[、,，;；\s]+", str(value or ""))
        if item.strip()
    ]


def pick_value(text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n\r]+)", text)
        if match:
            return match.group(1).strip()
    return ""


def find_sentence(text: str, keywords: list[str]) -> str:
    parts = [part.strip() for part in re.split(r"[\n\r。；;]", text) if part.strip()]
    return "；".join(
        part for part in parts if any(keyword in part for keyword in keywords)
    )[:240]


def extract_contract_from_text(text: str) -> dict:
    hospital = pick_value(text, ["医院名称", "医院", "客户名称"])
    if not hospital:
        match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,}医院)", text)
        hospital = match.group(1).strip() if match else ""

    amount = (
        pick_value(text, ["签约合同金额", "合同金额", "金额"])
        or (re.search(r"([0-9,.]+\s*(?:亿元|亿|万元|万|元))", text).group(1) if re.search(r"([0-9,.]+\s*(?:亿元|亿|万元|万|元))", text) else "")
    )
    key_terms = pick_value(text, ["关键合同条款", "合同条款", "条款"]) or find_sentence(
        text, ["回款", "保底", "托管", "独家", "库存", "冷链", "配送", "封顶"]
    )
    category_text = pick_value(text, ["在运营供应链品类", "供应链品类", "品类"])
    known_categories = ["高值耗材", "低值耗材", "试剂", "药品", "设备维保", "器械", "办公物资", "后勤物资"]
    categories = split_categories(category_text) or [item for item in known_categories if item in text]

    return {
        "hospitalName": hospital,
        "projectName": f"{hospital}供应链运营项目" if hospital else "供应链运营项目",
        "contractAmount": parse_amount_to_wan(amount),
        "keyTerms": key_terms,
        "categories": categories,
        "source": "text_extract",
    }


def normalize_contract(payload: dict) -> dict:
    hospital = str(payload.get("hospitalName") or payload.get("hospital_name") or "").strip()
    project = str(payload.get("projectName") or payload.get("project_name") or "").strip()
    if not hospital:
        raise ValueError("hospitalName is required")
    if not project:
        project = f"{hospital}供应链运营项目"
    return {
        "id": str(payload.get("id") or "").strip() or make_id("c"),
        "hospital_name": hospital,
        "project_name": project,
        "contract_amount": parse_amount_to_wan(payload.get("contractAmount") or payload.get("contract_amount")),
        "key_terms": str(payload.get("keyTerms") or payload.get("key_terms") or "").strip(),
        "categories": split_categories(payload.get("categories")),
        "owner": str(payload.get("owner") or "").strip(),
        "source": str(payload.get("source") or "manual").strip(),
    }


def normalize_revenue(payload: dict, conn: sqlite3.Connection) -> dict:
    contract_id = str(payload.get("contractId") or payload.get("contract_id") or "").strip()
    hospital = str(payload.get("hospitalName") or payload.get("hospital_name") or "").strip()
    project = str(payload.get("projectName") or payload.get("project_name") or "").strip()

    if not contract_id and hospital:
        row = conn.execute(
            """
            SELECT id FROM contracts
            WHERE hospital_name = ? AND (? = '' OR project_name = ?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (hospital, project, project),
        ).fetchone()
        if row:
            contract_id = row["id"]

    if not contract_id:
        raise ValueError("contractId or hospitalName is required")

    week = str(payload.get("week") or "").strip()
    if not re.match(r"^\d{4}-W\d{2}$", week):
        raise ValueError("week must look like 2026-W25")

    return {
        "id": str(payload.get("id") or "").strip() or make_id("r"),
        "contract_id": contract_id,
        "week": week,
        "amount": parse_amount_to_wan(payload.get("amount")),
        "submitted_by": str(payload.get("submittedBy") or payload.get("submitted_by") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
    }


def upsert_contract(conn: sqlite3.Connection, payload: dict) -> dict:
    item = normalize_contract(payload)
    existing = conn.execute(
        "SELECT id, created_at FROM contracts WHERE hospital_name = ? AND project_name = ?",
        (item["hospital_name"], item["project_name"]),
    ).fetchone()
    timestamp = now_iso()
    if existing:
        item["id"] = existing["id"]
        created = existing["created_at"]
    else:
        created = timestamp

    conn.execute(
        """
        INSERT INTO contracts
        (id, hospital_name, project_name, contract_amount, key_terms, categories, owner, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hospital_name, project_name) DO UPDATE SET
          contract_amount = excluded.contract_amount,
          key_terms = excluded.key_terms,
          categories = excluded.categories,
          owner = excluded.owner,
          source = excluded.source,
          updated_at = excluded.updated_at
        """,
        (
            item["id"],
            item["hospital_name"],
            item["project_name"],
            item["contract_amount"],
            item["key_terms"],
            json.dumps(item["categories"], ensure_ascii=False),
            item["owner"],
            item["source"],
            created,
            timestamp,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM contracts WHERE id = ?", (item["id"],)).fetchone()
    return row_to_contract(row)


def upsert_revenue(conn: sqlite3.Connection, payload: dict) -> dict:
    item = normalize_revenue(payload, conn)
    contract_exists = conn.execute("SELECT id FROM contracts WHERE id = ?", (item["contract_id"],)).fetchone()
    if not contract_exists:
        raise ValueError("contract not found")

    existing = conn.execute(
        "SELECT id, created_at FROM weekly_revenue WHERE contract_id = ? AND week = ?",
        (item["contract_id"], item["week"]),
    ).fetchone()
    timestamp = now_iso()
    if existing:
        item["id"] = existing["id"]
        created = existing["created_at"]
    else:
        created = timestamp

    conn.execute(
        """
        INSERT INTO weekly_revenue
        (id, contract_id, week, amount, submitted_by, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_id, week) DO UPDATE SET
          amount = excluded.amount,
          submitted_by = excluded.submitted_by,
          note = excluded.note,
          updated_at = excluded.updated_at
        """,
        (
            item["id"],
            item["contract_id"],
            item["week"],
            item["amount"],
            item["submitted_by"],
            item["note"],
            created,
            timestamp,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM weekly_revenue WHERE id = ?", (item["id"],)).fetchone()
    return row_to_revenue(row)


def get_contracts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM contracts ORDER BY hospital_name, project_name").fetchall()
    return [row_to_contract(row) for row in rows]


def get_revenue(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM weekly_revenue ORDER BY week DESC, updated_at DESC").fetchall()
    return [row_to_revenue(row) for row in rows]


def build_dashboard(conn: sqlite3.Connection, year: int) -> dict:
    contracts = get_contracts(conn)
    revenue = get_revenue(conn)
    contract_by_id = {item["id"]: item for item in contracts}
    weeks = sorted({item["week"] for item in revenue if item["week"].startswith(f"{year}-")})
    selected_week = weeks[-1] if weeks else ""

    revenue_by_contract: dict[str, list[dict]] = {}
    for record in revenue:
        if record["week"].startswith(f"{year}-"):
            revenue_by_contract.setdefault(record["contractId"], []).append(record)

    rows = []
    category_totals: dict[str, float] = {}
    week_totals: dict[str, float] = {week: 0 for week in weeks}

    for contract in contracts:
        contract_revenue = revenue_by_contract.get(contract["id"], [])
        ytd = sum(float(item["amount"] or 0) for item in contract_revenue)
        week_amount = sum(
            float(item["amount"] or 0) for item in contract_revenue if item["week"] == selected_week
        )
        for item in contract_revenue:
            week_totals[item["week"]] = week_totals.get(item["week"], 0) + float(item["amount"] or 0)
        share = ytd / max(len(contract["categories"]), 1)
        for category in contract["categories"]:
            category_totals[category] = category_totals.get(category, 0) + share
        rows.append(
            {
                **contract,
                "selectedWeekRevenue": week_amount,
                "ytdRevenue": ytd,
                "weeklySeries": [
                    {"week": week, "amount": sum(float(item["amount"] or 0) for item in contract_revenue if item["week"] == week)}
                    for week in weeks
                ],
            }
        )

    rows.sort(key=lambda item: item["ytdRevenue"], reverse=True)
    total_contract = sum(float(item["contractAmount"] or 0) for item in contracts)
    total_ytd = sum(float(item["ytdRevenue"] or 0) for item in rows)
    selected_week_total = week_totals.get(selected_week, 0)
    previous_week = weeks[-2] if len(weeks) > 1 else ""
    previous_week_total = week_totals.get(previous_week, 0)
    wow = ((selected_week_total - previous_week_total) / previous_week_total * 100) if previous_week_total else 0

    return {
        "year": year,
        "selectedWeek": selected_week,
        "summary": {
            "hospitalCount": len({item["hospitalName"] for item in contracts}),
            "projectCount": len(contracts),
            "totalContractAmount": total_contract,
            "totalYtdRevenue": total_ytd,
            "selectedWeekRevenue": selected_week_total,
            "wowChange": wow,
            "categoryCount": len(category_totals),
            "conversionRate": (total_ytd / total_contract * 100) if total_contract else 0,
        },
        "rows": rows,
        "weeks": weeks,
        "trend": [{"week": week, "amount": week_totals.get(week, 0)} for week in weeks],
        "categoryMix": [
            {"category": category, "amount": amount}
            for category, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
        ],
        "lastUpdated": now_iso(),
    }


def csv_response(headers: list[str], rows: list[list[object]]) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def xlsx_column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return max(value - 1, 0)


def parse_xlsx_bytes(xlsx_bytes: bytes) -> dict[str, list[list[object]]]:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    sheets: dict[str, list[list[object]]] = {}
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", ns):
                text = "".join(node.text or "" for node in item.findall(".//main:t", ns))
                shared_strings.append(text)

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rel_root.findall("pkgrel:Relationship", ns)
        }

        for sheet in workbook_root.findall("main:sheets/main:sheet", ns):
            name = sheet.attrib["name"]
            rel_id = sheet.attrib.get(f"{{{ns['rel']}}}id")
            target = rels.get(rel_id or "")
            if not target:
                continue
            if target.startswith("/xl/"):
                sheet_path = target.lstrip("/")
            elif target.startswith("xl/"):
                sheet_path = target
            else:
                sheet_path = f"xl/{target.lstrip('/')}"
            if sheet_path not in archive.namelist():
                continue
            sheet_root = ET.fromstring(archive.read(sheet_path))
            table_rows: list[list[object]] = []
            for row_node in sheet_root.findall(".//main:sheetData/main:row", ns):
                row_values: list[object] = []
                for cell in row_node.findall("main:c", ns):
                    ref = cell.attrib.get("r", "")
                    col_index = xlsx_column_index(ref)
                    while len(row_values) <= col_index:
                        row_values.append(None)
                    cell_type = cell.attrib.get("t", "")
                    value_node = cell.find("main:v", ns)
                    inline_node = cell.find("main:is", ns)
                    value: object = None
                    if cell_type == "inlineStr" and inline_node is not None:
                        value = "".join(node.text or "" for node in inline_node.findall(".//main:t", ns))
                    elif value_node is not None:
                        raw = value_node.text or ""
                        if cell_type == "s":
                            value = shared_strings[int(raw)] if raw.isdigit() and int(raw) < len(shared_strings) else raw
                        elif cell_type == "b":
                            value = raw == "1"
                        else:
                            value = parse_number(raw)
                    row_values[col_index] = value
                if any(value not in (None, "") for value in row_values):
                    table_rows.append(row_values)
            sheets[name] = table_rows
    return sheets


def parse_number(raw: str) -> object:
    if raw == "":
        return None
    try:
        number = float(raw)
    except ValueError:
        return raw
    if number.is_integer():
        return int(number)
    return number


def rows_to_dicts(rows: list[list[object]]) -> list[dict[str, object]]:
    header_index = None
    headers: list[str] = []
    for index, row in enumerate(rows[:30]):
        clean = [str(value).strip() if value not in (None, "") else "" for value in row]
        if sum(1 for value in clean if value) >= 3:
            header_index = index
            headers = clean
            break
    if header_index is None:
        return []

    records: list[dict[str, object]] = []
    for row in rows[header_index + 1 :]:
        record: dict[str, object] = {}
        for col_index, header in enumerate(headers):
            if header:
                record[header] = row[col_index] if col_index < len(row) else None
        if any(value not in (None, "") for value in record.values()):
            records.append(record)
    return records


def to_float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return parse_amount_to_wan(text)


def value_from(record: dict[str, object], fields: list[str]) -> object:
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return value
    return None


def period_to_week(period: object, fallback_date: object = None) -> str:
    text = str(period or "").strip()
    match = re.search(r"(\d{4})(\d{2})(\d{2})", text)
    if match:
        dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    if isinstance(fallback_date, (int, float)) and fallback_date > 20000:
        # Excel serial date. 25569 is 1970-01-01 in Excel's date system.
        timestamp = (float(fallback_date) - 25569) * 86400
        dt = datetime.utcfromtimestamp(timestamp)
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    date_text = str(fallback_date or "").strip()
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", date_text)
    if match:
        dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return ""


def normalize_hospital_name(value: object, sheet_name: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        if "北儿" in sheet_name:
            text = "北京儿童医院"
        elif "珠海" in sheet_name:
            text = "珠海市人民医院"
        elif "地坛" in sheet_name:
            text = "北京地坛医院"
        elif "云交易" in sheet_name:
            text = "未归属医院"
    if "洛阳正骨" in text:
        return "洛阳正骨医院"
    if "北京地坛" in text:
        return "北京地坛医院"
    if "北京儿童" in text:
        return "北京儿童医院"
    return re.sub(r"[（(]互联网医院[）)]", "", text).strip()


def add_import_record(
    records: list[dict[str, object]],
    *,
    sheet_name: str,
    record: dict[str, object],
    project_name: str,
    amount: float,
    categories: list[str],
    hospital_value: object = None,
    period_value: object = None,
    date_value: object = None,
) -> None:
    if amount == 0:
        return
    hospital = normalize_hospital_name(
        hospital_value if hospital_value not in (None, "") else value_from(record, ["医院名称", "医院", "所属机构", "企业名称"]),
        sheet_name,
    )
    if not hospital:
        hospital = "未归属医院"
    week = period_to_week(period_value if period_value not in (None, "") else record.get("时间周期"), date_value)
    if not week:
        return
    clean_categories = [item for item in categories if item]
    if not clean_categories:
        clean_categories = [project_name]
    records.append(
        {
            "hospitalName": hospital,
            "projectName": project_name,
            "week": week,
            "amount": amount,
            "categories": clean_categories,
            "sourceSheet": sheet_name,
        }
    )


def extract_operational_records_from_workbook(xlsx_bytes: bytes) -> tuple[list[dict[str, object]], dict[str, object]]:
    workbook = parse_xlsx_bytes(xlsx_bytes)
    records: list[dict[str, object]] = []
    diagnostics: dict[str, object] = {"sheets": [], "skippedSheets": []}

    for sheet_name, rows in workbook.items():
        table = rows_to_dicts(rows)
        before = len(records)
        if sheet_name == "处方流转订单表":
            for row in table:
                status = str(row.get("平台支付状态") or "")
                if status and "支付" not in status:
                    continue
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="处方流转",
                    amount=to_float(row.get("处方药品金额")),
                    categories=["药品"],
                    period_value=row.get("时间周期"),
                    date_value=row.get("处方生成时间"),
                )
        elif sheet_name == "BBC商城原版订单表":
            for row in table:
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="BBC商城",
                    amount=to_float(value_from(row, ["订单实付金额", "商品GMV", "订单总金额"])),
                    categories=["商城"],
                    hospital_value=row.get("企业名称"),
                    period_value=row.get("时间周期"),
                    date_value=value_from(row, ["支付时间", "创建时间"]),
                )
        elif sheet_name == "云交易":
            for row in table:
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="云交易",
                    amount=to_float(value_from(row, ["应收金额", "订单用户实付金额", "订单商品金额"])),
                    categories=["云交易"],
                    period_value=row.get("时间周期"),
                    date_value=value_from(row, ["start_date_day", "订单支付成功时间_second", "下单时间_second"]),
                )
        elif sheet_name == "北儿处方":
            for row in table:
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="处方流转",
                    amount=to_float(row.get("金额")),
                    categories=["药品"],
                    period_value=row.get("时间周期"),
                    date_value=value_from(row, ["收费时间", "下单时间"]),
                )
        elif sheet_name == "珠海双通道":
            for row in table:
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="双通道",
                    amount=to_float(value_from(row, ["小计金额", "总金额", "医保商品总金额"])),
                    categories=["药品", "医保双通道"],
                    period_value=row.get("时间周期"),
                    date_value=row.get("订单下单时间"),
                )
        elif sheet_name == "慧采":
            for row in table:
                categories = [
                    str(value).strip()
                    for value in [row.get("item_second_cate_name"), row.get("item_third_cate_name"), row.get("item_first_cate_name")]
                    if value not in (None, "")
                ]
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="慧采",
                    amount=to_float(row.get("gmv_sum")),
                    categories=categories or ["慧采"],
                    hospital_value=row.get("医院"),
                    period_value=row.get("时间周期"),
                    date_value=value_from(row, ["statdate", "dt"]),
                )
        elif sheet_name == "地坛HIV":
            for row in table:
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="HIV患者服务",
                    amount=to_float(value_from(row, ["引导成交订单金额", "引导有效订单金额"])),
                    categories=["HIV运营"],
                    period_value=row.get("时间周期"),
                    date_value=row.get("日期"),
                )
        elif sheet_name == "互医2.0":
            for row in table:
                amount = to_float(row.get("总问诊GMV")) + to_float(row.get("总药品GMV"))
                add_import_record(
                    records,
                    sheet_name=sheet_name,
                    record=row,
                    project_name="互联网医院2.0",
                    amount=amount,
                    categories=["问诊", "药品"],
                    hospital_value=row.get("医院"),
                    period_value=row.get("时间周期"),
                )
        else:
            diagnostics["skippedSheets"].append(sheet_name)

        imported = len(records) - before
        diagnostics["sheets"].append({"sheet": sheet_name, "rows": len(table), "importedRows": imported})

    return records, diagnostics


def import_operational_records(conn: sqlite3.Connection, records: list[dict[str, object]]) -> dict[str, object]:
    if records:
        conn.execute("DELETE FROM weekly_revenue WHERE contract_id IN (SELECT id FROM contracts WHERE source = 'seed')")
        conn.execute("DELETE FROM contracts WHERE source = 'seed'")
        conn.commit()

    contracts: dict[tuple[str, str], dict[str, object]] = {}
    revenue: dict[tuple[str, str, str], float] = {}
    source_sheets: dict[tuple[str, str], set[str]] = {}

    for record in records:
        hospital = str(record["hospitalName"])
        project = str(record["projectName"])
        week = str(record["week"])
        amount = float(record["amount"] or 0)
        key = (hospital, project)
        if key not in contracts:
            contracts[key] = {
                "hospitalName": hospital,
                "projectName": project,
                "contractAmount": 0,
                "keyTerms": "由运营底表导入；签约合同金额和关键合同条款待合同主数据补充",
                "categories": set(),
                "owner": "",
                "source": "excel_operational_base",
            }
            source_sheets[key] = set()
        contracts[key]["categories"].update(record.get("categories") or [])
        source_sheets[key].add(str(record.get("sourceSheet") or ""))
        revenue[(hospital, project, week)] = revenue.get((hospital, project, week), 0) + amount

    saved_contracts = []
    contract_ids: dict[tuple[str, str], str] = {}
    for key, payload in contracts.items():
        categories = sorted(payload["categories"]) or [payload["projectName"]]
        payload["categories"] = categories
        payload["keyTerms"] = f"{payload['keyTerms']}；来源：{'、'.join(sorted(source_sheets[key]))}"
        saved = upsert_contract(conn, payload)
        saved_contracts.append(saved)
        contract_ids[key] = saved["id"]

    saved_revenue = []
    for (hospital, project, week), amount in revenue.items():
        saved_revenue.append(
            upsert_revenue(
                conn,
                {
                    "contractId": contract_ids[(hospital, project)],
                    "week": week,
                    "amount": amount,
                    "submittedBy": "Excel底表导入",
                    "note": "按医院、业务线、周聚合导入",
                },
            )
        )

    return {
        "contractCount": len(saved_contracts),
        "weeklyRevenueCount": len(saved_revenue),
        "rawRecordCount": len(records),
    }


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "HospitalSupplyChainDashboard/1.0"

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean_path = parsed.path
        if clean_path == "/":
            clean_path = "/index.html"
        safe = Path(clean_path.lstrip("/"))
        target = (PUBLIC_DIR / safe).resolve()
        public_root = PUBLIC_DIR.resolve()
        if public_root not in target.parents and target != public_root:
            return str(PUBLIC_DIR / "index.html")
        return str(target)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            self.handle_api_get(parsed)
            return
        if path.startswith("/download/"):
            self.handle_download(path)
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api_post(parsed)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_bytes(self, data: bytes, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api_get(self, parsed) -> None:
        try:
            with connect_db() as conn:
                if parsed.path == "/api/health":
                    self.send_json({"ok": True, "time": now_iso()})
                elif parsed.path == "/api/contracts":
                    self.send_json({"contracts": get_contracts(conn)})
                elif parsed.path == "/api/revenue":
                    self.send_json({"revenue": get_revenue(conn)})
                elif parsed.path == "/api/dashboard":
                    qs = parse_qs(parsed.query)
                    year = int(qs.get("year", [CURRENT_YEAR])[0])
                    self.send_json(build_dashboard(conn, year))
                else:
                    self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_api_post(self, parsed) -> None:
        try:
            if parsed.path == "/api/import/excel":
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    self.send_json({"error": "未收到 Excel 文件"}, HTTPStatus.BAD_REQUEST)
                    return
                xlsx_bytes = self.rfile.read(length)
                records, diagnostics = extract_operational_records_from_workbook(xlsx_bytes)
                with connect_db() as conn:
                    result = import_operational_records(conn, records)
                self.send_json({"imported": result, "diagnostics": diagnostics})
                return

            payload = parse_json_body(self)
            with connect_db() as conn:
                if parsed.path == "/api/contracts/upsert":
                    items = payload if isinstance(payload, list) else [payload]
                    saved = [upsert_contract(conn, item) for item in items]
                    self.send_json({"saved": saved})
                elif parsed.path == "/api/revenue/weekly":
                    items = payload if isinstance(payload, list) else [payload]
                    saved = [upsert_revenue(conn, item) for item in items]
                    self.send_json({"saved": saved})
                elif parsed.path == "/api/extract":
                    text = str(payload.get("text") or "")
                    self.send_json({"contract": extract_contract_from_text(text)})
                else:
                    self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_download(self, path: str) -> None:
        with connect_db() as conn:
            if path == "/download/contracts.csv":
                contracts = get_contracts(conn)
                rows = [
                    [
                        item["id"],
                        item["hospitalName"],
                        item["projectName"],
                        item["contractAmount"],
                        item["keyTerms"],
                        "、".join(item["categories"]),
                        item["owner"],
                        item["updatedAt"],
                    ]
                    for item in contracts
                ]
                data = csv_response(
                    ["合同ID", "医院名称", "项目名称", "签约合同金额", "关键合同条款", "在运营供应链品类", "负责人", "更新时间"],
                    rows,
                )
                self.send_bytes(data, "contracts.csv", "text/csv; charset=utf-8")
            elif path == "/download/revenue.csv":
                rows = conn.execute(
                    """
                    SELECT r.id, c.hospital_name, c.project_name, r.week, r.amount, r.submitted_by, r.note, r.updated_at
                    FROM weekly_revenue r
                    JOIN contracts c ON c.id = r.contract_id
                    ORDER BY r.week DESC, c.hospital_name
                    """
                ).fetchall()
                data = csv_response(
                    ["记录ID", "医院名称", "项目名称", "周", "运营收入", "填报人", "备注", "更新时间"],
                    [
                        [
                            row["id"],
                            row["hospital_name"],
                            row["project_name"],
                            row["week"],
                            row["amount"],
                            row["submitted_by"],
                            row["note"],
                            row["updated_at"],
                        ]
                        for row in rows
                    ],
                )
                self.send_bytes(data, "weekly_revenue.csv", "text/csv; charset=utf-8")
            elif path == "/download/all.json":
                data = {
                    "contracts": get_contracts(conn),
                    "revenue": get_revenue(conn),
                    "dashboard": build_dashboard(conn, CURRENT_YEAR),
                }
                payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_bytes(payload, "hospital_supply_chain_data.json", "application/json; charset=utf-8")
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8910")))
    args = parser.parse_args()

    init_db()
    mimetypes.add_type("text/javascript", ".js")
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"医院供应链多人填报看板已启动: http://{url_host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
