"""名册代次同步、证书独立区间与历史快照投影。

同步语义（与 README「来源协议」一致）：
- CSV generation 是整批源版本；批次先通过 adapter 完整性校验才可能推进名册水位。
- 空 end 表示仍在岗/仍有效，区间均为闭区间；按 on 查当日任职与证书覆盖。
- person_id 是合成稳定身份：同名不同 ID 不合并；同 ID 姓名冲突进入隔离，既有身份不变。
- generation 回退整批隔离；同代重导不重复建区间；只有完整无冲突批才推进 generation。
- 证书续期只追加新区间，不覆写过去有效期；工作记录按 worked_on 关联历史任职。
- 不从缺席推断离职：complete=false 禁止该推断，complete=true 时本实现也不自动闭区间，
  离职以源区间 end 为准，历史区间永不删除。
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .adapter import decode
from .sources import FixtureTransport
from .store import RecordStore

REQUIRED_COLUMNS = ("generation", "project", "person_id", "name", "start", "end", "certificate")
OPEN = ""


def _require_date(value, field):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"缺少日期字段 {field}")
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"日期必须为 ISO-8601: {field}={text!r}") from error
    return text


def _optional_date(value, field):
    text = str(value or "").strip()
    if not text:
        return OPEN
    return _require_date(text, field)


def load_scenario(path):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"场景文件不可读: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"场景文件不是合法 JSON: {path}") from error
    if not isinstance(manifest, dict):
        raise ValueError("场景文件必须是 JSON 对象")
    run_id = str(manifest.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("场景缺少 run_id")
    sources = manifest.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(item, str) or not item for item in sources):
        raise ValueError("sources 必须是非空字符串数组")
    options = manifest.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options 必须是对象")
    complete = options.get("complete", False)
    if not isinstance(complete, bool):
        raise ValueError("options.complete 必须是布尔值")
    on = _require_date(options.get("on") or datetime.now(timezone.utc).date().isoformat(), "on")
    return {
        "name": str(manifest.get("name") or run_id),
        "run_id": run_id,
        "sources": list(sources),
        "options": options,
        "on": on,
        "complete": complete,
        "fixture_root": str(manifest.get("fixture_root") or "."),
    }


def _watermark(connection):
    row = connection.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
    return int(row["value"]) if row else 0


def _set_watermark(connection, generation):
    connection.execute(
        "INSERT INTO meta(key, value) VALUES ('generation', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(generation),),
    )


def _quarantine(connection, *, reason, source, generation, person, detail):
    cursor = connection.execute(
        "INSERT OR IGNORE INTO quarantine(reason, source, generation, person_id, detail) VALUES (?, ?, ?, ?, ?)",
        (reason, source, generation, person, detail),
    )
    return cursor.rowcount


def _parse_batch(rows, source):
    cleaned = [
        {key: (value.strip() if isinstance(value, str) else value) for key, value in row.items()}
        for row in rows
    ]
    if not cleaned:
        return None, []
    missing = [column for column in REQUIRED_COLUMNS if column not in cleaned[0]]
    if missing:
        raise ValueError(f"{source} 批次缺少列: {', '.join(missing)}")
    parsed = []
    generations = set()
    for row in cleaned:
        try:
            generation = int(row["generation"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"generation 必须是整数: {row['generation']!r}") from error
        person, name, project = row["person_id"], row["name"], row["project"]
        if not person or not name or not project:
            raise ValueError(f"{source} 批次行缺少 person_id、name 或 project")
        start = _require_date(row["start"], "start")
        end = _optional_date(row["end"], "end")
        if end and end < start:
            raise ValueError(f"任职区间 end 早于 start: {person} {start}..{end}")
        generations.add(generation)
        parsed.append({
            "person": person, "name": name, "project": project,
            "start": start, "end": end, "certificate": row["certificate"],
        })
    if len(generations) != 1:
        raise ValueError(f"{source} 单批 CSV 只能有一个 generation，实际 {sorted(generations)}")
    return generations.pop(), parsed


def _apply_batch(connection, source, rows):
    generation, parsed = _parse_batch(rows, source)
    if generation is None:
        return 0
    current = _watermark(connection)
    if generation < current:
        return _quarantine(
            connection,
            reason="generation_rollback",
            source=source,
            generation=generation,
            person="",
            detail=f"批次代次 {generation} 低于当前名册水位 {current}，整批隔离",
        )
    notifications = 0
    conflicted = False
    for row in parsed:
        existing = connection.execute(
            "SELECT name FROM persons WHERE person_id = ?", (row["person"],)
        ).fetchone()
        if existing is not None and existing["name"] != row["name"]:
            notifications += _quarantine(
                connection,
                reason="identity_conflict",
                source=source,
                generation=generation,
                person=row["person"],
                detail=f"既有身份 {existing['name']}，新报姓名 {row['name']}，既有身份保持不变",
            )
            conflicted = True
            continue
        connection.execute(
            "INSERT OR IGNORE INTO persons(person_id, name, first_generation) VALUES (?, ?, ?)",
            (row["person"], row["name"], generation),
        )
        if row["end"]:
            open_spell = connection.execute(
                "SELECT id FROM employment_intervals WHERE person_id = ? AND project = ? AND start = ? AND end = ''",
                (row["person"], row["project"], row["start"]),
            ).fetchone()
            if open_spell is not None:
                connection.execute(
                    "UPDATE employment_intervals SET end = ?, closed_generation = ? WHERE id = ?",
                    (row["end"], generation, open_spell["id"]),
                )
                continue
        connection.execute(
            "INSERT OR IGNORE INTO employment_intervals(person_id, project, start, end, certificate, generation, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["person"], row["project"], row["start"], row["end"], row["certificate"], generation, source),
        )
    if not conflicted and generation > current:
        _set_watermark(connection, generation)
    return notifications


def _load_certificates(connection, root):
    path = root / "certificates.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("certificates.json 必须是数组")
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("certificates.json 条目必须是对象")
        certificate = str(entry.get("id") or "").strip()
        person = str(entry.get("person") or "").strip()
        if not certificate or not person:
            raise ValueError("证书条目缺少 id 或 person")
        start = _require_date(entry.get("from"), "from")
        end = _optional_date(entry.get("until"), "until")
        if end and end < start:
            raise ValueError(f"证书区间 until 早于 from: {certificate}")
        connection.execute(
            "INSERT OR IGNORE INTO certificate_intervals(certificate, person_id, start, end) VALUES (?, ?, ?, ?)",
            (certificate, person, start, end),
        )


def _load_work(connection, root):
    path = root / "work.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("work.json 必须是数组")
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("work.json 条目必须是对象")
        work_id = str(entry.get("id") or "").strip()
        person = str(entry.get("person") or "").strip()
        project = str(entry.get("project") or "").strip()
        if not work_id or not person or not project:
            raise ValueError("工作记录缺少 id、person 或 project")
        worked_on = _require_date(entry.get("worked_on"), "worked_on")
        received_on = _optional_date(entry.get("received_on"), "received_on")
        connection.execute(
            "INSERT OR IGNORE INTO work_records(id, person_id, project, worked_on, received_on) VALUES (?, ?, ?, ?, ?)",
            (work_id, person, project, worked_on, received_on or None),
        )


def _covers(connection, table, on, **where):
    clause = " AND ".join(f"{column} = ?" for column in where)
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE {clause} AND start <= ? AND (end = '' OR end >= ?) LIMIT 1",
        (*where.values(), on, on),
    ).fetchone()
    return row is not None


def _project(connection, scenario):
    on = scenario["on"]
    roster = {}
    for row in connection.execute(
        "SELECT person_id, project, certificate FROM employment_intervals "
        "WHERE start <= ? AND (end = '' OR end >= ?) ORDER BY person_id, project",
        (on, on),
    ):
        qualified = bool(row["certificate"]) and _covers(
            connection, "certificate_intervals", on,
            certificate=row["certificate"], person_id=row["person_id"],
        )
        key = (row["person_id"], row["project"])
        roster[key] = roster.get(key, False) or qualified
    work = []
    for row in connection.execute("SELECT id, person_id, project, worked_on FROM work_records ORDER BY id"):
        linked = _covers(
            connection, "employment_intervals", row["worked_on"],
            person_id=row["person_id"], project=row["project"],
        )
        work.append({"id": row["id"], "linked": linked, "project": row["project"]})
    quarantine = []
    for row in connection.execute(
        "SELECT reason, source, generation, person_id, detail FROM quarantine "
        "ORDER BY reason, source, generation, person_id"
    ):
        entry = {"reason": row["reason"], "source": row["source"], "generation": row["generation"]}
        if row["person_id"]:
            entry["person"] = row["person_id"]
        if row["detail"]:
            entry["detail"] = row["detail"]
        quarantine.append(entry)
    intervals = []
    for row in connection.execute(
        "SELECT person_id, project, start, end, certificate, generation, source FROM employment_intervals "
        "ORDER BY person_id, project, start"
    ):
        intervals.append({
            "type": "employment",
            "person": row["person_id"],
            "project": row["project"],
            "start": row["start"],
            "end": row["end"] or None,
            "certificate": row["certificate"] or None,
            "generation": row["generation"],
            "source": row["source"],
        })
    for row in connection.execute(
        "SELECT certificate, person_id, start, end FROM certificate_intervals "
        "ORDER BY person_id, certificate, start"
    ):
        intervals.append({
            "type": "certificate",
            "person": row["person_id"],
            "certificate": row["certificate"],
            "start": row["start"],
            "end": row["end"] or None,
        })
    return {
        "scenario": scenario["name"],
        "run_id": scenario["run_id"],
        "on": on,
        "roster": [
            {"person": person, "project": project, "qualified": qualified}
            for (person, project), qualified in sorted(roster.items())
        ],
        "generation": _watermark(connection),
        "work": work,
        "quarantine": quarantine,
        "intervals": intervals,
    }


def run_scenario(scenario_path, database, output=None):
    scenario = load_scenario(scenario_path)
    store = RecordStore(database)
    store.initialize()
    root = Path(scenario["fixture_root"]).resolve()
    transport = FixtureTransport(root)
    notifications = 0
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO runs(run_id, name, options, executed_at) VALUES (?, ?, ?, ?)",
            (
                scenario["run_id"],
                scenario["name"],
                json.dumps(scenario["options"], ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        for route in scenario["sources"]:
            try:
                response = transport.request(route)
            except KeyError as error:
                raise ValueError(f"未知来源路由: {route}") from error
            if response.status != 200:
                raise ValueError(f"来源 {route} 状态 {response.status}")
            notifications += _apply_batch(connection, route, decode(response.body))
        _load_certificates(connection, root)
        _load_work(connection, root)
        result = _project(connection, scenario)
    result["notifications"] = notifications
    if output:
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
