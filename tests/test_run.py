import json
import tempfile
import unittest
from pathlib import Path

from collector_10.cli import main as cli_main
from collector_10.run import run_scenario
from collector_10.store import RecordStore

ROSTER_V10 = (
    "generation,project,person_id,name,start,end,certificate\n"
    "10,P1,ID-A,张某,2026-11-01,2026-11-05,C1\n"
    "10,P2,ID-A,张某,2026-11-06,,C2\n"
    "10,P1,ID-B,张某,2026-11-01,,C3\n"
)
CERTIFICATES = [
    {"id": "C1", "person": "ID-A", "from": "2026-11-01", "until": "2026-11-05"},
    {"id": "C2", "person": "ID-A", "from": "2026-11-06", "until": "2027-11-05"},
    {"id": "C3", "person": "ID-B", "from": "2026-11-01", "until": "2027-11-01"},
]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class RunWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.root = self.directory / "fixtures"
        self.root.mkdir()
        self.database = self.directory / "state.sqlite3"
        self.output = self.directory / "result.json"

    def arrange(self, routes, files=None):
        write_json(self.root / "routes.json", routes)
        for name, content in (files or {}).items():
            path = self.root / name
            if isinstance(content, (list, dict)):
                write_json(path, content)
            else:
                path.write_text(content, encoding="utf-8")

    def arrange_standard(self, work=None):
        self.arrange(
            {"/roster": [{"status": 200, "file": "roster.csv"}]},
            {"roster.csv": ROSTER_V10, "certificates.json": CERTIFICATES, "work.json": work or []},
        )

    def execute(self, sources=("/roster",), on="2026-11-04", complete=True, run_id="run-test"):
        scenario = self.directory / "scenario.json"
        write_json(scenario, {
            "name": "test",
            "run_id": run_id,
            "sources": list(sources),
            "options": {"on": on, "complete": complete},
            "fixture_root": str(self.root),
        })
        return run_scenario(scenario, self.database, self.output)

    def roster_entry(self, result, person, project):
        for entry in result["roster"]:
            if entry["person"] == person and entry["project"] == project:
                return entry
        return None

    def test_historical_snapshot_and_closed_interval_boundaries(self):
        self.arrange_standard()
        first = self.execute(on="2026-11-05")
        self.assertEqual(first["generation"], 10)
        # 任职 end 与证书 until 均为闭区间，边界日当天仍在岗且资质有效
        self.assertEqual(self.roster_entry(first, "ID-A", "P1"), {"person": "ID-A", "project": "P1", "qualified": True})
        self.assertIsNone(self.roster_entry(first, "ID-A", "P2"))
        second = self.execute(on="2026-11-06", run_id="run-next-day")
        self.assertIsNone(self.roster_entry(second, "ID-A", "P1"))
        self.assertEqual(self.roster_entry(second, "ID-A", "P2"), {"person": "ID-A", "project": "P2", "qualified": True})

    def test_certificate_validity_boundary_day(self):
        self.arrange_standard()
        on_valid = self.execute(on="2027-11-01")
        self.assertTrue(self.roster_entry(on_valid, "ID-B", "P1")["qualified"])
        on_expired = self.execute(on="2027-11-02", run_id="run-expired")
        entry = self.roster_entry(on_expired, "ID-B", "P1")
        self.assertIsNotNone(entry)  # 仍在岗（空 end），但证书已过期
        self.assertFalse(entry["qualified"])

    def test_same_name_distinct_ids_are_not_merged(self):
        self.arrange_standard()
        result = self.execute(on="2026-11-04")
        people = {entry["person"] for entry in result["roster"]}
        self.assertEqual(people, {"ID-A", "ID-B"})  # 两人同名张某，不合并
        self.assertEqual(result["quarantine"], [])
        with RecordStore(self.database).connect() as connection:
            names = connection.execute("SELECT person_id, name FROM persons ORDER BY person_id").fetchall()
        self.assertEqual([dict(row) for row in names], [
            {"person_id": "ID-A", "name": "张某"},
            {"person_id": "ID-B", "name": "张某"},
        ])

    def test_identity_conflict_quarantined_and_identity_kept(self):
        self.arrange(
            {
                "/roster": [{"status": 200, "file": "roster.csv"}],
                "/conflict": [{"status": 200, "file": "conflict.csv"}],
            },
            {
                "roster.csv": ROSTER_V10,
                "conflict.csv": "generation,project,person_id,name,start,end,certificate\n11,P1,ID-A,李某,2026-11-01,,C1\n",
                "certificates.json": CERTIFICATES,
            },
        )
        result = self.execute(sources=("/roster", "/conflict"), on="2026-11-07", complete=False)
        self.assertEqual(result["generation"], 10)  # 冲突批不推进水位
        conflicts = [item for item in result["quarantine"] if item["reason"] == "identity_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["person"], "ID-A")
        self.assertFalse(any(
            i.get("generation") == 11 for i in result["intervals"] if i["type"] == "employment"
        ))  # 冲突行不落区间
        with RecordStore(self.database).connect() as connection:
            name = connection.execute("SELECT name FROM persons WHERE person_id = 'ID-A'").fetchone()["name"]
        self.assertEqual(name, "张某")  # 既有身份不被改写
        repeated = self.execute(sources=("/roster", "/conflict"), on="2026-11-07", complete=False)
        self.assertEqual(repeated["notifications"], 0)
        self.assertEqual(len(repeated["quarantine"]), 1)

    def test_generation_rollback_quarantined_then_resync(self):
        self.arrange(
            {
                "/roster": [{"status": 200, "file": "roster.csv"}],
                "/rollback": [{"status": 200, "file": "rollback.csv"}],
                "/next": [{"status": 200, "file": "next.csv"}],
            },
            {
                "roster.csv": ROSTER_V10,
                "rollback.csv": "generation,project,person_id,name,start,end,certificate\n9,P1,ID-A,张某,2026-10-01,,C1\n",
                "next.csv": "generation,project,person_id,name,start,end,certificate\n11,P3,ID-C,王某,2026-11-08,,C4\n",
                "certificates.json": CERTIFICATES + [{"id": "C4", "person": "ID-C", "from": "2026-11-08", "until": ""}],
            },
        )
        rolled_back = self.execute(sources=("/roster", "/rollback"), on="2026-11-07")
        self.assertEqual(rolled_back["generation"], 10)
        self.assertEqual([item["reason"] for item in rolled_back["quarantine"]], ["generation_rollback"])
        self.assertFalse(any(i["start"] == "2026-10-01" for i in rolled_back["intervals"]))  # 回退批不生效
        # 回滚后再次同步：重放同代幂等，新完整批正常推进
        resync = self.execute(sources=("/roster", "/next"), on="2026-11-09", run_id="run-resync")
        self.assertEqual(resync["generation"], 11)
        self.assertEqual(len(resync["quarantine"]), 1)  # 隔离记录保持，不重复
        self.assertEqual(resync["notifications"], 0)  # 本轮无新增隔离
        self.assertEqual(self.roster_entry(resync, "ID-C", "P3"), {"person": "ID-C", "project": "P3", "qualified": True})

    def test_same_generation_reimport_builds_no_duplicate_intervals(self):
        self.arrange_standard()
        first = self.execute()
        second = self.execute(run_id="run-repeat")
        self.assertEqual(second["notifications"], 0)
        self.assertEqual(len(first["intervals"]), len(second["intervals"]))
        self.assertEqual(first["intervals"], second["intervals"])
        with RecordStore(self.database).connect() as connection:
            total = connection.execute("SELECT count(*) AS n FROM employment_intervals").fetchone()["n"]
        self.assertEqual(total, 3)

    def test_certificate_renewal_never_overwrites_past_validity(self):
        self.arrange_standard()
        self.execute(on="2026-11-04")
        renewed = CERTIFICATES + [{"id": "C3", "person": "ID-B", "from": "2026-11-01", "until": "2028-01-01"}]
        write_json(self.root / "certificates.json", renewed)
        result = self.execute(on="2027-12-01", run_id="run-renewal")
        c3_intervals = sorted(
            (i["start"], i["end"]) for i in result["intervals"]
            if i["type"] == "certificate" and i["certificate"] == "C3"
        )
        self.assertEqual(c3_intervals, [("2026-11-01", "2027-11-01"), ("2026-11-01", "2028-01-01")])
        self.assertTrue(self.roster_entry(result, "ID-B", "P1")["qualified"])  # 续期区间覆盖新日期
        past = self.execute(on="2026-11-04", run_id="run-renewal-past")
        self.assertTrue(self.roster_entry(past, "ID-B", "P1")["qualified"])  # 过去有效期仍可解释

    def test_late_work_record_links_to_historical_assignment(self):
        work = [
            {"id": "W1", "person": "ID-A", "project": "P1", "worked_on": "2026-11-04", "received_on": "2026-11-10"},
            {"id": "W2", "person": "ID-A", "project": "P1", "worked_on": "2026-11-08", "received_on": "2026-11-10"},
            {"id": "W3", "person": "ID-A", "project": "P2", "worked_on": "2026-11-08", "received_on": "2026-11-10"},
        ]
        self.arrange_standard(work=work)
        result = self.execute(on="2026-11-07")
        linked = {entry["id"]: entry for entry in result["work"]}
        # W1 迟到补传：按 worked_on 关联当日任职 P1，不按 received_on 归属当前项目 P2
        self.assertEqual(linked["W1"], {"id": "W1", "linked": True, "project": "P1"})
        self.assertFalse(linked["W2"]["linked"])  # 11-08 已离开 P1
        self.assertTrue(linked["W3"]["linked"])

    def test_incomplete_batch_never_infers_departure(self):
        self.arrange(
            {
                "/roster": [{"status": 200, "file": "roster.csv"}],
                "/next": [{"status": 200, "file": "next.csv"}],
            },
            {
                "roster.csv": ROSTER_V10,
                # 新一批次只含 ID-A，ID-B 缺席
                "next.csv": "generation,project,person_id,name,start,end,certificate\n11,P2,ID-A,张某,2026-11-06,,C2\n",
                "certificates.json": CERTIFICATES,
            },
        )
        for complete in (False, True):
            result = self.execute(
                sources=("/roster", "/next"), on="2027-01-01", complete=complete,
                run_id=f"run-complete-{complete}",
            )
            entry = self.roster_entry(result, "ID-B", "P1")
            self.assertIsNotNone(entry)  # 缺席不推断离职，空 end 区间保持有效
            self.assertEqual(result["generation"], 11)  # 完整无冲突批正常推进水位

    def test_corrupt_batch_aborts_run_without_partial_state(self):
        self.arrange(
            {"/broken": [{"status": 200, "file": "broken.csv"}]},
            {"broken.csv": "generation,project,person_id,name,start,end,certificate\n10,P1,ID-A\n"},
        )
        with self.assertRaises(ValueError):
            self.execute(sources=("/broken",))
        result = self.execute(sources=(), on="2026-11-04", run_id="run-empty")
        self.assertEqual(result["generation"], 0)
        self.assertEqual(result["roster"], [])
        self.assertEqual(result["intervals"], [])

    def test_mixed_generation_batch_is_rejected(self):
        self.arrange(
            {"/mixed": [{"status": 200, "file": "mixed.csv"}]},
            {"mixed.csv": (
                "generation,project,person_id,name,start,end,certificate\n"
                "10,P1,ID-A,张某,2026-11-01,,C1\n"
                "11,P1,ID-B,张某,2026-11-01,,C3\n"
            )},
        )
        with self.assertRaises(ValueError):
            self.execute(sources=("/mixed",))

    def test_cli_run_writes_output_file(self):
        self.arrange_standard()
        scenario = self.directory / "scenario.json"
        write_json(scenario, {
            "name": "cli",
            "run_id": "run-cli",
            "sources": ["/roster"],
            "options": {"on": "2026-11-04", "complete": True},
            "fixture_root": str(self.root),
        })
        self.assertEqual(cli_main(["--run", str(scenario), "--database", str(self.database), "--output", str(self.output)]), 0)
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result["generation"], 10)
        self.assertEqual(len(result["roster"]), 2)


if __name__ == "__main__":
    unittest.main()
