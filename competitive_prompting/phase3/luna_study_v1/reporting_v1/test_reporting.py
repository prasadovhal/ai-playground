import csv
from pathlib import Path

from .. import analysis
from .run import main


def test_frozen_csv_writer_and_endpoint_gate(tmp_path, monkeypatch):
    def check():
        path = tmp_path / 'rows.csv'
        analysis.csv_write(path, [{'condition': 'C', 'cost': 0.001}])
        with path.open(newline='') as f:
            assert list(csv.DictReader(f)) == [{'condition': 'C', 'cost': '0.001'}]
        empty = tmp_path / 'empty.csv'
        analysis.csv_write(empty, [])
        assert empty.read_text() == ''
        assert analysis.Path is Path
        assert analysis.paired([], False) == []
        return 'offline'

    monkeypatch.setattr(analysis, 'analyze', check)
    assert main() == 'offline'
