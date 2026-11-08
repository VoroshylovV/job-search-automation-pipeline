"""tg_market.gold: вибірка, перевірка розмітки, зіставлення вакансій, метрики з довірчими інтервалами (без API)."""
import json

from tg_market import gold


def test_wilson_and_kappa_basics():
    lo, hi = gold.wilson(90, 100)
    assert 0.82 < lo < 0.84 and 0.94 < hi < 0.96
    assert gold.wilson(0, 0)[0] != gold.wilson(0, 0)[0]                    # NaN при n=0
    assert gold.kappa([("a", "a"), ("b", "b")]) == 1.0
    assert abs(gold.kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")])) < 1e-9   # збіг на рівні випадковості
    assert gold.kappa([]) is None


def _raw(n):
    return {f"c/{i}": {"channel": "c", "post_id": i, "datetime": f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00",
                       "text": f"текст {i}"} for i in range(n)}


def test_build_sample_is_reproducible_and_keeps_ratio():
    raw = _raw(300)
    is_vac = {k: int(k.split("/")[1]) % 3 != 0 for k in raw}            # ~2/3 вакансійних
    vacs = {k: [{"title": f"QA {k}", "is_it": "True", "direction": "qa", "level": "junior"}] for k, v in is_vac.items() if v}
    a = gold.build_sample(raw, is_vac, vacs, 100, seed=1)
    b = gold.build_sample(raw, is_vac, vacs, 100, seed=1)
    assert a == b and len(a[0]) == 100
    assert sum(1 for p in a[0] if is_vac[p["key"]]) == 70
    assert len(a[1]) == 70 and all(v["true_direction"] == "" for v in a[1])   # відповіді моделі не підказуються


def test_validate_labels_reports_row_and_allowed_values():
    posts = [{"key": "c/1", "true_is_vacancy": "maybe", "true_vacancy_count": "x"}]
    vrows = [{"key": "c/1", "true_is_it": "y", "true_direction": "backend", "true_level": "mid", "true_title": ""}]
    errs = gold.validate_labels(posts, vrows)
    assert len(errs) == 4 and "рядок 2" in errs[0]
    ok = [{"key": "c/1", "true_is_vacancy": "y", "true_vacancy_count": "2"}]
    assert gold.validate_labels(ok, [{"key": "c/1", "true_is_it": "n", "true_direction": "none", "true_level": "",
                                       "true_title": ""}]) == []


def _gp(key, vac, cnt=""):
    return {"key": key, "true_is_vacancy": vac, "true_vacancy_count": cnt}


def _gv(key, hint, it, d, lv, title=""):
    return {"key": key, "title_hint": hint, "true_title": title, "true_is_it": it, "true_direction": d, "true_level": lv}


def test_match_pairs_similar_titles_only():
    g = [{"title_hint": "Junior Data Analyst", "true_title": ""}, {"title_hint": "QA Engineer", "true_title": ""}]
    p = [{"title": "Data Analyst (Junior)"}, {"title": "Chef"}]
    pairs, missed, extra = gold.match(g, p)
    assert len(pairs) == 1 and pairs[0][1]["title"] == "Data Analyst (Junior)"
    assert missed[0]["title_hint"] == "QA Engineer" and extra[0]["title"] == "Chef"


def test_score_counts_precision_recall_and_field_accuracy():
    posts = [_gp("c/1", "y", "1"), _gp("c/2", "y", "1"), _gp("c/3", "n", "0"), _gp("c/4", "n", "0"), _gp("c/5", "")]
    vrows = [_gv("c/1", "Junior BI Analyst", "y", "analytics", "junior"),
             _gv("c/2", "QA Engineer", "y", "qa", "middle"),
             _gv("c/3", "SMM Manager", "n", "none", "")]                  # модель вигадала вакансію
    pred_is_vac = {"c/1": True, "c/2": False, "c/3": True, "c/4": False}   # c/2 пропущено, c/3 хибна
    pred_vacs = {"c/1": [{"key": "c/1", "title": "BI Analyst", "is_it": "True", "direction": "analytics", "level": "middle"}],
                 "c/3": [{"key": "c/3", "title": "SMM Manager", "is_it": "True", "direction": "marketing", "level": "junior"}]}
    metrics, errors = gold.score(posts, vrows, pred_is_vac, pred_vacs, "m")
    m = {x["metric"]: x for x in metrics}
    assert (m["post_is_vacancy_precision"]["k"], m["post_is_vacancy_precision"]["n"]) == (1, 2)
    assert (m["post_is_vacancy_recall"]["k"], m["post_is_vacancy_recall"]["n"]) == (1, 2)
    assert (m["vacancy_extraction_precision"]["k"], m["vacancy_extraction_precision"]["n"]) == (1, 2)
    assert (m["vacancy_extraction_recall"]["k"], m["vacancy_extraction_recall"]["n"]) == (1, 2)
    assert m["direction_accuracy"]["value_pct"] == 100.0 and m["level_accuracy"]["value_pct"] == 0.0
    fields = {(e["field"], e["key"]) for e in errors}
    assert ("level", "c/1") in fields and ("vacancy_missed", "c/2") in fields and ("vacancy_extra", "c/3") in fields
    assert m["post_is_vacancy_recall"]["ci95_low_pct"] < 50 < m["post_is_vacancy_recall"]["ci95_high_pct"]


def test_main_sample_refuses_to_overwrite_labels(tmp_path, monkeypatch, capsys):
    data = tmp_path
    (data / "raw").mkdir()
    rows = [{"run_id": "r", "channel": "c", "post_id": i, "datetime": "2026-10-01T00:00:00+00:00", "text": f"t{i}",
             "views": None} for i in range(10)]
    (data / "raw" / "c.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (data / "posts.csv").write_text("channel,post_id,classified\n" + "".join(f"c,{i},True\n" for i in range(10)), encoding="utf-8")
    (data / "vacancies.csv").write_text("channel,post_id,title,is_it,direction,level\n"
                                        + "".join(f"c,{i},QA {i},True,qa,junior\n" for i in range(0, 10, 2)), encoding="utf-8")
    monkeypatch.setattr(gold, "DATA", data)
    monkeypatch.setattr(gold, "GOLD", data / "gold")
    assert gold.main(["sample", "--n", "8"]) == 0
    first = (data / "gold" / "gold_posts.csv").read_text(encoding="utf-8-sig")
    assert gold.main(["sample", "--n", "8"]) == 2                          # розмітку не затираємо
    assert (data / "gold" / "gold_posts.csv").read_text(encoding="utf-8-sig") == first
    assert "уже існує" in capsys.readouterr().out
