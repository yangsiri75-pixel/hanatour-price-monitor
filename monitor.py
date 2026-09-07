#!/usr/bin/env python3
"""하나투어 경쟁상품 출발일별 가격 모니터링.

실행 흐름
1. 하나투어 상품목록 API(getPkgProdLst)에서 대표상품(MJK1116) 전체 출발일을 조회
2. config.json 의 products 조건(상품코드 접두/접미)으로 대상 상품만 추출
3. data/latest.json(직전 스냅샷)과 비교 → 가격변동 / 신규출발일 / 삭제출발일 / 잔여석·출발확정 변경
4. data/history.csv 누적, data/latest.json 갱신, data/changes.json + data/report.md 작성
5. 변동이 있으면 즉시, 없으면 heartbeat_days(기본 2일)마다 이메일 발송 (SMTP 환경변수 있을 때)

환경변수 (GitHub Secrets)
  SMTP_USER  발신 Gmail 주소
  SMTP_PASS  Gmail 앱 비밀번호
  MAIL_TO    수신자 (쉼표로 여러 명)
  FORCE_NOTIFY=1  강제 발송 (테스트용)
"""
import csv
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
KST = timezone(timedelta(hours=9))

LATEST = DATA / "latest.json"
HISTORY = DATA / "history.csv"
CHANGES = DATA / "changes.json"
REPORT = DATA / "report.md"
STATE = DATA / "state.json"

KEEP_FIELDS = [
    "saleProdCd", "saleProdNm", "adtAmt", "chdAmt", "infAmt", "dcAmt",
    "seatCnt", "remaSeatCnt", "depDay", "depTm", "arrDay", "arrTm",
    "depFixYn", "bkngStatCd", "minDepNop", "promCd",
]
HISTORY_COLS = ["run_at", "product_id", "depDay", "saleProdCd", "adtAmt", "chdAmt",
                "infAmt", "remaSeatCnt", "seatCnt", "depFixYn", "bkngStatCd"]


def load_json(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def won(v):
    try:
        return f"{int(v):,}원"
    except Exception:
        return str(v)


def fmt_day(d):
    try:
        dt = datetime.strptime(d, "%Y%m%d")
        return dt.strftime("%m/%d") + "(" + "월화수목금토일"[dt.weekday()] + ")"
    except Exception:
        return d


def fetch_all(cfg, start, end):
    api = cfg["api"]
    base = {
        "sort": "RPRS_SORT1", "ljoinDvCd": "", "dtClrNum": "",
        "pageSize": str(api.get("page_size", 200)), "ptnCd": api["ptnCd"],
        "saleSiteCd": "", "areaCd": api["areaCd"], "depCityCd": api["depCityCd"],
        "strtDepDay": start, "endDepDay": end, "prodDtlAttrCd": "",
        "rprsProdCds": api["rprsProdCds"], "dtcmAreaCd": "", "prodDvCd": "",
        "scods": "", "rprsProdAirEnn": "Y", "prodTypeCd": "", "trvlDayCnts": "",
        "promCds": "", "adtMinAmt": "", "adtMaxAmt": "", "prodBrndCds": "",
        "rcctCds": "", "frdmSchdYn": "", "tipInclYn": "", "chssInclYn": "",
        "shpnYn": "", "tcEnn": "", "guidInclYn": "", "shipInclYn": "",
        "thmCdCont": "", "monYn": "", "tueYn": "", "wedYn": "", "thuYn": "",
        "friYn": "", "satYn": "", "sndyYn": "", "depTms": "", "depAirCds": "",
        "htlGradCds": "", "cpndCityProdYn": "", "rprsProdNm": "",
        "inpPathCd": "CBP", "depDay": "", "depYm": "", "page": "1",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "prgmId": api["prgmId"],
        "Origin": "https://hope.hanatour.com",
        "Referer": "https://hope.hanatour.com/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    }
    items, page_size = [], int(base["pageSize"])
    for page in range(1, 30):
        body = dict(base, page=str(page))
        last_err = None
        for attempt in range(3):
            try:
                r = requests.post(api["url"], json=body, headers=headers, timeout=30)
                r.raise_for_status()
                j = r.json()
                break
            except Exception as e:  # 재시도
                last_err = e
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"API 호출 실패 (page {page}): {last_err}")
        data = j.get("data") or {}
        lst = data.get("prodList") or []
        items.extend(lst)
        total = int(data.get("allNoc") or 0)
        if len(lst) < page_size or len(items) >= total:
            break
        time.sleep(1)
    return items


def pick(item):
    return {k: item.get(k) for k in KEEP_FIELDS}


def build_snapshot(cfg, items):
    snap = {}
    for prod in cfg["products"]:
        pre, suf = prod.get("code_prefix", ""), prod.get("code_suffix", "")
        contains = prod.get("name_contains")
        rows = {}
        for it in items:
            code = it.get("saleProdCd") or ""
            if pre and not code.startswith(pre):
                continue
            if suf and not code.endswith(suf):
                continue
            if contains and contains not in (it.get("saleProdNm") or ""):
                continue
            rows[it["depDay"]] = pick(it)
        snap[prod["id"]] = {"label": prod["label"], "dates": dict(sorted(rows.items()))}
    return snap


def diff(prev, cur, cfg, today):
    watch = cfg["notify"].get("watch_fields", ["adtAmt"])
    info = cfg["notify"].get("info_fields", [])
    out = {"price_changes": [], "new_dates": [], "removed_dates": [], "info_changes": []}
    for pid, p in cur.items():
        pv = (prev.get(pid) or {}).get("dates", {})
        cv = p["dates"]
        for d, row in cv.items():
            if d not in pv:
                if prev:  # 최초 실행은 신규로 치지 않음
                    out["new_dates"].append({"product": pid, "depDay": d, "row": row})
                continue
            old = pv[d]
            for f in watch:
                if old.get(f) != row.get(f):
                    out["price_changes"].append({
                        "product": pid, "depDay": d, "field": f,
                        "old": old.get(f), "new": row.get(f), "row": row,
                    })
            for f in info:
                if old.get(f) != row.get(f):
                    out["info_changes"].append({
                        "product": pid, "depDay": d, "field": f,
                        "old": old.get(f), "new": row.get(f), "row": row,
                    })
        for d, old in pv.items():
            if d not in cv and d >= today:
                out["removed_dates"].append({"product": pid, "depDay": d, "row": old})
    return out


def append_history(run_at, cur):
    new_file = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HISTORY_COLS)
        for pid, p in cur.items():
            for d, r in p["dates"].items():
                w.writerow([run_at, pid, d, r["saleProdCd"], r["adtAmt"], r["chdAmt"],
                            r["infAmt"], r["remaSeatCnt"], r["seatCnt"], r["depFixYn"],
                            r["bkngStatCd"]])


def status_label(row):
    tags = []
    if row.get("depFixYn") == "Y":
        tags.append("출발확정")
    if row.get("remaSeatCnt") is not None:
        tags.append(f"잔여{row['remaSeatCnt']}석")
    return " ".join(tags)


def build_report(cfg, cur, ch, run_at, first_run):
    url_t = cfg["product_page_url"]
    L = [f"# {cfg['site_name']} 가격 모니터링 리포트", "", f"- 조회시각: {run_at}"]
    if cfg.get("dashboard_url"):
        L.append(f"- 📊 대시보드(이력·그래프·달력): {cfg['dashboard_url']}")
    L.append("")
    if first_run:
        L.append("> 최초 실행 — 기준 스냅샷을 저장했습니다. 다음 실행부터 변동을 비교합니다.")
        L.append("")
    if ch["price_changes"]:
        L += ["## 🔔 가격 변동", "", "| 출발일 | 이전 | 현재 | 증감 | 상태 | 링크 |", "|---|---|---|---|---|---|"]
        for c in ch["price_changes"]:
            delta = (c["new"] or 0) - (c["old"] or 0)
            arrow = "▼" if delta < 0 else "▲"
            L.append(f"| {fmt_day(c['depDay'])} | {won(c['old'])} | **{won(c['new'])}** | {arrow} {abs(delta):,} | "
                     f"{status_label(c['row'])} | [보기]({url_t.format(code=c['row']['saleProdCd'])}) |")
        L.append("")
    if ch["new_dates"]:
        L += ["## ➕ 신규 출발일", ""]
        for c in ch["new_dates"]:
            L.append(f"- {fmt_day(c['depDay'])} {won(c['row']['adtAmt'])} {status_label(c['row'])}")
        L.append("")
    if ch["removed_dates"]:
        L += ["## ➖ 사라진 출발일 (판매종료/마감)", ""]
        for c in ch["removed_dates"]:
            L.append(f"- {fmt_day(c['depDay'])} (마지막 가격 {won(c['row']['adtAmt'])})")
        L.append("")
    if ch["info_changes"]:
        L += ["## ℹ️ 잔여석·출발확정 변경", ""]
        names = {"remaSeatCnt": "잔여석", "depFixYn": "출발확정"}
        for c in ch["info_changes"]:
            L.append(f"- {fmt_day(c['depDay'])} {names.get(c['field'], c['field'])}: {c['old']} → {c['new']}")
        L.append("")
    if not any(ch.values()) and not first_run:
        L += ["변동 없음.", ""]
    chg_map = {(c["product"], c["depDay"]): c for c in ch["price_changes"]}
    new_set = {(c["product"], c["depDay"]) for c in ch["new_dates"]}
    for pid, p in cur.items():
        L += [f"## 현재 가격표 — {p['label']}", "", f"총 {len(p['dates'])}개 출발일 (이번 조회에서 바뀐 출발일은 색으로 표시)", "",
              "| 출발일 | 성인 | 변동 | 아동 | 유아 | 잔여석 | 출발확정 | 상품코드 |", "|---|---|---|---|---|---|---|---|"]
        for d, r in p["dates"].items():
            c = chg_map.get((pid, d))
            if c:
                delta = (c["new"] or 0) - (c["old"] or 0)
                mark = f"{'▼' if delta < 0 else '▲'} {abs(delta):,} (이전 {won(c['old'])})"
            elif (pid, d) in new_set:
                mark = "🆕 신규"
            else:
                mark = ""
            L.append(f"| {fmt_day(d)} | {won(r['adtAmt'])} | {mark} | {won(r['chdAmt'])} | {won(r['infAmt'])} | "
                     f"{r['remaSeatCnt']}/{r['seatCnt']} | {'Y' if r['depFixYn']=='Y' else ''} | {r['saleProdCd']} |")
        L.append("")
    return "\n".join(L)


def md_table_to_html(md):
    """아주 단순한 마크다운→HTML (제목/표/목록/문단)."""
    import re
    html, in_table = [], False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                html.append('<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:13px">')
                in_table, tag = True, "th"
            else:
                tag = "td"
            cells = [c.replace("**", "") for c in cells]
            import re
            cells = [re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', c) for c in cells]
            joined = " ".join(cells)
            if tag == "td" and "▼" in joined:
                style, first = ' style="background:#fdecec"', ' style="color:#d03b3b;font-weight:700"'
            elif tag == "td" and "▲" in joined:
                style, first = ' style="background:#e6f0fb"', ' style="color:#1c5cab;font-weight:700"'
            elif tag == "td" and "🆕" in joined:
                style, first = ' style="background:#eaf7ea"', ' style="font-weight:700"'
            else:
                style, first = "", ""
            html.append(f"<tr{style}>" + "".join(
                f"<{tag}{first if i == 0 else ''}>{c}</{tag}>" for i, c in enumerate(cells)) + "</tr>")
            continue
        if in_table:
            html.append("</table>")
            in_table = False
        if line.startswith("# "):
            html.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("## "):
            html.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("- "):
            txt = re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', line[2:])
            html.append(f"<div>• {txt}</div>")
        elif line.startswith("> "):
            html.append(f"<p style='color:#666'>{line[2:]}</p>")
        elif line.strip():
            html.append(f"<p>{line}</p>")
    if in_table:
        html.append("</table>")
    return "<div style='font-family:sans-serif'>" + "\n".join(html) + "</div>"


def send_mail(subject, md):
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("[mail] SMTP 환경변수 없음 — 메일 발송 건너뜀")
        return False
    recipients = [x.strip() for x in to.replace(";", ",").split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(recipients)
    msg.attach(MIMEText(md, "plain", "utf-8"))
    msg.attach(MIMEText(md_table_to_html(md), "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())
    print(f"[mail] 발송 완료 → {recipients}")
    return True


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    now = datetime.now(KST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y%m%d")
    end = (now + timedelta(days=int(cfg.get("days_ahead", 150)))).strftime("%Y%m%d")

    items = fetch_all(cfg, today, end)
    print(f"[fetch] 전체 {len(items)}건 조회 ({today}~{end})")
    cur = build_snapshot(cfg, items)
    for pid, p in cur.items():
        print(f"[snapshot] {pid}: {len(p['dates'])}개 출발일")
        if not p["dates"]:
            raise RuntimeError(f"대상 상품({pid})이 0건 — 상품코드 규칙이 바뀌었을 수 있음")

    prev = load_json(LATEST, {})
    first_run = not prev
    ch = diff(prev, cur, cfg, today)
    state = load_json(STATE, {})

    has_alert = bool(ch["price_changes"] or ch["new_dates"] or ch["removed_dates"])
    last = state.get("last_notified_at")
    hb_days = int(cfg["notify"].get("heartbeat_days", 2))
    heartbeat_due = True
    if last:
        try:
            heartbeat_due = (now - datetime.fromisoformat(last)) >= timedelta(days=hb_days)
        except Exception:
            heartbeat_due = True
    force = os.environ.get("FORCE_NOTIFY") == "1"
    should_notify = first_run or has_alert or heartbeat_due or force
    reason = ("최초 실행" if first_run else "가격/출발일 변동" if has_alert
              else "정기 이상없음 보고" if heartbeat_due else "강제 발송" if force else "발송 안함")

    report = build_report(cfg, cur, ch, run_at, first_run)
    REPORT.write_text(report, encoding="utf-8")
    append_history(run_at, cur)
    LATEST.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {
        "run_at": run_at, "site": cfg["site_name"], "first_run": first_run,
        "has_price_changes": bool(ch["price_changes"]), "has_alert": has_alert,
        "notify": should_notify, "notify_reason": reason,
        "counts": {k: len(v) for k, v in ch.items()},
        "price_changes": [{"depDay": c["depDay"], "old": c["old"], "new": c["new"],
                           "delta": (c["new"] or 0) - (c["old"] or 0),
                           "code": c["row"]["saleProdCd"]} for c in ch["price_changes"]],
        "new_dates": [{"depDay": c["depDay"], "adtAmt": c["row"]["adtAmt"]} for c in ch["new_dates"]],
        "removed_dates": [{"depDay": c["depDay"], "adtAmt": c["row"]["adtAmt"]} for c in ch["removed_dates"]],
        "info_changes": [{"depDay": c["depDay"], "field": c["field"], "old": c["old"], "new": c["new"]}
                         for c in ch["info_changes"]],
        "products": {pid: {"label": p["label"], "dates": len(p["dates"]),
                           "min_adt": min(r["adtAmt"] for r in p["dates"].values()),
                           "max_adt": max(r["adtAmt"] for r in p["dates"].values())}
                     for pid, p in cur.items()},
    }
    CHANGES.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[diff] {summary['counts']} → notify={should_notify} ({reason})")

    if should_notify:
        tag = "🔔가격변동" if ch["price_changes"] else ("➕출발일변경" if has_alert else "✅이상없음")
        subject = f"[{cfg['site_name']} 모니터] {tag} {run_at}"
        if ch["price_changes"]:
            subject += f" — {len(ch['price_changes'])}건"
        try:
            if send_mail(subject, report):
                state["last_notified_at"] = now.isoformat()
        except Exception as e:
            print(f"[mail] 발송 실패: {e}", file=sys.stderr)
        if not os.environ.get("SMTP_USER"):
            state["last_notified_at"] = now.isoformat()  # 메일 미설정 시에도 주기 관리
    state["last_run_at"] = now.isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"notify={'true' if should_notify else 'false'}\n")
            f.write(f"has_alert={'true' if has_alert else 'false'}\n")


if __name__ == "__main__":
    main()
