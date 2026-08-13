import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, flash, g, redirect, render_template, request, send_file, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "library.db"
JSON_PATH = BASE_DIR / "library.json"
SYNC_STATE_PATH = BASE_DIR / ".sync_state.json"
CONFIG_PATH = BASE_DIR / "config.json"

app = Flask(__name__)
app.secret_key = "archivio-dev-secret"
app.config["JSON_SORT_KEYS"] = False


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except json.JSONDecodeError:
        return {}


def resolve_json_path(raw_path: str | None = None) -> Path:
    config = load_config()
    value = (raw_path or config.get("json_file") or "library.json").strip()
    if not value:
        value = "library.json"
    path = Path(value)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()

    if path.exists() and path.is_dir():
        return (path / "library.json").resolve()
    if path.suffix == "" and not path.exists():
        candidate = path.with_name(f"{path.name or 'library'}.json")
        if candidate.parent == path.parent:
            return candidate
    return path


def refresh_runtime_paths() -> Path:
    global JSON_PATH
    JSON_PATH = resolve_json_path()
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    return JSON_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        default_config = {
            "api_providers": [
                "国会図書館",
                "OpenBD",
                "楽天ブックス",
                "Discogs",
                "Amazon",
            ],
            "json_file": "library.json",
            "rakuten_app_id": "",
            "rakuten_access_key": "",
        }
        CONFIG_PATH.write_text(json.dumps(default_config, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        config = load_config()
        config.setdefault("json_file", "library.json")
        config.setdefault("api_providers", ["国会図書館", "OpenBD", "楽天ブックス", "Discogs", "Amazon"])
        config.setdefault("rakuten_app_id", "")
        config.setdefault("rakuten_access_key", "")
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def get_rakuten_app_id() -> str:
    config = load_config()
    value = str(config.get("rakuten_app_id") or os.getenv("RAKUTEN_APP_ID") or "").strip()
    return value


def get_rakuten_access_key() -> str:
    config = load_config()
    value = str(config.get("rakuten_access_key") or os.getenv("RAKUTEN_ACCESS_KEY") or "").strip()
    return value


def ensure_library_json() -> None:
    refresh_runtime_paths()
    if JSON_PATH.exists() and JSON_PATH.is_dir():
        raise ValueError(f"library.json の保存先がフォルダです。ファイルパスを指定してください: {JSON_PATH}")
    if JSON_PATH.exists():
        return
    payload = {"works": []}
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_sync_state(file_hash: str, modified_at: float) -> None:
    SYNC_STATE_PATH.write_text(
        json.dumps({"hash": file_hash, "mtime": modified_at}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cleanup_orphaned_records(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM work_tags WHERE work_id NOT IN (SELECT id FROM works)")
    conn.execute("DELETE FROM work_series WHERE work_id NOT IN (SELECT id FROM works)")
    conn.execute("DELETE FROM work_people WHERE work_id NOT IN (SELECT id FROM works)")
    conn.execute("DELETE FROM versions WHERE work_id NOT IN (SELECT id FROM works)")
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM work_tags)")
    conn.execute("DELETE FROM series WHERE id NOT IN (SELECT series_id FROM work_series)")
    conn.execute("DELETE FROM people WHERE id NOT IN (SELECT person_id FROM work_people)")
    conn.commit()


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS works (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            reading TEXT,
            evaluation INTEGER,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS versions (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            title TEXT,
            media_type TEXT,
            format_type TEXT,
            volume TEXT,
            publisher TEXT,
            purchase_source TEXT,
            location TEXT,
            status TEXT,
            platform TEXT,
            release_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS series (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            sort_order REAL
        );

        CREATE TABLE IF NOT EXISTS people (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS work_tags (
            work_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            PRIMARY KEY (work_id, tag_id),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS work_series (
            work_id TEXT NOT NULL,
            series_id TEXT NOT NULL,
            sort_order REAL,
            PRIMARY KEY (work_id, series_id),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS work_people (
            work_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            role TEXT,
            display_order INTEGER,
            PRIMARY KEY (work_id, person_id, role),
            FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


def split_csv(value: str) -> list[str]:
    if not value:
        return []
    parts = value.replace("、", ",").split(",")
    return [item.strip() for item in parts if item.strip()]


def parse_people_text(value: str) -> list[tuple[str, str]]:
    if not value:
        return []
    results = []
    for segment in value.replace("\r", "").splitlines():
        item = segment.strip()
        if not item:
            continue
        if "|" in item:
            name, role = item.split("|", 1)
        elif ":" in item:
            name, role = item.split(":", 1)
        else:
            name, role = item, ""
        results.append((name.strip(), role.strip()))
    return results


def validate_work_form_data(form_data: dict) -> dict:
    title = (form_data.get("title") or "").strip()
    if not title:
        raise ValueError("タイトルは必須です。")

    media_types = {"書籍", "マンガ", "雑誌", "CD", "DVD", "Blu-ray", "4K UHD"}
    media_type = (form_data.get("media_type") or "書籍").strip()
    if media_type not in media_types:
        raise ValueError("メディア種別が不正です。")

    status_values = {"未読", "読書中", "読了"}
    status = (form_data.get("status") or "未読").strip()
    if status not in status_values:
        raise ValueError("状態が不正です。")

    evaluation_raw = form_data.get("evaluation")
    evaluation = None
    if evaluation_raw not in (None, ""):
        try:
            evaluation = int(evaluation_raw)
        except (TypeError, ValueError):
            raise ValueError("評価は 1〜5 の整数で入力してください。")
        if evaluation < 1 or evaluation > 5:
            raise ValueError("評価は 1〜5 の整数で入力してください。")

    def as_text_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.replace("\r", "").splitlines() if line.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    authors = []
    if media_type == "書籍":
        authors = as_text_list(form_data.get("authors"))

    people_value = form_data.get("people") or ""
    if isinstance(people_value, (list, tuple, set)):
        people = []
        for item in people_value:
            if isinstance(item, tuple) and len(item) == 2:
                people.append((str(item[0]).strip(), str(item[1]).strip()))
            elif isinstance(item, str):
                people.extend(parse_people_text(item))
    else:
        people = parse_people_text(str(people_value))

    tags_value = form_data.get("tags") or ""
    if isinstance(tags_value, (list, tuple, set)):
        tag_text = ", ".join(str(item) for item in tags_value if str(item).strip())
    else:
        tag_text = str(tags_value)

    series_value = form_data.get("series") or ""
    if isinstance(series_value, (list, tuple, set)):
        series_text = ", ".join(str(item) for item in series_value if str(item).strip())
    else:
        series_text = str(series_value)

    payload = {
        "title": title,
        "reading": (form_data.get("reading") or "").strip(),
        "evaluation": evaluation,
        "notes": (form_data.get("notes") or "").strip(),
        "version_title": (form_data.get("version_title") or title).strip() or title,
        "media_type": media_type,
        "format_type": (form_data.get("format_type") or "").strip(),
        "volume": (form_data.get("volume") or "").strip(),
        "publisher": (form_data.get("publisher") or "").strip(),
        "purchase_source": (form_data.get("purchase_source") or "").strip(),
        "location": (form_data.get("location") or "").strip(),
        "status": status,
        "platform": (form_data.get("platform") or "").strip(),
        "tags": split_csv(tag_text),
        "series": split_csv(series_text),
        "people": people,
        "authors": authors,
    }
    return payload


def normalize_search_identifier(identifier: str) -> str:
    if not identifier:
        return ""
    text = str(identifier).strip().upper()
    digits = re.sub(r"[^0-9X]", "", text)
    if not digits:
        return ""
    if len(digits) in {8, 10, 13}:
        return digits
    return digits


def parse_bulk_identifier_text(raw: str) -> list[str]:
    if raw is None:
        return []
    text = str(raw).replace("、", ",").replace("；", ";")
    seen: set[str] = set()
    results: list[str] = []
    for segment in re.split(r"[\n,;\r]+", text):
        for token in re.split(r"\s+", segment.strip()):
            normalized = normalize_search_identifier(token)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            results.append(normalized)
    return results


def build_bulk_candidate(identifier: str, candidate: dict | None = None) -> dict:
    base = candidate or {}
    title = (base.get("title") or f"未確認候補 ({identifier})").strip()
    media_type = (base.get("media_type") or "書籍").strip()
    return {
        "identifier": normalize_search_identifier(identifier) or identifier,
        "provider": base.get("provider") or "手動候補",
        "title": title,
        "reading": (base.get("reading") or "").strip(),
        "media_type": media_type,
        "format_type": (base.get("format_type") or "").strip(),
        "volume": "",
        "publisher": (base.get("publisher") or "").strip(),
        "purchase_source": (base.get("purchase_source") or "").strip(),
        "location": "",
        "status": "未読",
        "platform": "",
        "version_title": title,
        "authors": [str(item).strip() for item in (base.get("authors") or []) if str(item).strip()],
        "series": [str(item).strip() for item in (base.get("series") or []) if str(item).strip()],
        "tags": [str(item).strip() for item in (base.get("tags") or []) if str(item).strip()],
        "people": [],
        "notes": "",
        "evaluation": None,
    }


def save_work_payload(conn: sqlite3.Connection, payload: dict) -> str:
    work_id = str(uuid.uuid4())
    title = payload["title"]
    reading = payload["reading"]
    evaluation = payload["evaluation"]
    notes = payload["notes"]
    version_title = payload["version_title"]
    media_type = payload["media_type"]
    format_type = payload["format_type"]
    volume = payload["volume"]
    publisher = payload["publisher"]
    purchase_source = payload["purchase_source"]
    location = payload["location"]
    status = payload["status"]
    platform = payload["platform"]
    tags = payload["tags"]
    series = payload["series"]
    people = payload["people"]
    authors = payload["authors"]
    now = utc_now()

    conn.execute(
        """
        INSERT INTO works (id, title, reading, evaluation, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (work_id, title, reading, evaluation, notes, now, now),
    )
    conn.execute(
        """
        INSERT INTO versions (
            id, work_id, title, media_type, format_type, volume,
            publisher, purchase_source, location, status, platform,
            release_date, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            work_id,
            version_title,
            media_type,
            format_type,
            volume,
            publisher,
            purchase_source,
            location,
            status,
            platform,
            "",
            now,
            now,
        ),
    )
    sync_work_metadata(conn, work_id, tags, series, people, authors)
    return work_id


def infer_media_type_from_identifier(identifier: str) -> str:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return "書籍"

    if normalized.startswith("978"):
        return "書籍"

    if len(normalized) == 13:
        if normalized.startswith("454"):
            return "Blu-ray"
        if normalized.startswith("4988"):
            return "DVD"
        if normalized.startswith(("457", "490", "494")):
            return "CD"
        if normalized.startswith("498"):
            return "CD"
        return "CD"

    return "書籍"


def resolve_media_type_for_lookup(identifier: str, preferred_media_type: str | None = None) -> str:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return "書籍"

    selected = (preferred_media_type or "").strip()
    if selected == "CD":
        return "CD"
    if selected == "映像":
        if normalized.startswith("454"):
            return "Blu-ray"
        return "DVD"

    if normalized.startswith("978"):
        return "書籍"
    if len(normalized) == 13:
        if normalized.startswith("454"):
            return "Blu-ray"
        if normalized.startswith("4988"):
            return "DVD"
        if normalized.startswith(("457", "490", "494")):
            return "CD"
        if normalized.startswith("498"):
            return "CD"
        return "CD"
    return "書籍"


def normalize_openbd_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("content", "text", "value", "name", "Title", "PersonName", "CorporateName", "PublisherName", "SubjectCode"):
            if key in value:
                normalized = normalize_openbd_text(value[key])
                if normalized:
                    return normalized
        for nested in value.values():
            normalized = normalize_openbd_text(nested)
            if normalized:
                return normalized
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            normalized = normalize_openbd_text(item)
            if normalized:
                parts.append(normalized)
        return ", ".join(parts)
    return str(value).strip()


def fetch_rakuten_cd_candidate(identifier: str, preferred_media_type: str | None = None) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return None

    app_id = get_rakuten_app_id()
    access_key = get_rakuten_access_key()
    if not app_id or not access_key:
        return None

    media_type = "CD"
    try:
        response = requests.get(
            "https://openapi.rakuten.co.jp/services/api/BooksCD/Search/20170404",
            params={
                "applicationId": app_id,
                "accessKey": access_key,
                "format": "json",
                "jan": normalized,
                "hits": 1,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    items = payload.get("Items") or []
    if not items:
        return None

    item = items[0].get("Item") or {}
    title = normalize_openbd_text(item.get("title") or item.get("itemName") or "未確認候補")
    title_kana = normalize_openbd_text(item.get("titleKana") or "")
    artist_name = normalize_openbd_text(item.get("artistName") or item.get("author") or "")
    publisher = normalize_openbd_text(item.get("label") or item.get("publisherName") or "")
    authors = [artist_name] if artist_name else []

    return {
        "provider": "楽天ブックス",
        "identifier": normalized,
        "title": title,
        "reading": title_kana,
        "media_type": media_type,
        "format_type": "",
        "publisher": publisher,
        "purchase_source": "Rakuten Books",
        "authors": authors,
        "series": [],
        "tags": [media_type],
    }


def fetch_rakuten_video_candidate(identifier: str, preferred_media_type: str | None = None) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return None

    app_id = get_rakuten_app_id()
    access_key = get_rakuten_access_key()
    if not app_id or not access_key:
        return None

    selected = (preferred_media_type or "").strip()
    if selected == "映像":
        media_type = "Blu-ray" if normalized.startswith("454") else "DVD"
    else:
        media_type = "Blu-ray" if normalized.startswith("454") else "DVD"

    try:
        response = requests.get(
            "https://openapi.rakuten.co.jp/services/api/BooksDVD/Search/20170404",
            params={
                "applicationId": app_id,
                "accessKey": access_key,
                "format": "json",
                "jan": normalized,
                "hits": 1,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    items = payload.get("Items") or []
    if not items:
        return None

    item = items[0].get("Item") or {}
    title = normalize_openbd_text(item.get("title") or item.get("itemName") or "未確認候補")
    title_kana = normalize_openbd_text(item.get("titleKana") or "")
    artist_name = normalize_openbd_text(item.get("artistName") or item.get("author") or "")
    publisher = normalize_openbd_text(item.get("label") or item.get("publisherName") or "")
    authors = [artist_name] if artist_name else []

    return {
        "provider": "楽天ブックス",
        "identifier": normalized,
        "title": title,
        "reading": title_kana,
        "media_type": media_type,
        "format_type": "",
        "publisher": publisher,
        "purchase_source": "Rakuten Books",
        "authors": authors,
        "series": [],
        "tags": [media_type],
    }


def fetch_openbd_candidate(identifier: str) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return None

    try:
        response = requests.get("https://api.openbd.jp/v1/get", params={"isbn": normalized}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, list) or not payload:
        return None

    item = payload[0]
    if not isinstance(item, dict):
        return None

    summary = item.get("summary") or {}
    onix = item.get("onix") or {}
    descriptive_detail = onix.get("DescriptiveDetail") or {}
    publishing_detail = onix.get("PublishingDetail") or {}
    contributors = descriptive_detail.get("Contributor") or []
    subjects = descriptive_detail.get("Subject") or []
    publishers = publishing_detail.get("Publisher") or []

    authors = []
    for contributor in contributors:
        if isinstance(contributor, dict):
            name = contributor.get("PersonName") or contributor.get("CorporateName") or ""
            normalized_name = normalize_openbd_text(name)
            if normalized_name and normalized_name not in authors:
                authors.append(normalized_name)

    tags = []
    for subject in subjects:
        if isinstance(subject, dict):
            code = subject.get("SubjectCode") or subject.get("SubjectHeadingText") or ""
            normalized_code = normalize_openbd_text(code)
            if normalized_code and normalized_code not in tags:
                tags.append(normalized_code)

    publisher_name = ""
    for publisher in publishers:
        if isinstance(publisher, dict):
            name = publisher.get("PublisherName") or ""
            normalized_name = normalize_openbd_text(name)
            if normalized_name:
                publisher_name = normalized_name
                break

    title = normalize_openbd_text(summary.get("title") or "未確認候補")
    title_kana = normalize_openbd_text(summary.get("titleKana") or "")
    return {
        "provider": "OpenBD",
        "identifier": normalized,
        "title": title,
        "reading": title_kana,
        "media_type": infer_media_type_from_identifier(normalized),
        "format_type": "",
        "publisher": publisher_name or summary.get("publisher") or "",
        "purchase_source": "OpenBD",
        "authors": authors or [],
        "series": [],
        "tags": tags,
    }


def fetch_discogs_candidate(identifier: str, preferred_media_type: str | None = None) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized or len(normalized) != 13:
        return None

    try:
        response = requests.get(
            "https://api.discogs.com/database/search",
            params={"barcode": normalized, "type": "release", "per_page": 1},
            headers={"User-Agent": "Archivio/1.0 (contact@example.com)"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    results = payload.get("results") or []
    if not results:
        return None

    item = results[0]
    title = normalize_openbd_text(item.get("title") or "未確認候補")
    artists = [normalize_openbd_text(artist) for artist in (item.get("artist") or []) if normalize_openbd_text(artist)]
    if isinstance(item.get("artist"), str):
        artists = [normalize_openbd_text(item.get("artist"))] if normalize_openbd_text(item.get("artist")) else []
    label = normalize_openbd_text(item.get("label") or "")
    media_type = resolve_media_type_for_lookup(normalized, preferred_media_type)
    if media_type == "書籍":
        media_type = "CD"

    return {
        "provider": "Discogs",
        "identifier": normalized,
        "title": title,
        "reading": "",
        "media_type": media_type,
        "format_type": "",
        "publisher": label,
        "purchase_source": "Discogs",
        "authors": artists,
        "series": [],
        "tags": [],
    }


def fetch_google_books_candidate(identifier: str) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return None

    try:
        response = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{normalized}", "maxResults": 1},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    items = payload.get("items") or []
    if not items:
        return None

    info = items[0].get("volumeInfo") or {}
    title = normalize_openbd_text(info.get("title") or "未確認候補")
    authors = [normalize_openbd_text(author) for author in (info.get("authors") or []) if normalize_openbd_text(author)]
    publisher = normalize_openbd_text(info.get("publisher") or "")
    published_date = normalize_openbd_text(info.get("publishedDate") or "")
    subtitle = normalize_openbd_text(info.get("subtitle") or "")
    if subtitle and title and subtitle not in title:
        title = f"{title}: {subtitle}"

    return {
        "provider": "Google Books",
        "identifier": normalized,
        "title": title,
        "reading": "",
        "media_type": infer_media_type_from_identifier(normalized),
        "format_type": "",
        "publisher": publisher,
        "purchase_source": "Google Books",
        "authors": authors,
        "series": [],
        "tags": [published_date] if published_date else [],
    }


def fetch_openlibrary_candidate(identifier: str) -> dict | None:
    normalized = normalize_search_identifier(identifier)
    if not normalized:
        return None

    try:
        response = requests.get(
            "https://openlibrary.org/isbn/" + normalized + ".json",
            timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    title = normalize_openbd_text(payload.get("title") or "未確認候補")
    subtitle = normalize_openbd_text(payload.get("subtitle") or "")
    if subtitle and title and subtitle not in title:
        title = f"{title}: {subtitle}"

    authors = []
    for author in payload.get("authors") or []:
        if isinstance(author, dict):
            key = author.get("key")
            if not key:
                continue
            try:
                author_response = requests.get(f"https://openlibrary.org{key}.json", timeout=10)
                author_response.raise_for_status()
                author_payload = author_response.json() or {}
            except Exception:
                author_payload = {}
            author_name = normalize_openbd_text(author_payload.get("name") or author.get("name") or "")
            if author_name and author_name not in authors:
                authors.append(author_name)

    publish_dates = payload.get("publish_date")
    publish_date = normalize_openbd_text(publish_dates if isinstance(publish_dates, str) else "")
    publishers = payload.get("publishers") or []
    publisher = normalize_openbd_text(publishers[0] if isinstance(publishers, list) and publishers else "")

    return {
        "provider": "OpenLibrary",
        "identifier": normalized,
        "title": title,
        "reading": "",
        "media_type": infer_media_type_from_identifier(normalized),
        "format_type": "",
        "publisher": publisher,
        "purchase_source": "OpenLibrary",
        "authors": authors,
        "series": [],
        "tags": [publish_date] if publish_date else [],
    }


def lookup_external_candidates(identifier: str, preferred_media_type: str | None = None) -> list[dict]:
    normalized = normalize_search_identifier(identifier)

    sample = {
        "9784041101101": {
            "title": "トップガン",
            "reading": "とっぷがん",
            "media_type": "DVD",
            "format_type": "劇場版",
            "publisher": "パラマウント",
            "purchase_source": "Amazon",
            "authors": ["トム・クルーズ"],
            "series": ["アクション", "90年代映画"],
            "tags": ["映画", "お気に入り"],
        },
        "9784091234567": {
            "title": "ドラえもん",
            "reading": "ドラえもん",
            "media_type": "書籍",
            "format_type": "単行本",
            "publisher": "小学館",
            "purchase_source": "DMMブックス",
            "authors": ["藤子・F・不二雄"],
            "series": ["児童書"],
            "tags": ["漫画", "家族"],
        },
        "B0000000001": {
            "title": "Top Gun",
            "reading": "とっぷがん",
            "media_type": "Blu-ray",
            "format_type": "特別版",
            "publisher": "Paramount",
            "purchase_source": "Amazon",
            "authors": ["Tom Cruise"],
            "series": ["Action"],
            "tags": ["Movie"],
        },
    }
    if not normalized:
        return []

    auto_media_type = (preferred_media_type or "").strip()
    if auto_media_type in ("", "自動"):
        inferred = resolve_media_type_for_lookup(normalized, preferred_media_type)
        if normalized.startswith("978"):
            pass
        elif inferred == "CD":
            rakuten_cd_candidate = fetch_rakuten_cd_candidate(normalized, "CD")
            if rakuten_cd_candidate:
                return [rakuten_cd_candidate]
            rakuten_video_candidate = fetch_rakuten_video_candidate(normalized, "映像")
            if rakuten_video_candidate:
                return [rakuten_video_candidate]
        elif inferred in {"DVD", "Blu-ray"}:
            rakuten_video_candidate = fetch_rakuten_video_candidate(normalized, "映像")
            if rakuten_video_candidate:
                return [rakuten_video_candidate]
            rakuten_cd_candidate = fetch_rakuten_cd_candidate(normalized, "CD")
            if rakuten_cd_candidate:
                return [rakuten_cd_candidate]

    if preferred_media_type == "CD":
        rakuten_cd_candidate = fetch_rakuten_cd_candidate(normalized, preferred_media_type)
        if rakuten_cd_candidate:
            return [rakuten_cd_candidate]
        rakuten_video_candidate = fetch_rakuten_video_candidate(normalized, "映像")
        if rakuten_video_candidate:
            return [rakuten_video_candidate]
        discogs_candidate = fetch_discogs_candidate(normalized, preferred_media_type)
        if discogs_candidate:
            return [discogs_candidate]

    if preferred_media_type == "映像":
        rakuten_candidate = fetch_rakuten_video_candidate(normalized, preferred_media_type)
        if rakuten_candidate:
            return [rakuten_candidate]
        rakuten_cd_candidate = fetch_rakuten_cd_candidate(normalized, "CD")
        if rakuten_cd_candidate:
            return [rakuten_cd_candidate]

    openbd_candidate = fetch_openbd_candidate(normalized)
    if openbd_candidate:
        return [openbd_candidate]

    discogs_candidate = fetch_discogs_candidate(normalized, preferred_media_type)
    if discogs_candidate:
        return [discogs_candidate]

    if len(normalized) == 13:
        return [{
            "provider": "手動候補",
            "identifier": normalized,
            "title": f"未確認候補 (JAN: {normalized})",
            "reading": "",
            "media_type": resolve_media_type_for_lookup(normalized, preferred_media_type),
            "format_type": "",
            "publisher": "",
            "purchase_source": "JANコード",
            "authors": [],
            "series": [],
            "tags": [],
        }]

    candidates = []
    seen = set()
    for fetcher in (fetch_google_books_candidate, fetch_openlibrary_candidate):
        candidate = fetcher(normalized)
        if not candidate:
            continue
        signature = f"{candidate['provider']}::{candidate['title']}::{candidate['publisher']}::{','.join(candidate['authors'])}"
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append(candidate)
    if candidates:
        return candidates

    base = sample.get(normalized)
    if not base:
        return [{
            "provider": "ローカル候補",
            "identifier": normalized,
            "title": "未確認候補",
            "reading": "",
            "media_type": "書籍",
            "format_type": "",
            "publisher": "",
            "purchase_source": "",
            "authors": [],
            "series": [],
            "tags": [],
        }]

    candidates = []
    for provider, title in [
        ("国会図書館", base["title"]),
        ("OpenBD", base["title"]),
        ("Amazon", base["title"]),
    ]:
        candidate = {
            "provider": provider,
            "identifier": normalized,
            "title": title,
            "reading": base["reading"],
            "media_type": base["media_type"],
            "format_type": base["format_type"],
            "publisher": base["publisher"],
            "purchase_source": base["purchase_source"],
            "authors": base["authors"],
            "series": base["series"],
            "tags": base["tags"],
        }
        candidates.append(candidate)
    return candidates


def upsert_tag(conn: sqlite3.Connection, tag_name: str) -> str:
    tag_name = tag_name.strip()
    if not tag_name:
        raise ValueError("空のタグ名は保存できません")
    existing = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    if existing:
        return existing["id"]
    tag_id = str(uuid.uuid4())
    conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
    return tag_id


def upsert_series(conn: sqlite3.Connection, series_name: str) -> str:
    series_name = series_name.strip()
    if not series_name:
        raise ValueError("空のシリーズ名は保存できません")
    existing = conn.execute("SELECT id FROM series WHERE name = ?", (series_name,)).fetchone()
    if existing:
        return existing["id"]
    series_id = str(uuid.uuid4())
    conn.execute("INSERT INTO series (id, name, sort_order) VALUES (?, ?, ?)", (series_id, series_name, 0))
    return series_id


def upsert_person(conn: sqlite3.Connection, person_name: str) -> str:
    person_name = person_name.strip()
    if not person_name:
        raise ValueError("空の人物名は保存できません")
    existing = conn.execute("SELECT id FROM people WHERE name = ?", (person_name,)).fetchone()
    if existing:
        return existing["id"]
    person_id = str(uuid.uuid4())
    conn.execute("INSERT INTO people (id, name) VALUES (?, ?)", (person_id, person_name))
    return person_id


def sync_work_metadata(conn: sqlite3.Connection, work_id: str, tag_names: list[str], series_names: list[str], people: list[tuple[str, str]], authors: list[str] | None = None) -> None:
    conn.execute("DELETE FROM work_tags WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM work_series WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM work_people WHERE work_id = ?", (work_id,))

    for tag_name in tag_names:
        tag_id = upsert_tag(conn, tag_name)
        conn.execute("INSERT OR IGNORE INTO work_tags (work_id, tag_id) VALUES (?, ?)", (work_id, tag_id))

    for index, series_name in enumerate(series_names):
        series_id = upsert_series(conn, series_name)
        conn.execute(
            "INSERT OR IGNORE INTO work_series (work_id, series_id, sort_order) VALUES (?, ?, ?)",
            (work_id, series_id, index + 1),
        )

    for index, (person_name, role) in enumerate(people):
        if role == "著者":
            continue
        person_id = upsert_person(conn, person_name)
        conn.execute(
            "INSERT OR IGNORE INTO work_people (work_id, person_id, role, display_order) VALUES (?, ?, ?, ?)",
            (work_id, person_id, role, index + 1),
        )

    for index, person_name in enumerate(authors or []):
        person_name = person_name.strip()
        if not person_name:
            continue
        person_id = upsert_person(conn, person_name)
        conn.execute(
            "INSERT OR IGNORE INTO work_people (work_id, person_id, role, display_order) VALUES (?, ?, ?, ?)",
            (work_id, person_id, "著者", index + 1),
        )


def read_json_library() -> dict:
    if not JSON_PATH.exists():
        ensure_library_json()
    try:
        with JSON_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"library.json の形式が不正です: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"library.json を読み込めません: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("library.json のトップレベルはオブジェクトである必要があります")
    if "works" not in raw or not isinstance(raw["works"], list):
        raise ValueError("library.json は 'works' 配列を持つ必要があります")
    return raw


def compute_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4096), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_json_to_sqlite(force: bool = False) -> bool:
    if not JSON_PATH.exists():
        ensure_library_json()
    try:
        data = read_json_library()
    except ValueError:
        return False

    current_hash = compute_file_hash(JSON_PATH)
    current_mtime = JSON_PATH.stat().st_mtime
    previous = {}
    if SYNC_STATE_PATH.exists():
        try:
            previous = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}

    if not force and previous.get("hash") == current_hash and previous.get("mtime") == current_mtime:
        return True

    conn = get_db()
    try:
        for table_name in [
            "work_tags",
            "work_series",
            "work_people",
            "versions",
            "tags",
            "series",
            "people",
            "works",
        ]:
            conn.execute(f"DELETE FROM {table_name}")

        for work in data.get("works", []):
            work_id = work.get("id") or str(uuid.uuid4())
            title = work.get("title") or "未設定タイトル"
            reading = work.get("reading") or ""
            evaluation = work.get("evaluation")
            notes = work.get("notes") or ""
            created_at = work.get("created_at") or utc_now()
            updated_at = work.get("updated_at") or created_at

            conn.execute(
                """
                INSERT INTO works (id, title, reading, evaluation, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (work_id, title, reading, evaluation, notes, created_at, updated_at),
            )

            for tag_name in work.get("tags", []):
                tag_name = str(tag_name).strip()
                if not tag_name:
                    continue
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
                if tag_id is None:
                    tag_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
                else:
                    tag_id = tag_id["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO work_tags (work_id, tag_id) VALUES (?, ?)",
                    (work_id, tag_id),
                )

            for series_name in work.get("series", []):
                series_name = str(series_name).strip()
                if not series_name:
                    continue
                series_row = conn.execute("SELECT id FROM series WHERE name = ?", (series_name,)).fetchone()
                if series_row is None:
                    series_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO series (id, name, sort_order) VALUES (?, ?, ?)",
                        (series_id, series_name, 0),
                    )
                else:
                    series_id = series_row["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO work_series (work_id, series_id, sort_order) VALUES (?, ?, ?)",
                    (work_id, series_id, 0),
                )

            for person in work.get("people", []):
                name = str(person.get("name") or "").strip()
                role = str(person.get("role") or "").strip()
                if not name:
                    continue
                person_row = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
                if person_row is None:
                    person_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO people (id, name) VALUES (?, ?)", (person_id, name))
                else:
                    person_id = person_row["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO work_people (work_id, person_id, role, display_order) VALUES (?, ?, ?, ?)",
                    (work_id, person_id, role or "", 0),
                )

            for version in work.get("versions", []):
                version_id = version.get("id") or str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO versions (
                        id, work_id, title, media_type, format_type, volume,
                        publisher, purchase_source, location, status, platform,
                        release_date, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        work_id,
                        version.get("title") or work.get("title") or "",
                        version.get("media_type") or "書籍",
                        version.get("format_type") or "",
                        version.get("volume") or "",
                        version.get("publisher") or "",
                        version.get("purchase_source") or "",
                        version.get("location") or "",
                        version.get("status") or "未読",
                        version.get("platform") or "",
                        version.get("release_date") or "",
                        version.get("created_at") or utc_now(),
                        version.get("updated_at") or utc_now(),
                    ),
                )

        conn.commit()
        write_sync_state(current_hash, current_mtime)
        return True
    finally:
        conn.close()


def export_library_json() -> dict:
    conn = get_db()
    works = []
    for work in conn.execute("SELECT * FROM works ORDER BY title COLLATE NOCASE").fetchall():
        work_id = work["id"]
        versions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM versions WHERE work_id = ? ORDER BY created_at DESC",
                (work_id,),
            ).fetchall()
        ]
        tags = [
            row["name"]
            for row in conn.execute(
                """
                SELECT t.name FROM tags t
                JOIN work_tags wt ON wt.tag_id = t.id
                WHERE wt.work_id = ?
                ORDER BY t.name COLLATE NOCASE
                """,
                (work_id,),
            ).fetchall()
        ]
        series = [
            row["name"]
            for row in conn.execute(
                """
                SELECT s.name FROM series s
                JOIN work_series ws ON ws.series_id = s.id
                WHERE ws.work_id = ?
                ORDER BY s.name COLLATE NOCASE
                """,
                (work_id,),
            ).fetchall()
        ]
        people = [
            {"name": row["name"], "role": row["role"]}
            for row in conn.execute(
                """
                SELECT p.name, wp.role FROM people p
                JOIN work_people wp ON wp.person_id = p.id
                WHERE wp.work_id = ?
                ORDER BY wp.display_order, p.name COLLATE NOCASE
                """,
                (work_id,),
            ).fetchall()
        ]
        works.append(
            {
                "id": work_id,
                "title": work["title"],
                "reading": work["reading"],
                "evaluation": work["evaluation"],
                "notes": work["notes"],
                "created_at": work["created_at"],
                "updated_at": work["updated_at"],
                "tags": tags,
                "series": series,
                "people": people,
                "versions": versions,
            }
        )
    conn.close()
    return {"works": works}


def write_library_json() -> None:
    JSON_PATH.write_text(json.dumps(export_library_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    current_hash = compute_file_hash(JSON_PATH)
    current_mtime = JSON_PATH.stat().st_mtime
    write_sync_state(current_hash, current_mtime)


def ensure_stable_data_mode() -> None:
    ensure_config()
    ensure_library_json()
    init_db()
    sync_json_to_sqlite(force=False)


@app.before_request
def load_library_status() -> None:
    g.read_only_mode = False
    g.sync_warning = None
    try:
        read_json_library()
    except ValueError as exc:
        g.read_only_mode = True
        g.sync_warning = str(exc)


@app.route("/")
def dashboard():
    conn = get_db()
    cleanup_orphaned_records(conn)
    total_works = conn.execute("SELECT COUNT(*) AS count FROM works").fetchone()["count"]
    total_versions = conn.execute(
        "SELECT COUNT(*) AS count FROM versions v JOIN works w ON w.id = v.work_id"
    ).fetchone()["count"]
    unread = conn.execute(
        "SELECT COUNT(*) AS count FROM versions v JOIN works w ON w.id = v.work_id WHERE v.status = '未読'"
    ).fetchone()["count"]
    by_media = conn.execute(
        "SELECT v.media_type, COUNT(*) AS count FROM versions v JOIN works w ON w.id = v.work_id GROUP BY v.media_type ORDER BY count DESC"
    ).fetchall()
    recent = conn.execute(
        "SELECT * FROM works ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return render_template(
        "dashboard.html",
        total_works=total_works,
        total_versions=total_versions,
        unread=unread,
        by_media=by_media,
        recent=recent,
        sync_warning=g.sync_warning,
        read_only_mode=g.read_only_mode,
    )


@app.route("/works")
def work_list():
    query = request.args.get("q", "").strip()
    media_type = request.args.get("media_type", "").strip()
    status = request.args.get("status", "").strip()
    sort = request.args.get("sort", "title")
    tag_name = request.args.get("tag", "").strip()
    series_name = request.args.get("series", "").strip()
    person_name = request.args.get("person", "").strip()

    conn = get_db()
    base_sql = """
        SELECT DISTINCT w.*
        FROM works w
        LEFT JOIN versions v ON v.work_id = w.id
        LEFT JOIN work_tags wt ON wt.work_id = w.id
        LEFT JOIN tags t ON t.id = wt.tag_id
        LEFT JOIN work_series ws ON ws.work_id = w.id
        LEFT JOIN series s ON s.id = ws.series_id
        LEFT JOIN work_people wp ON wp.work_id = w.id
        LEFT JOIN people p ON p.id = wp.person_id
        WHERE 1 = 1
    """
    params = []

    if query:
        like = f"%{query}%"
        base_sql += """
            AND (
                w.title LIKE ? OR w.reading LIKE ? OR v.location LIKE ? OR v.publisher LIKE ? OR v.title LIKE ? OR t.name LIKE ? OR s.name LIKE ? OR p.name LIKE ?
            )
        """
        params.extend([like, like, like, like, like, like, like, like])

    if media_type:
        base_sql += " AND v.media_type = ? "
        params.append(media_type)

    if status:
        base_sql += " AND v.status = ? "
        params.append(status)

    if tag_name:
        base_sql += " AND t.name = ? "
        params.append(tag_name)

    if series_name:
        base_sql += " AND s.name = ? "
        params.append(series_name)

    if person_name:
        base_sql += " AND p.name = ? "
        params.append(person_name)

    sort_mapping = {
        "title": "w.title COLLATE NOCASE",
        "reading": "w.reading COLLATE NOCASE",
        "created_at": "w.created_at DESC",
        "updated_at": "w.updated_at DESC",
        "evaluation": "w.evaluation DESC, w.title COLLATE NOCASE",
    }
    order_clause = sort_mapping.get(sort, "w.title COLLATE NOCASE")
    base_sql += f" ORDER BY {order_clause} "

    rows = conn.execute(base_sql, params).fetchall()

    media_types = [row["media_type"] for row in conn.execute("SELECT DISTINCT media_type FROM versions WHERE media_type IS NOT NULL AND media_type != '' ORDER BY media_type COLLATE NOCASE").fetchall()]
    statuses = ["未読", "読書中", "読了"]

    works = []
    for work in rows:
        work_id = work["id"]
        first_version = conn.execute(
            "SELECT * FROM versions WHERE work_id = ? ORDER BY created_at DESC LIMIT 1",
            (work_id,),
        ).fetchone()
        works.append({"work": work, "version": first_version})
    conn.close()
    return render_template(
        "work_list.html",
        works=works,
        query=query,
        media_type=media_type,
        status=status,
        sort=sort,
        media_types=media_types,
        statuses=statuses,
        sync_warning=g.sync_warning,
        read_only_mode=g.read_only_mode,
    )


@app.route("/works/bulk", methods=["GET", "POST"])
def work_bulk_add():
    if request.method == "POST":
        if g.read_only_mode:
            flash("JSON同期に問題があるため、一括追加はできません。")
            return redirect(url_for("work_bulk_add"))

        identifiers = parse_bulk_identifier_text(request.form.get("identifiers") or "")
        if not identifiers:
            flash("ISBN / JAN / ASIN を 1 件以上入力してください。")
            return redirect(url_for("work_bulk_add"))

        candidates = []
        for identifier in identifiers:
            candidate = lookup_external_candidates(identifier, "自動")
            selected = (candidate[0] if candidate else {})
            candidates.append(build_bulk_candidate(identifier, selected))

        session["bulk_candidates"] = json.dumps(candidates, ensure_ascii=False)
        return redirect(url_for("work_bulk_review"))

    return render_template("bulk_add.html", sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/works/bulk/review", methods=["GET", "POST"])
def work_bulk_review():
    raw_candidates = session.get("bulk_candidates")
    if not raw_candidates:
        return redirect(url_for("work_bulk_add"))

    if request.method == "POST":
        if g.read_only_mode:
            flash("JSON同期に問題があるため、一括追加はできません。")
            return redirect(url_for("work_bulk_review"))

        identifiers = request.form.getlist("identifier")
        if not identifiers:
            flash("保存対象がありません。")
            return redirect(url_for("work_bulk_add"))

        candidate_count = len(identifiers)
        payloads = []
        for index in range(candidate_count):
            fields = {
                "title": request.form.getlist("title")[index] if index < len(request.form.getlist("title")) else "",
                "reading": request.form.getlist("reading")[index] if index < len(request.form.getlist("reading")) else "",
                "media_type": request.form.getlist("media_type")[index] if index < len(request.form.getlist("media_type")) else "書籍",
                "format_type": request.form.getlist("format_type")[index] if index < len(request.form.getlist("format_type")) else "",
                "publisher": request.form.getlist("publisher")[index] if index < len(request.form.getlist("publisher")) else "",
                "purchase_source": request.form.getlist("purchase_source")[index] if index < len(request.form.getlist("purchase_source")) else "",
                "version_title": request.form.getlist("version_title")[index] if index < len(request.form.getlist("version_title")) else "",
                "status": request.form.getlist("status")[index] if index < len(request.form.getlist("status")) else "未読",
                "tags": request.form.getlist("tags")[index] if index < len(request.form.getlist("tags")) else "",
                "series": request.form.getlist("series")[index] if index < len(request.form.getlist("series")) else "",
                "people": request.form.getlist("people")[index] if index < len(request.form.getlist("people")) else "",
                "authors": request.form.getlist("authors")[index] if index < len(request.form.getlist("authors")) else "",
                "notes": request.form.getlist("notes")[index] if index < len(request.form.getlist("notes")) else "",
                "location": request.form.getlist("location")[index] if index < len(request.form.getlist("location")) else "",
                "platform": request.form.getlist("platform")[index] if index < len(request.form.getlist("platform")) else "",
                "volume": request.form.getlist("volume")[index] if index < len(request.form.getlist("volume")) else "",
                "evaluation": request.form.getlist("evaluation")[index] if index < len(request.form.getlist("evaluation")) else "",
            }
            if not fields["title"].strip():
                continue
            payload = {
                "title": fields["title"],
                "reading": fields["reading"],
                "evaluation": fields["evaluation"],
                "notes": fields["notes"],
                "version_title": fields["version_title"] or fields["title"],
                "media_type": fields["media_type"] or "書籍",
                "format_type": fields["format_type"],
                "volume": fields["volume"],
                "publisher": fields["publisher"],
                "purchase_source": fields["purchase_source"],
                "location": fields["location"],
                "status": fields["status"],
                "platform": fields["platform"],
                "tags": fields["tags"],
                "series": fields["series"],
                "people": fields["people"],
                "authors": fields["authors"],
            }
            payloads.append((identifiers[index], validate_work_form_data(payload)))

        if not payloads:
            flash("保存対象がありません。")
            return redirect(url_for("work_bulk_add"))

        conn = get_db()
        try:
            created_ids = []
            for _, payload in payloads:
                created_ids.append(save_work_payload(conn, payload))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            flash(f"一括追加中にエラーが発生しました: {exc}")
            return redirect(url_for("work_bulk_review"))
        finally:
            conn.close()

        session.pop("bulk_candidates", None)
        write_library_json()
        flash(f"{len(created_ids)} 件を一括追加しました。")
        return redirect(url_for("work_list"))

    try:
        candidates = json.loads(raw_candidates)
    except json.JSONDecodeError:
        candidates = []

    return render_template("bulk_review.html", candidates=candidates, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/works/new", methods=["GET", "POST"])
def work_new():
    if request.method == "POST":
        if g.read_only_mode:
            flash("JSON同期に問題があるため、新規登録はできません。")
            return redirect(url_for("work_list"))

        try:
            payload = validate_work_form_data(request.form)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("work_new"))

        conn = get_db()
        try:
            work_id = save_work_payload(conn, payload)
            conn.commit()
        finally:
            conn.close()
        write_library_json()
        flash("作品を登録しました。")
        return redirect(url_for("work_detail", work_id=work_id))

    defaults = {
        "title": request.args.get("title", ""),
        "reading": request.args.get("reading", ""),
        "media_type": request.args.get("media_type", "書籍"),
        "format_type": request.args.get("format_type", ""),
        "publisher": request.args.get("publisher", ""),
        "purchase_source": request.args.get("purchase_source", ""),
        "version_title": request.args.get("version_title", ""),
        "tags": ", ".join(request.args.getlist("tag") or []),
        "series": ", ".join(request.args.getlist("series") or []),
        "people": request.args.get("people", ""),
        "authors": "\n".join(request.args.getlist("authors") or []),
    }
    return render_template("work_form.html", work=None, version=None, tags=defaults["tags"], series=defaults["series"], people=defaults["people"], authors=defaults["authors"], defaults=defaults, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/works/<work_id>")
def work_detail(work_id):
    conn = get_db()
    work = conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
    if work is None:
        conn.close()
        return redirect(url_for("work_list"))

    versions = conn.execute("SELECT * FROM versions WHERE work_id = ? ORDER BY created_at DESC", (work_id,)).fetchall()
    tags = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN work_tags wt ON wt.tag_id = t.id
        WHERE wt.work_id = ?
        ORDER BY t.name COLLATE NOCASE
        """,
        (work_id,),
    ).fetchall()
    series = conn.execute(
        """
        SELECT s.name, ws.sort_order FROM series s
        JOIN work_series ws ON ws.series_id = s.id
        WHERE ws.work_id = ?
        ORDER BY s.name COLLATE NOCASE
        """,
        (work_id,),
    ).fetchall()
    people = conn.execute(
        """
        SELECT p.name, wp.role FROM people p
        JOIN work_people wp ON wp.person_id = p.id
        WHERE wp.work_id = ? AND wp.role != '著者'
        ORDER BY wp.display_order, p.name COLLATE NOCASE
        """,
        (work_id,),
    ).fetchall()
    authors = conn.execute(
        """
        SELECT p.name FROM people p
        JOIN work_people wp ON wp.person_id = p.id
        WHERE wp.work_id = ? AND wp.role = '著者'
        ORDER BY wp.display_order, p.name COLLATE NOCASE
        """,
        (work_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "work_detail.html",
        work=work,
        versions=versions,
        tags=[tag["name"] for tag in tags],
        series=series,
        people=people,
        authors=[author["name"] for author in authors],
        sync_warning=g.sync_warning,
        read_only_mode=g.read_only_mode,
    )


@app.route("/works/<work_id>/edit", methods=["GET", "POST"])
def work_edit(work_id):
    conn = get_db()
    work = conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
    if work is None:
        conn.close()
        return redirect(url_for("work_list"))

    if request.method == "POST":
        if g.read_only_mode:
            flash("JSON同期に問題があるため、編集はできません。")
            return redirect(url_for("work_detail", work_id=work_id))

        try:
            payload = validate_work_form_data(request.form)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("work_edit", work_id=work_id))

        now = utc_now()
        tags = payload["tags"]
        series = payload["series"]
        people = payload["people"]
        authors = payload["authors"]
        conn.execute(
            """
            UPDATE works
            SET title = ?, reading = ?, evaluation = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload["title"],
                payload["reading"],
                payload["evaluation"],
                payload["notes"],
                now,
                work_id,
            ),
        )
        version = conn.execute("SELECT * FROM versions WHERE work_id = ? ORDER BY created_at DESC LIMIT 1", (work_id,)).fetchone()
        if version:
            conn.execute(
                """
                UPDATE versions
                SET title = ?, media_type = ?, format_type = ?, volume = ?, publisher = ?, purchase_source = ?, location = ?, status = ?, platform = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["version_title"],
                    payload["media_type"],
                    payload["format_type"],
                    payload["volume"],
                    payload["publisher"],
                    payload["purchase_source"],
                    payload["location"],
                    payload["status"],
                    payload["platform"],
                    now,
                    version["id"],
                ),
            )
        sync_work_metadata(conn, work_id, tags, series, people, authors)
        conn.commit()
        conn.close()
        write_library_json()
        flash("作品を更新しました。")
        return redirect(url_for("work_detail", work_id=work_id))

    version = conn.execute("SELECT * FROM versions WHERE work_id = ? ORDER BY created_at DESC LIMIT 1", (work_id,)).fetchone()
    tag_names = [tag["name"] for tag in conn.execute(
        "SELECT t.name FROM tags t JOIN work_tags wt ON wt.tag_id = t.id WHERE wt.work_id = ? ORDER BY t.name COLLATE NOCASE",
        (work_id,),
    ).fetchall()]
    series_names = [s["name"] for s in conn.execute(
        "SELECT s.name FROM series s JOIN work_series ws ON ws.series_id = s.id WHERE ws.work_id = ? ORDER BY s.name COLLATE NOCASE",
        (work_id,),
    ).fetchall()]
    people_names = [
        f"{p['name']}|{p['role']}" if p['role'] else p['name']
        for p in conn.execute(
            "SELECT p.name, wp.role FROM people p JOIN work_people wp ON wp.person_id = p.id WHERE wp.work_id = ? AND wp.role != '著者' ORDER BY wp.display_order, p.name COLLATE NOCASE",
            (work_id,),
        ).fetchall()
    ]
    author_names = [
        p["name"] for p in conn.execute(
            "SELECT p.name FROM people p JOIN work_people wp ON wp.person_id = p.id WHERE wp.work_id = ? AND wp.role = '著者' ORDER BY wp.display_order, p.name COLLATE NOCASE",
            (work_id,),
        ).fetchall()
    ]
    conn.close()
    return render_template(
        "work_form.html",
        work=work,
        version=version,
        tags=", ".join(tag_names),
        series=", ".join(series_names),
        people="\n".join(people_names),
        authors="\n".join(author_names),
        sync_warning=g.sync_warning,
        read_only_mode=g.read_only_mode,
    )


@app.route("/works/<work_id>/delete", methods=["POST"])
def work_delete(work_id):
    if g.read_only_mode:
        flash("JSON同期に問題があるため、削除はできません。")
        return redirect(url_for("work_list"))

    conn = get_db()
    conn.execute("DELETE FROM work_tags WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM work_series WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM work_people WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM versions WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM works WHERE id = ?", (work_id,))
    conn.commit()
    conn.close()
    write_library_json()
    flash("作品を削除しました。")
    return redirect(url_for("work_list"))


@app.route("/import", methods=["GET", "POST"])
def import_library():
    if request.method == "POST":
        if g.read_only_mode:
            flash("JSON同期に問題があるため、インポートはできません。")
            return redirect(url_for("work_list"))
        uploaded = request.files.get("library_file")
        if not uploaded or not uploaded.filename:
            flash("ファイルを選択してください。")
            return redirect(url_for("import_library"))
        mode = request.form.get("mode", "replace")
        try:
            data = json.loads(uploaded.read().decode("utf-8"))
        except Exception as exc:
            flash(f"JSONの読み込みに失敗しました: {exc}")
            return redirect(url_for("import_library"))

        if not isinstance(data, dict) or not isinstance(data.get("works", []), list):
            flash("works 配列を含む JSON ファイルを選択してください。")
            return redirect(url_for("import_library"))

        conn = get_db()
        if mode == "replace":
            for table in ["work_tags", "work_series", "work_people", "versions", "tags", "series", "people", "works"]:
                conn.execute(f"DELETE FROM {table}")
            for work in data["works"]:
                work_id = work.get("id") or str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO works (id, title, reading, evaluation, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        work_id,
                        work.get("title") or "未設定タイトル",
                        work.get("reading") or "",
                        work.get("evaluation"),
                        work.get("notes") or "",
                        work.get("created_at") or utc_now(),
                        work.get("updated_at") or utc_now(),
                    ),
                )
                for version in work.get("versions", []):
                    conn.execute(
                        "INSERT INTO versions (id, work_id, title, media_type, format_type, volume, publisher, purchase_source, location, status, platform, release_date, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            version.get("id") or str(uuid.uuid4()),
                            work_id,
                            version.get("title") or work.get("title") or "",
                            version.get("media_type") or "書籍",
                            version.get("format_type") or "",
                            version.get("volume") or "",
                            version.get("publisher") or "",
                            version.get("purchase_source") or "",
                            version.get("location") or "",
                            version.get("status") or "未読",
                            version.get("platform") or "",
                            version.get("release_date") or "",
                            version.get("created_at") or utc_now(),
                            version.get("updated_at") or utc_now(),
                        ),
                    )
                for tag_name in work.get("tags", []):
                    tag_name = str(tag_name).strip()
                    if not tag_name:
                        continue
                    tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
                    if tag_row is None:
                        tag_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
                    else:
                        tag_id = tag_row["id"]
                    conn.execute("INSERT OR IGNORE INTO work_tags (work_id, tag_id) VALUES (?, ?)", (work_id, tag_id))
                for name in work.get("series", []):
                    series_name = str(name).strip()
                    if not series_name:
                        continue
                    series_row = conn.execute("SELECT id FROM series WHERE name = ?", (series_name,)).fetchone()
                    if series_row is None:
                        series_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO series (id, name, sort_order) VALUES (?, ?, ?)", (series_id, series_name, 0))
                    else:
                        series_id = series_row["id"]
                    conn.execute("INSERT OR IGNORE INTO work_series (work_id, series_id, sort_order) VALUES (?, ?, ?)", (work_id, series_id, 0))
                for person in work.get("people", []):
                    name = str(person.get("name") or "").strip()
                    role = str(person.get("role") or "").strip()
                    if not name:
                        continue
                    person_row = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
                    if person_row is None:
                        person_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO people (id, name) VALUES (?, ?)", (person_id, name))
                    else:
                        person_id = person_row["id"]
                    conn.execute("INSERT OR IGNORE INTO work_people (work_id, person_id, role, display_order) VALUES (?, ?, ?, ?)", (work_id, person_id, role, 0))
        else:
            for work in data["works"]:
                if not work.get("title"):
                    continue
                existing = conn.execute("SELECT id FROM works WHERE title = ? LIMIT 1", (work.get("title"),)).fetchone()
                if existing is None:
                    work_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO works (id, title, reading, evaluation, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (work_id, work.get("title") or "未設定タイトル", work.get("reading") or "", work.get("evaluation"), work.get("notes") or "", work.get("created_at") or utc_now(), work.get("updated_at") or utc_now()),
                    )
                    for version in work.get("versions", []):
                        conn.execute(
                            "INSERT INTO versions (id, work_id, title, media_type, format_type, volume, publisher, purchase_source, location, status, platform, release_date, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                version.get("id") or str(uuid.uuid4()),
                                work_id,
                                version.get("title") or work.get("title") or "",
                                version.get("media_type") or "書籍",
                                version.get("format_type") or "",
                                version.get("volume") or "",
                                version.get("publisher") or "",
                                version.get("purchase_source") or "",
                                version.get("location") or "",
                                version.get("status") or "未読",
                                version.get("platform") or "",
                                version.get("release_date") or "",
                                version.get("created_at") or utc_now(),
                                version.get("updated_at") or utc_now(),
                            ),
                        )
        conn.commit()
        conn.close()
        write_library_json()
        flash("JSON をインポートしました。")
        return redirect(url_for("work_list"))

    return render_template("import_form.html", sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/tags")
def tag_list():
    conn = get_db()
    cleanup_orphaned_records(conn)
    rows = conn.execute(
        """
        SELECT t.name, COUNT(wt.tag_id) AS count
        FROM tags t
        LEFT JOIN work_tags wt ON wt.tag_id = t.id
        GROUP BY t.name
        HAVING COUNT(wt.tag_id) > 0
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()
    return render_template("tag_list.html", rows=rows, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/series")
def series_list():
    conn = get_db()
    cleanup_orphaned_records(conn)
    rows = conn.execute(
        """
        SELECT s.name, COUNT(ws.series_id) AS count
        FROM series s
        LEFT JOIN work_series ws ON ws.series_id = s.id
        GROUP BY s.name
        HAVING COUNT(ws.series_id) > 0
        ORDER BY s.name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()
    return render_template("series_list.html", rows=rows, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/people")
def people_list():
    conn = get_db()
    cleanup_orphaned_records(conn)
    rows = conn.execute(
        """
        SELECT p.name, COUNT(wp.person_id) AS count
        FROM people p
        LEFT JOIN work_people wp ON wp.person_id = p.id
        GROUP BY p.name
        HAVING COUNT(wp.person_id) > 0
        ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()
    return render_template("people_list.html", rows=rows, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/lookup", methods=["GET", "POST"])
def lookup():
    identifier = (request.form.get("identifier") or request.args.get("identifier") or "").strip()
    requested_media_type = request.form.get("search_media_type") or request.args.get("search_media_type")
    if requested_media_type is not None:
        preferred_media_type = requested_media_type.strip()
    elif session.get("last_lookup_identifier") and identifier == str(session.get("last_lookup_identifier", "")).strip():
        preferred_media_type = str(session.get("last_lookup_media_type") or "自動").strip()
    else:
        preferred_media_type = "自動"
    candidates = []

    if identifier:
        candidates = lookup_external_candidates(identifier, preferred_media_type)
        session["last_lookup_identifier"] = identifier
        session["last_lookup_candidates"] = json.dumps(candidates, ensure_ascii=False)
        session["last_lookup_media_type"] = preferred_media_type
    elif session.get("last_lookup_identifier"):
        identifier = str(session.get("last_lookup_identifier", "")).strip()
        preferred_media_type = str(session.get("last_lookup_media_type") or "自動").strip()
        raw_candidates = session.get("last_lookup_candidates")
        if raw_candidates:
            try:
                candidates = json.loads(raw_candidates)
            except json.JSONDecodeError:
                candidates = []

    return render_template("lookup.html", identifier=identifier, search_media_type=preferred_media_type, candidates=candidates, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/lookup/confirm", methods=["POST"])
def lookup_confirm():
    identifier = request.form.get("identifier", "").strip()
    preferred_media_type = (request.form.get("search_media_type") or session.get("last_lookup_media_type") or "自動").strip()
    if identifier:
        session["last_lookup_identifier"] = identifier
        session["last_lookup_media_type"] = preferred_media_type
        session["last_lookup_candidates"] = json.dumps(lookup_external_candidates(identifier, preferred_media_type), ensure_ascii=False)

    candidate = {
        "provider": request.form.get("provider", ""),
        "identifier": request.form.get("identifier", ""),
        "title": request.form.get("title", ""),
        "reading": request.form.get("reading", ""),
        "media_type": request.form.get("media_type", "書籍"),
        "format_type": request.form.get("format_type", ""),
        "publisher": request.form.get("publisher", ""),
        "purchase_source": request.form.get("purchase_source", ""),
        "authors": request.form.get("authors", "").splitlines(),
        "series": request.form.get("series", "").splitlines(),
        "tags": request.form.get("tags", "").splitlines(),
    }
    return render_template("lookup_confirm.html", candidate=candidate, sync_warning=g.sync_warning, read_only_mode=g.read_only_mode)


@app.route("/lookup/apply", methods=["POST"])
def lookup_apply():
    candidate = {
        "title": request.form.get("title", ""),
        "reading": request.form.get("reading", ""),
        "media_type": request.form.get("media_type", "書籍"),
        "format_type": request.form.get("format_type", ""),
        "publisher": request.form.get("publisher", ""),
        "purchase_source": request.form.get("purchase_source", ""),
        "version_title": request.form.get("title", ""),
        "authors": request.form.get("authors", "").splitlines(),
        "series": request.form.get("series", "").splitlines(),
        "tags": request.form.get("tags", "").splitlines(),
        "people": "",
    }
    params = {
        "title": candidate["title"],
        "reading": candidate["reading"],
        "media_type": candidate["media_type"],
        "format_type": candidate["format_type"],
        "publisher": candidate["publisher"],
        "purchase_source": candidate["purchase_source"],
        "version_title": candidate["version_title"],
        "people": candidate["people"],
        "authors": "\n".join(author for author in candidate["authors"] if author.strip()),
    }
    for tag in candidate["tags"]:
        if tag.strip():
            params.setdefault("tag", []).append(tag.strip())
    for series_name in candidate["series"]:
        if series_name.strip():
            params.setdefault("series", []).append(series_name.strip())
    return redirect(url_for("work_new", **params))


@app.route("/export")
def export_library():
    payload = json.dumps(export_library_json(), ensure_ascii=False, indent=2)
    return send_file(
        io.BytesIO(payload.encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name="library_export.json",
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    config = load_config()
    current_json_path = str(JSON_PATH)

    if request.method == "POST":
        raw_path = (request.form.get("json_file") or "library.json").strip()
        if raw_path:
            candidate = Path(raw_path)
            if candidate.exists() and candidate.is_dir():
                raw_path = str((candidate / "library.json").resolve())
            elif not candidate.name.lower().endswith(".json") and not candidate.exists():
                raw_path = str(candidate.with_suffix(".json").resolve())
        config["json_file"] = raw_path or "library.json"
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        refresh_runtime_paths()
        try:
            ensure_library_json()
            sync_json_to_sqlite(force=True)
            flash("設定を保存しました。")
        except Exception as exc:
            flash(f"設定の保存後にエラーが発生しました: {exc}")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        json_file=current_json_path,
        sync_warning=g.sync_warning,
        read_only_mode=g.read_only_mode,
    )


@app.route("/health")
def health_check():
    return {"status": "ok", "json_valid": not g.read_only_mode, "library_file": str(JSON_PATH)}


ensure_config()
refresh_runtime_paths()
ensure_library_json()
init_db()
conn = get_db()
cleanup_orphaned_records(conn)
conn.close()
sync_json_to_sqlite(force=False)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
