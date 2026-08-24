"""Raw-capture reads must work whether or not the day has been compressed."""
import gzip
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_files import (  # noqa: E402
    data_file_exists,
    iter_data_files,
    open_data_file,
)


class TestDataFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plain(self, name: str, body: str) -> Path:
        p = self.d / name
        p.write_text(body, encoding="utf-8")
        return p

    def _gz(self, name: str, body: str) -> Path:
        p = self.d / name
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_reads_plain(self) -> None:
        p = self._plain("btc_2026-08-24.jsonl", '{"a":1}\n')
        with open_data_file(p) as fh:
            self.assertEqual(fh.read(), '{"a":1}\n')

    def test_reads_gz_when_given_plain_path(self) -> None:
        self._gz("btc_2026-08-23.jsonl.gz", '{"a":2}\n')
        with open_data_file(self.d / "btc_2026-08-23.jsonl") as fh:
            self.assertEqual(fh.read(), '{"a":2}\n')

    def test_prefers_plain_when_both_exist(self) -> None:
        self._plain("btc_2026-08-23.jsonl", "PLAIN\n")
        self._gz("btc_2026-08-23.jsonl.gz", "GZ\n")
        with open_data_file(self.d / "btc_2026-08-23.jsonl") as fh:
            self.assertEqual(fh.read(), "PLAIN\n")

    def test_missing_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            open_data_file(self.d / "nope.jsonl")

    def test_exists_covers_both_forms(self) -> None:
        self._gz("btc_2026-08-23.jsonl.gz", "x\n")
        self.assertTrue(data_file_exists(self.d / "btc_2026-08-23.jsonl"))
        self.assertFalse(data_file_exists(self.d / "eth_2026-08-23.jsonl"))

    def test_iter_returns_one_entry_per_day_sorted(self) -> None:
        self._gz("btc_2026-08-22.jsonl.gz", "a\n")
        self._gz("btc_2026-08-23.jsonl.gz", "b\n")
        self._plain("btc_2026-08-24.jsonl", "c\n")
        got = [p.name for p in iter_data_files(self.d, "btc_*.jsonl")]
        self.assertEqual(got, ["btc_2026-08-22.jsonl.gz",
                              "btc_2026-08-23.jsonl.gz",
                              "btc_2026-08-24.jsonl"])

    def test_iter_dedupes_when_both_forms_present(self) -> None:
        self._plain("btc_2026-08-23.jsonl", "p\n")
        self._gz("btc_2026-08-23.jsonl.gz", "g\n")
        got = iter_data_files(self.d, "btc_*.jsonl")
        self.assertEqual([p.name for p in got], ["btc_2026-08-23.jsonl"])

    def test_iter_missing_dir_is_empty(self) -> None:
        self.assertEqual(iter_data_files(self.d / "nope", "*.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
