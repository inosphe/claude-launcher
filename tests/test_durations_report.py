"""``tools/durations_report.py`` ranks modules and tests from a sweep's junit report."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import durations_report as dr  # noqa: E402

JUNIT = (
    '<testsuites><testsuite>'
    '<testcase file="tests/test_a.py" classname="tests.test_a" name="t1" time="1.5"/>'
    '<testcase file="tests/test_a.py" classname="tests.test_a.TestK" name="t2" time="0.5"/>'
    '<testcase file="tests/test_b.py" classname="tests.test_b" name="t1" time="3.0"/>'
    '</testsuite></testsuites>'
)


def test_rank_orders_modules_by_cumulative_time_and_names_nodes():
    report = dr.rank(dr.collect(dr.ET.fromstring(JUNIT)), top=5)
    assert report["tests"] == 3 and report["sum_seconds"] == 5.0
    assert [m["module"] for m in report["top_modules"]] == ["tests/test_b.py", "tests/test_a.py"]
    assert report["top_modules"][0]["share"] == 0.6
    assert report["slowest_tests"][0] == {"nodeid": "tests/test_b.py::t1", "seconds": 3.0}
    assert "tests/test_a.py::TestK::t2" in {t["nodeid"] for t in report["slowest_tests"]}


def test_main_reads_a_gzipped_report_by_prefix_without_writing(tmp_path, capsys):
    receipts = tmp_path / "r"
    # receipts_dir keys by repository; a direct file path skips that lookup.
    path = tmp_path / "abc123.junit.xml.gz"
    path.write_bytes(gzip.compress(JUNIT.encode()))
    before = sorted(p.name for p in tmp_path.iterdir())
    assert dr.main([str(path), "--json", "--top", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tests"] == 3 and len(out["top_modules"]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not receipts.exists()
