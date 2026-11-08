from datetime import date

from tg_market import drive_export as de


def test_safe_cell_blocks_formulas_but_keeps_numbers():
    assert de.safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert de.safe_cell("@cmd") == "'@cmd"
    assert de.safe_cell("+cmd") == "'+cmd"
    assert de.safe_cell("-5") == "-5"
    assert de.safe_cell("12,5") == "12,5"
    assert de.safe_cell("Data Analyst") == "Data Analyst"
    assert de.safe_cell(None) == ""


def test_snapshot_title_uses_month():
    assert de.snapshot_title(date(2026, 11, 8)) == "TG-ринок: дані збору 2026-11"


def test_collect_tables_reads_raw_and_out_and_sanitizes(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    (data / "posts.csv").write_text("channel,post_id\na,1\n", encoding="utf-8")
    (data / "vacancies.csv").write_text("title,company\n=SUM(1),Acme\n", encoding="utf-8")
    (data / "state.json").write_text("{}", encoding="utf-8")
    (out / "unique_vacancies.csv").write_text("﻿title,n\nQA,2\n", encoding="utf-8")
    tables = dict(de.collect_tables(data, out))
    assert list(tables) == ["posts", "vacancies", "unique_vacancies"]  # state.json не вивантажується
    assert tables["vacancies"][1][0] == "'=SUM(1)"
    assert tables["unique_vacancies"][0] == ["title", "n"]  # BOM знято


def test_main_dry_run_does_not_need_google(tmp_path, monkeypatch, capsys):
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    (data / "posts.csv").write_text("channel,post_id\na,1\nb,2\n", encoding="utf-8")
    monkeypatch.setattr(de, "DATA", data)
    monkeypatch.setattr(de, "OUT", out)
    assert de.main(["--dry-run"]) == 0
    assert "posts: 2 рядків" in capsys.readouterr().out


def test_main_without_folder_env_exits_2(tmp_path, monkeypatch, capsys):
    data, out = tmp_path / "data", tmp_path / "out"
    data.mkdir()
    out.mkdir()
    (data / "posts.csv").write_text("channel,post_id\na,1\n", encoding="utf-8")
    monkeypatch.setattr(de, "DATA", data)
    monkeypatch.setattr(de, "OUT", out)
    monkeypatch.delenv(de.FOLDER_ENV, raising=False)
    assert de.main([]) == 2
    assert de.FOLDER_ENV in capsys.readouterr().out
