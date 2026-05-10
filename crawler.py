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
OUTPUT_DIR = Path("output")

HEADERS = {"User-Agent": "Mozilla/5.0"}
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>',
    re.IGNORECASE,
)

LEAGUES = [
    {
        "name": "Premier League",
        "club_url": "https://www.gogoalshop.se/club/Premier-League/645",
        "teams": {
            "Manchester United",
            "Liverpool",
            "Chelsea",
            "Manchester City",
            "Arsenal",
            "Tottenham Hotspur",
            "West Ham United",
            "Newcastle United",
            "Aston Villa",
        },
    },
    {
        "name": "La Liga",
        "club_url": "https://www.gogoalshop.se/club/La-Liga/646",
        "teams": {
            "Barcelona",
            "Real Madrid",
            "Atletico Madrid",
            "Valencia",
            "Sevilla",
            "Real Betis",
            "Athletic Club De Bilbao",
            "Celta Vigo",
            "Rcd Espanyol",
            "Rcd Mallorca",
            "Real Sociedad",
            "Real Zaragoza",
            "Malaga",
        },
    },
]

TARGET_CATEGORIES = [
    "Fan Jerseys",
    "Player Version Jerseys",
    "Retro Jerseys",
    "Kids Jerseys",
    "Women Jerseys",
]


def sanitize_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", s).strip()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def normalize_team_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("-", " ").strip()).title()


def get_html(session: requests.Session, url: str, retry: int = 7, timeout: int = 45) -> str:
    last_err = None
    for i in range(retry):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"GET failed: {url} | {last_err}")


def extract_next_data(html: str) -> dict:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("No __NEXT_DATA__ found")
    return json.loads(m.group(1))


def parse_teams_from_club_html(html: str, team_whitelist: set) -> List[Tuple[str, str, str]]:
    paths = sorted(set(re.findall(r'href="(/team/([A-Za-z0-9\\-]+)/([A-Za-z0-9]+))"', html)))
    teams = []
    for _, slug, team_id in paths:
        team_name = normalize_team_name(slug)
        teams.append((team_name, slug, team_id))

    uniq = {}
    for t in teams:
        uniq[(t[1], t[2])] = t
    teams = list(uniq.values())

    if team_whitelist:
        allow = {normalize_team_name(x) for x in team_whitelist}
        teams = [t for t in teams if normalize_team_name(t[0]) in allow]

    return teams


def product_filename(alias_title: str, product_id: str) -> str:
    return sanitize_name(f"productdetail-{alias_title}-{product_id}.html")


def build_product_map_from_items(items: List[dict]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for it in items or []:
        alias = str(it.get("alias_title", "")).strip()
        pid = str(it.get("product_id", "")).strip()
        if alias and pid:
            out[pid] = {
                "alias": alias,
                "url": f"{BASE}/productdetail/{alias}/{pid}",
            }
    return out


def collect_products_fallback_from_html(html: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    matches = re.findall(r'href="(/productdetail/([^"/]+(?:-[^"/]+)*)/([0-9]+))"', html)
    for path, alias, pid in matches:
        out[pid] = {"alias": alias, "url": f"{BASE}{path}"}
    return out


def download_one_product(session: requests.Session, url: str, path: Path) -> Tuple[int, int]:
    if path.exists():
        return 0, 0

    try:
        html = get_html(session, url)
        path.write_text(html, encoding="utf-8")
        return 1, 0
    except Exception:
        pass

    try:
        time.sleep(1.2)
        html = get_html(session, url)
        path.write_text(html, encoding="utf-8")
        return 1, 0
    except Exception:
        return 0, 1


def handle_category(
    session: requests.Session,
    league_name: str,
    team_name: str,
    team_slug: str,
    team_id: str,
    category: dict,
    team_folder: Path,
    retry_failures: List[Dict[str, str]],
) -> Dict[str, str]:
    category_title = str(category.get("title", "")).strip() or "Unknown"
    category_id = str(category.get("category_id", "")).strip()
    category_count = int(category.get("count", 0) or 0)

    result = {
        "league": league_name,
        "team": team_name,
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

    if category_id == "0":
        result["mode"] = "SKIP_CATEGORY_0"
        result["note"] = "skip category_id=0"
        return result

    category_folder = team_folder / sanitize_name(f"{team_name} {category_title}")
    ensure_dir(category_folder)

    category_path = f"/team/{team_slug}/{team_id}/category-{category_id}"
    category_url = f"{BASE}{category_path}"

    # 第一步：先抓球队页里的 data
    discovered: Dict[str, Dict[str, str]] = {}
    discovered.update(build_product_map_from_items(category.get("data") or []))

    first_html = ""
    first_pp = {}
    total = category_count
    limit = 20
    pages = 1

    # 第二步：再尝试进入完整类目页
    try:
        first_html = get_html(session, category_url)
        first_data = extract_next_data(first_html)
        first_pp = first_data.get("props", {}).get("pageProps", {})
        page_param = first_pp.get("pageParam", {}) or {}

        total = int(page_param.get("total", category_count) or category_count or 0)
        limit = int(page_param.get("limit", 20) or 20)
        pages = max(1, int(math.ceil(total / float(limit)))) if total > 0 else 1

        discovered.update(build_product_map_from_items(first_pp.get("productList") or []))

        if not (first_pp.get("productList") or []):
            discovered.update(collect_products_fallback_from_html(first_html))

        for p in range(2, pages + 1):
            page_url = f"{category_url}/page-{p}"
            try:
                h = get_html(session, page_url)
                d = extract_next_data(h)
                pp = d.get("props", {}).get("pageProps", {})
                page_items = pp.get("productList") or []
                page_map = build_product_map_from_items(page_items)

                if not page_map:
                    page_map = collect_products_fallback_from_html(h)

                discovered.update(page_map)
            except Exception as e:
                retry_failures.append(
                    {
                        "league": league_name,
                        "team": team_name,
                        "category": category_title,
                        "url": page_url,
                        "reason": f"page fetch failed: {e}",
                    }
                )
    except Exception as e:
        # 完整页打不开，也不放弃，至少保留球队页 data
        if not discovered:
            result["mode"] = "CATEGORY_FAIL"
            result["note"] = str(e)
            return result

    result["discovered_products"] = str(len(discovered))

    downloaded = 0
    failed = 0
    items = list(discovered.items())

    def _task(item):
        pid, meta = item
        alias = meta["alias"]
        url = meta["url"]
        fname = product_filename(alias, pid)
        fpath = category_folder / fname
        d, f = download_one_product(session, url, fpath)
        if f:
            retry_failures.append(
                {
                    "league": league_name,
                    "team": team_name,
                    "category": category_title,
                    "url": url,
                    "reason": "product download failed after retry",
                }
            )
        return d, f

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(_task, it) for it in items]
        for fu in as_completed(futures):
            d, f = fu.result()
            downloaded += d
            failed += f

    expected_names = {
        product_filename(meta["alias"], pid) for pid, meta in discovered.items()
    }
    for f in category_folder.glob("*.html"):
        if f.name not in expected_names:
            f.unlink(missing_ok=True)

    # 不再保留 links.txt 作为主结果
    for txt in category_folder.glob("links.txt"):
        txt.unlink(missing_ok=True)

    remain = len(list(category_folder.glob("productdetail-*.html")))
    result["downloaded"] = str(downloaded)
    result["failed"] = str(failed)
    result["remain"] = str(remain)

    note_parts = []
    if total and len(discovered) != total:
        note_parts.append(f"discovered({len(discovered)})!=total({total})")
    if remain != len(discovered):
        note_parts.append(f"remain({remain})!=discovered({len(discovered)})")
    result["note"] = "; ".join(note_parts)

    return result


def main() -> None:
    ensure_dir(OUTPUT_DIR)
    session = requests.Session()

    summary_rows: List[Dict[str, str]] = []
    missing_rows: List[Dict[str, str]] = []
    retry_failures: List[Dict[str, str]] = []

    for league in LEAGUES:
        league_name = league["name"]
        club_url = league["club_url"]
        team_whitelist = league["teams"]

        league_folder = OUTPUT_DIR / sanitize_name(league_name)
        ensure_dir(league_folder)

        club_html = get_html(session, club_url)
        club_path_parts = club_url.rstrip("/").split("/")
        club_file_name = club_path_parts[-2] + "-" + club_path_parts[-1] + ".html"
        (league_folder / club_file_name).write_text(club_html, encoding="utf-8")

        teams = parse_teams_from_club_html(club_html, team_whitelist)

        if not teams:
            summary_rows.append(
                {
                    "league": league_name,
                    "team": "-",
                    "category": "-",
                    "mode": "NO_TEAMS",
                    "category_id": "-",
                    "expected_total": "0",
                    "discovered_products": "0",
                    "downloaded": "0",
                    "failed": "0",
                    "remain": "0",
                    "note": "No teams parsed from club page",
                }
            )
            continue

        for team_name, slug, team_id in teams:
            team_folder = league_folder / sanitize_name(team_name)
            ensure_dir(team_folder)

            team_url = f"{BASE}/team/{slug}/{team_id}"

            try:
                team_html = get_html(session, team_url)
                team_file = team_folder / f"team-{slug}-{team_id}.html"
                team_file.write_text(team_html, encoding="utf-8")

                team_data = extract_next_data(team_html)
                page_props = team_data.get("props", {}).get("pageProps", {})
                categories = page_props.get("teamCategoryList") or []
            except Exception as e:
                summary_rows.append(
                    {
                        "league": league_name,
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

            category_map = {}
            for c in categories:
                title = str(c.get("title", "")).strip()
                if title:
                    category_map[title] = c

            for wanted in TARGET_CATEGORIES:
                if wanted not in category_map:
                    missing_rows.append(
                        {
                            "league": league_name,
                            "team": team_name,
                            "category": wanted,
                            "reason": "category not found in teamCategoryList",
                        }
                    )
                    summary_rows.append(
                        {
                            "league": league_name,
                            "team": team_name,
                            "category": wanted,
                            "mode": "MISSING_CATEGORY",
                            "category_id": "-",
                            "expected_total": "0",
                            "discovered_products": "0",
                            "downloaded": "0",
                            "failed": "0",
                            "remain": "0",
                            "note": "category not found in teamCategoryList",
                        }
                    )
                    continue

                row = handle_category(
                    session=session,
                    league_name=league_name,
                    team_name=team_name,
                    team_slug=slug,
                    team_id=team_id,
                    category=category_map[wanted],
                    team_folder=team_folder,
                    retry_failures=retry_failures,
                )
                summary_rows.append(row)

    summary_csv = OUTPUT_DIR / "run-summary.csv"
    summary_txt = OUTPUT_DIR / "run-summary.txt"
    missing_csv = OUTPUT_DIR / "missing-categories.csv"
    retry_csv = OUTPUT_DIR / "retry-failures.csv"

    fields = [
        "league",
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

    with summary_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in summary_rows:
            w.writerow(r)

    lines = ["|".join(fields)]
    for r in summary_rows:
        lines.append("|".join(str(r.get(k, "")) for k in fields))
    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with missing_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["league", "team", "category", "reason"])
        w.writeheader()
        for r in missing_rows:
            w.writerow(r)

    with retry_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["league", "team", "category", "url", "reason"])
        w.writeheader()
        for r in retry_failures:
            w.writerow(r)

    total_remain = sum(int(r.get("remain", "0") or 0) for r in summary_rows)
    total_failed = sum(int(r.get("failed", "0") or 0) for r in summary_rows)
    print(f"完成：联赛={len(LEAGUES)} | 类目行={len(summary_rows)} | 商品HTML总数={total_remain} | 下载失败={total_failed}")
    print(f"输出目录：{OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
