from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path


KNOWN_SOURCES = {
    "处方流转订单表",
    "BBC商城原版订单表",
    "云交易",
    "北儿处方",
    "珠海双通道",
    "慧采",
    "地坛HIV",
    "互医2.0",
}


def load_parser(path: Path):
    spec = importlib.util.spec_from_file_location("excel_parser", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载 Excel 解析器: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_changed_at(path: Path) -> float:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return float(result.stdout.strip())
    except OSError:
        pass
    return path.stat().st_mtime


def select_latest_sources(input_dir: Path, parser):
    files = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".xlsx", ".xlsm"}
        and not path.name.startswith("~$")
    )
    candidates: dict[str, list[dict]] = defaultdict(list)
    skipped = []
    for path in files:
        records, diagnostics = parser.extract_operational_records_from_workbook(path.read_bytes())
        changed_at = git_changed_at(path)
        records_by_source: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            records_by_source[str(record.get("sourceSheet") or "未知")].append(record)

        for sheet in diagnostics.get("sheets", []):
            source = str(sheet.get("sheet") or "")
            if source not in KNOWN_SOURCES:
                continue
            candidates[source].append(
                {
                    "path": path,
                    "changedAt": changed_at,
                    "records": records_by_source.get(source, []),
                    "rows": int(sheet.get("rows") or 0),
                    "importedRows": int(sheet.get("importedRows") or 0),
                }
            )
        skipped.extend(
            {"file": path.name, "sheet": name}
            for name in diagnostics.get("skippedSheets", [])
        )

    selected = {}
    all_records = []
    sheets = []
    for source, source_candidates in candidates.items():
        winner = max(source_candidates, key=lambda item: (item["changedAt"], str(item["path"])))
        selected[source] = winner["path"].name
        all_records.extend(winner["records"])
        sheets.append(
            {
                "sheet": source,
                "file": winner["path"].name,
                "rows": winner["rows"],
                "importedRows": winner["importedRows"],
            }
        )

    return all_records, sheets, selected, skipped, files


def read_embedded_data(dashboard_path: Path) -> dict:
    html = dashboard_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*const DATA = (.*);\s*$", html)
    if not match:
        return {}
    return json.loads(match.group(1))


def records_from_dashboard(data: dict) -> list[dict]:
    records = []
    for row in data.get("rows", []):
        sources = [str(source) for source in row.get("sources", []) if source]
        source = sources[0] if len(sources) == 1 else "历史汇总"
        for item in row.get("weeklySeries", []):
            amount = float(item.get("amount") or 0)
            if amount == 0:
                continue
            records.append(
                {
                    "hospitalName": row.get("hospitalName") or "未归属医院",
                    "projectName": row.get("projectName") or "未归属项目",
                    "week": item.get("week") or "",
                    "amount": amount,
                    "categories": row.get("categories") or [row.get("projectName") or "未归属项目"],
                    "sourceSheet": source,
                }
            )
    return records


def add_history_fallback(records: list[dict], sheets: list[dict], selected: dict, existing_data: dict) -> None:
    historical_records = records_from_dashboard(existing_data)
    historical_by_source: dict[str, list[dict]] = defaultdict(list)
    for record in historical_records:
        historical_by_source[str(record.get("sourceSheet") or "历史汇总")].append(record)

    previous_sheets = {str(item.get("sheet")): item for item in existing_data.get("sheets", [])}
    for source in KNOWN_SOURCES:
        if source in selected or source not in historical_by_source:
            continue
        fallback = historical_by_source[source]
        records.extend(fallback)
        selected[source] = "上次已发布数据"
        previous = previous_sheets.get(source, {})
        sheets.append(
            {
                "sheet": source,
                "file": "上次已发布数据",
                "rows": int(previous.get("rows") or len(fallback)),
                "importedRows": int(previous.get("importedRows") or len(fallback)),
            }
        )


def build_data(records: list[dict], sheets: list[dict], selected: dict, files: list[Path]) -> dict:
    years = sorted(
        {
            int(match.group(1))
            for record in records
            if (match := re.match(r"(\d{4})-W\d{2}$", str(record.get("week") or "")))
        }
    )
    reporting_year = years[-1] if years else datetime.now().year
    ytd_records = [record for record in records if str(record.get("week") or "").startswith(f"{reporting_year}-")]

    weekly: dict[tuple[str, str, str], float] = defaultdict(float)
    meta = {}
    category_totals: dict[str, float] = defaultdict(float)
    source_totals: dict[str, float] = defaultdict(float)
    source_rows: dict[str, int] = defaultdict(int)

    for record in ytd_records:
        hospital = str(record["hospitalName"])
        project = str(record["projectName"])
        week = str(record["week"])
        amount = float(record.get("amount") or 0)
        key = (hospital, project)
        weekly[(hospital, project, week)] += amount
        item = meta.setdefault(
            key,
            {"hospitalName": hospital, "projectName": project, "categories": set(), "sources": set()},
        )
        item["categories"].update(record.get("categories") or [])
        item["sources"].add(record.get("sourceSheet") or "")

        categories = record.get("categories") or [project]
        share = amount / max(len(categories), 1)
        for category in categories:
            category_totals[str(category)] += share
        source = str(record.get("sourceSheet") or "未知")
        source_totals[source] += amount
        source_rows[source] += 1

    weeks = sorted({week for _, _, week in weekly})
    selected_week = weeks[-1] if weeks else ""
    previous_week = weeks[-2] if len(weeks) > 1 else ""
    rows = []
    for (hospital, project), item in meta.items():
        ytd = sum(value for (h, p, _), value in weekly.items() if h == hospital and p == project)
        rows.append(
            {
                "hospitalName": hospital,
                "contractAmount": None,
                "keyTerms": "待合同扫描件/OCR或合同主数据补充",
                "projectName": project,
                "categories": sorted(item["categories"]) or [project],
                "sources": sorted(source for source in item["sources"] if source),
                "selectedWeekRevenue": round(weekly.get((hospital, project, selected_week), 0), 2),
                "ytdRevenue": round(ytd, 2),
                "weeklySeries": [
                    {"week": week, "amount": round(weekly.get((hospital, project, week), 0), 2)}
                    for week in weeks
                ],
            }
        )
    rows.sort(key=lambda row: row["ytdRevenue"], reverse=True)

    trend = [
        {"week": week, "amount": round(sum(value for (_, _, w), value in weekly.items() if w == week), 2)}
        for week in weeks
    ]
    selected_total = next((item["amount"] for item in trend if item["week"] == selected_week), 0)
    previous_total = next((item["amount"] for item in trend if item["week"] == previous_week), 0)
    wow = ((selected_total - previous_total) / previous_total * 100) if previous_total else 0

    return {
        "generatedDate": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "reportingYear": reporting_year,
        "inputFile": f"{len(files)} 个 Excel 文件",
        "selectedSources": selected,
        "summary": {
            "hospitalCount": len({row["hospitalName"] for row in rows}),
            "projectCount": len(rows),
            "categoryCount": len(category_totals),
            "rawRecordCount": len(records),
            "rawRecordCount2026": len(ytd_records),
            "weeklyRevenueCount": len(weekly),
            "totalYtdRevenue": round(sum(row["ytdRevenue"] for row in rows), 2),
            "selectedWeekRevenue": round(selected_total, 2),
            "wowChange": round(wow, 2),
            "selectedWeek": selected_week,
            "previousWeek": previous_week,
        },
        "rows": rows,
        "trend": trend,
        "categoryMix": [
            {"category": key, "amount": round(value, 2)}
            for key, value in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
        ],
        "sourceMix": [
            {"source": key, "amount": round(value, 2), "rows": source_rows[key]}
            for key, value in sorted(source_totals.items(), key=lambda item: item[1], reverse=True)
        ],
        "sheets": sorted(sheets, key=lambda item: item["sheet"]),
    }


def build_spec() -> dict:
    return {
        "name": "hospital_supply_chain_data_port",
        "version": "2.0.0",
        "unit": "CNY",
        "generatedDate": datetime.now().strftime("%Y-%m-%d"),
        "dashboardFields": [
            "医院名称",
            "签约合同金额",
            "关键合同条款",
            "在运营的供应链品类",
            "每个项目每周运营收入",
            "YTD运营收入",
        ],
        "endpoints": [
            {
                "method": "UPLOAD",
                "path": "/input/*.xlsx",
                "purpose": "上传每周最新 Excel 底表，自动刷新看板并累计 YTD",
                "payloadExample": {"file": "本周系统导出底表.xlsx"},
            }
        ],
        "fieldMapping": [
            {"sheet": "处方流转订单表", "hospital": "医院名称", "project": "处方流转", "week": "时间周期", "amount": "处方药品金额", "category": "药品"},
            {"sheet": "BBC商城原版订单表", "hospital": "企业名称", "project": "BBC商城", "week": "时间周期", "amount": "订单实付金额/商品GMV/订单总金额", "category": "商城"},
            {"sheet": "云交易", "hospital": "未归属医院", "project": "云交易", "week": "时间周期", "amount": "应收金额/订单用户实付金额", "category": "云交易"},
            {"sheet": "北儿处方", "hospital": "北京儿童医院", "project": "处方流转", "week": "时间周期", "amount": "金额", "category": "药品"},
            {"sheet": "珠海双通道", "hospital": "珠海市人民医院", "project": "双通道", "week": "时间周期", "amount": "小计金额/总金额", "category": "药品、医保双通道"},
            {"sheet": "慧采", "hospital": "医院", "project": "慧采", "week": "时间周期/statdate", "amount": "gmv_sum", "category": "item_second_cate_name/item_third_cate_name"},
            {"sheet": "地坛HIV", "hospital": "北京地坛医院", "project": "HIV患者服务", "week": "时间周期", "amount": "引导成交订单金额", "category": "HIV运营"},
            {"sheet": "互医2.0", "hospital": "医院", "project": "互联网医院2.0", "week": "时间周期", "amount": "总问诊GMV+总药品GMV", "category": "问诊、药品"},
        ],
    }


def replace_embedded_json(html: str, variable: str, value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    pattern = rf"(?m)^(\s*)const {re.escape(variable)} = .*?;\s*$"
    replacement = rf"\1const {variable} = {payload};"
    updated, count = re.subn(pattern, replacement, html, count=1)
    if count != 1:
        raise RuntimeError(f"index.html 中没有找到 const {variable}")
    return updated


def main() -> int:
    parser_args = argparse.ArgumentParser()
    parser_args.add_argument("--input-dir", default="input")
    parser_args.add_argument("--dashboard", default="index.html")
    parser_args.add_argument("--parser", default="scripts/excel_parser.py")
    parser_args.add_argument("--summary", default="hospital_supply_chain_dashboard_summary.json")
    parser_args.add_argument("--spec", default="hospital_supply_chain_data_port_spec.json")
    args = parser_args.parse_args()

    input_dir = Path(args.input_dir)
    dashboard_path = Path(args.dashboard)
    existing_data = read_embedded_data(dashboard_path)
    parser = load_parser(Path(args.parser))
    records, sheets, selected, skipped, files = select_latest_sources(input_dir, parser)
    add_history_fallback(records, sheets, selected, existing_data)
    if not records:
        raise RuntimeError("没有识别到可导入的数据，请检查 input/README.md 中的 Sheet 名称")
    data = build_data(records, sheets, selected, files)
    spec = build_spec()

    html = dashboard_path.read_text(encoding="utf-8")
    html = replace_embedded_json(html, "DATA", data)
    html = replace_embedded_json(html, "SPEC", spec)
    dashboard_path.write_text(html, encoding="utf-8")

    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    spec_path = Path(args.spec)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"selectedSources": selected, "summary": data["summary"], "skipped": skipped}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
