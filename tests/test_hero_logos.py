"""top_companies_with_logos: the row of logos on /hero."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from aggregates import top_companies_with_logos  # noqa: E402


def _db(with_names: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (company_domain TEXT, closed_at TEXT, confidence TEXT, posted_at TEXT)")
    name_col = ", company_name TEXT" if with_names else ""
    conn.execute(f"CREATE TABLE companies (domain TEXT, logo_url TEXT{name_col})")
    return conn


def _jobs(conn, domain: str, n: int, **kw) -> None:
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [(domain, kw.get("closed_at"), kw.get("confidence", "verified"), None)] * n,
    )


def _company(conn, domain: str, logo: str | None, name: str | None = None) -> None:
    if name is not None:
        conn.execute("INSERT INTO companies VALUES (?, ?, ?)", (domain, logo, name))
    else:
        conn.execute("INSERT INTO companies (domain, logo_url) VALUES (?, ?)", (domain, logo))


def test_busiest_first_and_only_with_a_logo():
    conn = _db()
    _jobs(conn, "big.com", 50)
    _jobs(conn, "nologo.com", 40)
    _jobs(conn, "mid.com", 20)
    _company(conn, "big.com", "https://big.com/logo.png", "Big")
    _company(conn, "nologo.com", None, "No Logo")
    _company(conn, "mid.com", "https://mid.com/logo.png", "Mid")
    rows = top_companies_with_logos(conn)
    assert [r["domain"] for r in rows] == ["big.com", "mid.com"]
    assert rows[0] == {"domain": "big.com", "name": "Big", "n": 50, "logo_url": "https://big.com/logo.png"}


def test_closed_and_unverified_jobs_do_not_count():
    conn = _db()
    _jobs(conn, "a.com", 5)
    _jobs(conn, "b.com", 3)
    _jobs(conn, "b.com", 10, closed_at="2026-09-01")
    _jobs(conn, "b.com", 10, confidence="best_effort")
    _company(conn, "a.com", "https://a.com/l.png", "A")
    _company(conn, "b.com", "https://b.com/l.png", "B")
    assert [r["domain"] for r in top_companies_with_logos(conn)] == ["a.com", "b.com"]


def test_one_image_shows_once_and_the_limit_holds():
    conn = _db()
    _jobs(conn, "amazon.com", 30)
    _jobs(conn, "amazon.co.uk", 20)
    _company(conn, "amazon.com", "https://amazon.com/favicon.ico", "Amazon")
    _company(conn, "amazon.co.uk", "https://amazon.com/favicon.ico", "Amazon UK")
    for i in range(40):
        _jobs(conn, f"c{i}.com", 10 - i // 10)
        _company(conn, f"c{i}.com", f"https://c{i}.com/l.png", f"C{i}")
    rows = top_companies_with_logos(conn, limit=30)
    assert len(rows) == 30
    assert [r["domain"] for r in rows].count("amazon.co.uk") == 0
    assert rows[0]["domain"] == "amazon.com"


def test_snapshot_without_company_names():
    conn = _db(with_names=False)
    _jobs(conn, "a.com", 2)
    _company(conn, "a.com", "https://a.com/l.png")
    assert top_companies_with_logos(conn) == [
        {"domain": "a.com", "name": None, "n": 2, "logo_url": "https://a.com/l.png"}]


def test_snapshot_without_a_logo_column_returns_nothing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (company_domain TEXT, closed_at TEXT, confidence TEXT, posted_at TEXT)")
    conn.execute("CREATE TABLE companies (domain TEXT)")
    _jobs(conn, "a.com", 2)
    assert top_companies_with_logos(conn) == []
