import json
import math
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


BASE = "https://www.gogoalshop.se"
CLUB_URL = "https://www.gogoalshop.se/club/Premier-League/645"
OUTPUT = Path("output")

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

TARGET_CATEGORIES = [
    "Fan Jerseys",
    "Player Version Jerseys",
    "Retro Jerseys",
    "Kids Jerseys",
    "Women Jerseys",
]

HEADERS = {"User-Agent": "Mozilla/5.0"}
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>')


def get_html(session, url, retry=6):
    last = None
    for i in range(retry):
        try:
            r = session.get(url, headers=HEADERS, timeout=40)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败: {url} | {last}")


def get_next_data(html):
    m = NEXT_RE.search(html)
    if not m:
        raise RuntimeError("未找到 __NEXT_DATA__")
    return json.loads(m.group(1))


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "-", s).strip()


def product_file(alias, pid):
    return f"productdetail-{safe_name(alias)}-{pid}.html"


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def parse_teams_from_club(html):
    links = sorted(set(re.findall(r'href="(/team/[^"#\s]+)"', html)))
    teams = []
    for p in links:
        parts = p.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "team":
            slug = parts[1]
            tid = parts[2]
            team_name = slug.replace("-", " ")
            teams.append((team_name, slug, tid))
    # 去重
    uniq = {}
    for t in teams:
        uniq[(t[1], t[2])] = t
    return list(uniq.values())


def clean_non_expected_html(folder: Path, expected_names):
    for f in folder.glob("*.html"):
        if f.name not in expected_names:
            f.unlink(missing_ok=True)


def main():
    ensure_dir(OUTPUT)
    summary = ["TEAM|CATEGORY|MODE|TOTAL|DOWNLOADED|FAILED|REMAIN|NOTE"]

    session = requests.Session()

    # 1) 联赛页 -> 球队列表
    club_html = get_html(session, CLUB_URL)
    teams = parse_teams_from_club(club_html)
    teams = [t for t in teams if t[0].title() in TEAM_WHITELIST]

    for team_name_raw, slug, tid in teams:
        team_name = team_name_raw.title()
        team_folder = OUTPUT / safe_name(team_name)
        ensure_dir(team_folder)

        team_url = f"{BASE}/team/{slug}/{tid}"
        try:
            team_html = get_html(session, team_url)
            (team_folder / f"team-{slug}-{tid}.html").write_text(team_html, encoding="utf-8")
            team_data = get_next_data(team_html)
            page_props = team_data["props"]["pageProps"]
            cat_list = page_props.get("teamCategoryList", [])
        except Exception as e:
            summary.append(f"{team_name}|-|TEAM_FAIL|0|0|0|0|{e}")
            continue

        cat_map = {}
        for c in cat_list:
            title = c.get("title")
            if title:
                cat_map[title] = c

        for cat_title in TARGET_CATEGORIES:
            folder = team_folder / safe_name(f"{team_name} {cat_title}")
            ensure_dir(folder)

            cat = cat_map.get(cat_title)
            if not cat:
                (folder / "links.txt").write_text(
                    f"TEAM={team_name}\nCATEGORY={cat_title}\nSTATUS=NOT_FOUND\n",
                    encoding="utf-8"
                )
                for f in folder.glob("*.html"):
                    f.unlink(missing_ok=True)
                summary.append(f"{team_name}|{cat_title}|NOT_FOUND|0|0|0|0|")
                continue

            cat_id = str(cat.get("category_id", ""))
            count = int(cat.get("count", 0))
            cat_path = f"/team/{slug}/{tid}/category-{cat_id}"

            # 无 More：只写 links.txt
            if count <= 4:
                lines = [
                    f"TEAM={team_name}",
                    f"CATEGORY={cat_title}",
                    "MODE=NO_MORE",
                    f"CATEGORY_URL={BASE}{cat_path}",
                ]
                for item in (cat.get("data") or []):
                    alias = str(item.get("alias_title", "")).strip()
                    pid = str(item.get("product_id", "")).strip()
                    if alias and pid:
                        lines.append(f"{BASE}/productdetail/{alias}/{pid}")
                (folder / "links.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
                for f in folder.glob("*.html"):
                    f.unlink(missing_ok=True)
                summary.append(f"{team_name}|{cat_title}|NO_MORE|{count}|0|0|0|")
                continue

            # 有 More：抓全量商品详情页
            try:
                first_html = get_html(session, f"{BASE}{cat_path}")
                first_data = get_next_data(first_html)
                pp = first_data["props"]["pageProps"]
                page_param = pp.get("pageParam", {})
                total = int(page_param.get("total", 0))
                limit = int(page_param.get("limit", 20)) or 20
                pages = int(math.ceil(total / float(limit)))

                expected = {}  # filename -> url
                for p in range(1, pages + 1):
                    if p == 1:
                        obj = first_data
                    else:
                        h = get_html(session, f"{BASE}{cat_path}/page-{p}")
                        obj = get_next_data(h)

                    plist = obj["props"]["pageProps"].get("productList", [])
                    for it in plist:
                        alias = str(it.get("alias_title", "")).strip()
                        pid = str(it.get("product_id", "")).strip()
                        if not alias or not pid:
                            continue
                        fn = product_file(alias, pid)
                        expected[fn] = f"{BASE}/productdetail/{alias}/{pid}"

                def job(one):
                    fn, url = one
                    fp = folder / fn
                    if fp.exists():
                        return (1, 0)
                    html = get_html(session, url)
                    fp.write_text(html, encoding="utf-8")
                    return (1, 0)

                downloaded = 0
                failed = 0
                with ThreadPoolExecutor(max_workers=10) as ex:
                    futures = [ex.submit(job, one) for one in expected.items()]
                    for fu in as_completed(futures):
                        try:
                            d, f = fu.result()
                            downloaded += d
                            failed += f
                        except Exception:
                            failed += 1

                clean_non_expected_html(folder, set(expected.keys()))
                (folder / "links.txt").unlink(missing_ok=True)
                remain = len(list(folder.glob("*.html")))
                summary.append(f"{team_name}|{cat_title}|MORE|{total}|{downloaded}|{failed}|{remain}|")
            except Exception as e:
                summary.append(f"{team_name}|{cat_title}|MORE_FAIL|0|0|0|0|{e}")

    (OUTPUT / "run-summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("完成，输出在 output 目录。")


if __name__ == "__main__":
    main()
