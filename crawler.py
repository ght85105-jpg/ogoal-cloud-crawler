import csv
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import requests

BASE = "https://www.gogoalshop.se"
CLUB_URL = "https://www.gogoalshop.se/club/Premier-League/645"
OUTPUT_DIR = Path("output")

HEADERS = {"User-Agent": "Mozilla/5.0"}
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>',
    re.IGNORECASE,
)

# 可选：只抓这 9 个队。设为空 set() 表示抓联赛页里全部队伍
TEAM_WHITELIST = {
    "Manchester United",
    "Liverpool",
    "Chelsea",
    "Manchester City",
    "Arsenal",
    "Tottenham Hotspur",
    "West Ham United",
    "Newcastle United",
    "Aston Villa",
}


def sanitize_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", s).strip()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def get_html(session: requests.Session, url: str, retry: int = 7, timeout: int = 45) -> str:
    last_err = None
    for i in range(retry):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(1.3 * (i + 1))
    raise RuntimeError(f"GET failed: {url} | {last_err}")


def extract_next_data(html: str) -> dict:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("No __NEXT_DATA__ found")
    return json.loads(m.group(1))


def parse_teams_from_club_html(html: str) -> List[Tuple[str, str, str]]:
    """
    从联赛页源码中提取 /team/{slug}/{id}
    返回: [(team_name, slug, team_id), ...]
    """
    # 只匹配真正 team 链接，避免 /team/xxx.gif 这种资源链接
    paths = sorted(set(re.findall(r'href="(/team/([A-Za-z0-9\-]+)/([A-Za-z0-9]+))"', html)))
    teams = []
    for full_path, slug, team_id in paths:
        team_name = slug.replace("-", " ").strip()
        teams.append((team_name, slug, team_id))

    # 去重
    uniq = {}
    for t in teams:
        uniq[(t[1], t[2])] = t
    teams = list(uniq.values())

    if TEAM_WHITELIST:
        allow = {x.lower().strip() for x in TEAM_WHITELIST}
        teams = [t for t in teams if t[0].lower() in allow]

    return teams


def product_filename(alias_title: str, product_id: str) -> str:
    return sanitize_name(f"productdetail-{alias_title}-{product_id}.html")


def collect_products_from_page_props(page_props: dict) -> Dict[str, Dict[str, str]]:
    """
    从 pageProps.productList 提取商品
    返回: {product_id: {"alias":..., "url":...}}
    """
    out: Dict[str, Dict[str, str]] = {}
    plist = page_props.get("productList") or []
    for it in plist:
        alias = str(it.get("alias_title", "")).strip()
        pid = str(it.get("product_id", "")).strip()
        if alias and pid:
            out[pid] = {"alias": alias, "url": f"{BASE}/productdetail/{alias}/{pid}"}
    return out


def collect_products_fallback_from_html(html: str) -> Dict[str, Dict[str, str]]:
    """
    兜底：从 html 里直接提取 /productdetail/{alias}/{id}
    """
    out: Dict[str, Dict[str, str]] = {}
    matches = re.findall(r'href="(/productdetail/([^"/]+(?:-[^"/]+)*)/([0-9]+))"', html)
    for path, alias, pid in matches:
        out[pid] = {"alias": alias, "url": f"{BASE}{path}"}
    return out


def download_products_for_category(
    session: requests.Session,
    team_slug: str,
    team_id: str,
    category: dict,
    team_folder: Path,
    max_workers: int = 12,
) -> Dict[str, str]:
    """
    按源码分页全量抓一个类目，并下载商品详情页
    """
    category_title = str(category.get("title", "Unknown")).strip() or "Unknown"
    category_id = str(category.get("category_id", "")).strip()
    category_count = int(category.get("count", 0) or 0)
    category_folder = team_folder / sanitize_name(f"{team_folder.name} {category_title}")
    ensure_dir(category_folder)

    category_path = f"/team/{team_slug}/{team_id}/category-{category_id}"
    category_url = f"{BASE}{category_path}"

    result = {
        "category": category_title,
        "mode": "FULL",
        "category_id": category_id,
        "expected_total": str(category_count),
        "discovered_products": "0",
        "downloaded": "0",
        "failed": "0",
        "remain": "0",
        "note": "",
    }

    try:
        first_html = get_html(session, category_url)
        first_data = extract_next_data(first_html)
        first_pp = first_data.get("props", {}).get("pageProps", {})
    except Exception as e:
        result["mode"] = "CATEGORY_FAIL"
        result["note"] = str(e)
        return result

    page_param = first_pp.get("pageParam", {}) or {}
    total = int(page_param.get("total", category_count) or category_count or 0)
    limit = int(page_param.get("limit", 20) or 20)
    pages = max(1, int(math.ceil(total / float(limit)))) if total > 0 else 1

    all_products: Dict[str, Dict[str, str]] = {}

    # 第1页
    all_products.update(collect_products_from_page_props(first_pp))
    # 兜底提取
    if not all_products:
        all_products.update(collect_products_fallback_from_html(first_html))

    # 后续分页
    for p in range(2, pages + 1):
        page_url = f"{category_url}/page-{p}"
        try:
            h = get_html(session, page_url)
            d = extract_next_data(h)
            pp = d.get("props", {}).get("pageProps", {})
            page_products = collect_products_from_page_props(pp)
            if not page_products:
                page_products = collect_products_fallback_from_html(h)
            all_products.update(page_products)
        except Exception:
            # 某一页失败不立刻中断，后面通过校验体现差异
            continue

    # 如果发现数量仍偏少，再用 teamCategoryList.data 兜底补一层
    data_items = category.get("data") or []
    for it in data_items:
        alias = str(it.get("alias_title", "")).strip()
        pid = str(it.get("product_id", "")).strip()
        if alias and pid:
            all_products[pid] = {"alias": alias, "url": f"{BASE}/productdetail/{alias}/{pid}"}

    discovered = len(all_products)
    result["discovered_products"] = str(discovered)

    # 下载商品详情页
    def _task(item: Tuple[str, Dict[str, str]]) -> Tuple[int, int]:
        pid, meta = item
        alias = meta["alias"]
        url = meta["url"]
        fname = product_filename(alias, pid)
        fpath = category_folder / fname
        if fpath.exists():
            return 0, 0
        try:
            ph = get_html(session, url)
            fpath.write_text(ph, encoding="utf-8")
            return 1, 0
        except Exception:
            return 0, 1

    downloaded = 0
    failed = 0
    items = list(all_products.items())
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_task, it) for it in items]
        for fu in as_completed(futures):
            d, f = fu.result()
            downloaded += d
            failed += f

    # 只保留商品详情页 html（避免脏文件）
    expected_names = {
        product_filename(meta["alias"], pid) for pid, meta in all_products.items()
    }
    for f in category_folder.glob("*.html"):
        if f.name not in expected_names:
            f.unlink(missing_ok=True)

    remain = len(list(category_folder.glob("productdetail-*.html")))
    result["downloaded"] = str(downloaded)
    result["failed"] = str(failed)
    result["remain"] = str(remain)

    # 审计 note
    note = []
    if total > 0 and discovered != total:
        note.append(f"discovered({discovered})!=total({total})")
    if remain != discovered:
        note.append(f"remain({remain})!=discovered({discovered})")
    result["note"] = "; ".join(note)

    # 写每类目链接清单（便于复核）
    links_txt = category_folder / "links.txt"
    lines = [
        f"CATEGORY={category_title}",
        f"CATEGORY_ID={category_id}",
        f"CATEGORY_URL={category_url}",
        f"TOTAL={total}",
        f"DISCOVERED={discovered}",
        "",
    ]
    for pid, meta in sorted(all_products.items(), key=lambda x: int(x[0])):
        lines.append(meta["url"])
    links_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return result


def main() -> None:
    ensure_dir(OUTPUT_DIR)
    session = requests.Session()

    summary_rows: List[Dict[str, str]] = []

    # 1) 联赛页拿队伍
    club_html = get_html(session, CLUB_URL)
    teams = parse_teams_from_club_html(club_html)

    if not teams:
        raise RuntimeError("联赛页没有解析到任何队伍，请检查链接或源码结构")

    for team_name_raw, slug, team_id in teams:
        team_name = team_name_raw.title()
        team_folder = OUTPUT_DIR / sanitize_name(team_name)
        ensure_dir(team_folder)

        # 下载 team 页源码
        team_url = f"{BASE}/team/{slug}/{team_id}"
        try:
            team_html = get_html(session, team_url)
            team_html_path = team_folder / f"team-{slug}-{team_id}.html"
            team_html_path.write_text(team_html, encoding="utf-8")
            team_data = extract_next_data(team_html)
            page_props = team_data.get("props", {}).get("pageProps", {})
            categories = page_props.get("teamCategoryList") or []
        except Exception as e:
            summary_rows.append(
                {
                    "team": team_name,
                    "category": "-",
                    "mode": "TEAM_FAIL",
                    "category_id": "-",
                    "expected_total": "0",
                    "discovered_products": "0",
                    "downloaded": "0",
                    "failed": "0",
                    "remain": "0",
                    "note": str(e),
                }
            )
            continue

        if not categories:
            summary_rows.append(
                {
                    "team": team_name,
                    "category": "-",
                    "mode": "NO_CATEGORY",
                    "category_id": "-",
                    "expected_total": "0",
                    "discovered_products": "0",
                    "downloaded": "0",
                    "failed": "0",
                    "remain": "0",
                    "note": "teamCategoryList empty",
                }
            )
            continue

        # 2) 抓该队全部类目（不再只抓固定5类）
        for cat in categories:
            row = download_products_for_category(session, slug, team_id, cat, team_folder)
            row["team"] = team_name
            summary_rows.append(row)

    # 3) 输出汇总 CSV + txt
    csv_path = OUTPUT_DIR / "run-summary.csv"
    txt_path = OUTPUT_DIR / "run-summary.txt"

    fields = [
        "team",
        "category",
        "mode",
        "category_id",
        "expected_total",
        "discovered_products",
        "downloaded",
        "failed",
        "remain",
        "note",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in summary_rows:
            w.writerow(r)

    lines = ["|".join(fields)]
    for r in summary_rows:
        lines.append("|".join(str(r.get(k, "")) for k in fields))
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 控制台输出简报
    total_cat = len(summary_rows)
    total_remain = sum(int(r.get("remain", "0") or 0) for r in summary_rows)
    total_failed = sum(int(r.get("failed", "0") or 0) for r in summary_rows)
    print(f"完成：类目={total_cat} | 商品HTML总数={total_remain} | 下载失败={total_failed}")
    print(f"汇总文件：{csv_path} / {txt_path}")


if __name__ == "__main__":
    main()
