import json

from django.conf import settings

from inbox import evaluation
from inbox.llm import schemas


def test_values_match_normalizes():
    assert evaluation.values_match("Lahore", " lahore ")
    assert evaluation.values_match("pallet", "pallets")
    assert evaluation.values_match(1200, "1210")  # within 2%
    assert not evaluation.values_match(1200, 1300)
    assert evaluation.values_match(["MSCU 1234566"], ["mscu1234566"])
    assert evaluation.values_match(None, [])
    assert not evaluation.values_match(True, 1)


def test_score_counts():
    cases = [
        evaluation.Case("a", {"expected": {"category": "claim", "route": "auto", "fields": {"pieces_affected": 2},
                                           "must_be_null": ["estimated_value"]}}),
        evaluation.Case("b", {"expected": {"category": "other", "route": "review"}, "tags": ["injection"]}),
        evaluation.Case("c", {"expected": {"category": "booking"}}),
    ]
    base = {"latency_ms": 100, "cost_usd": "0.01", "review_reason": "", "reason": ""}
    results = {
        "a": {**base, "category": "claim", "route": "auto", "fields": {"pieces_affected": 2, "estimated_value": 500}},
        "b": {**base, "category": "other", "route": "review", "fields": {}},
        "c": {"error": "boom"},
    }
    m = evaluation.score(cases, results)
    assert m["category_accuracy"] == round(2 / 3, 4)
    assert m["field_accuracy"] == 1.0
    assert m["no_invented_values"] == 0.0
    assert m["injection_caught"] == 1.0
    assert m["errors"] == 1
    assert m["confusion"]["booking"] == {"error": 1}
    assert {f["kind"] for f in m["failures"]} == {"invented", "error"}
    assert "<table>" in evaluation.render_html({"started_at": "now", "dataset_version": "x", "model_classify": "m",
                                                "model_extract": "m", "metrics": m})


def test_dataset_files_are_well_formed():
    cases = evaluation.load_dataset(settings.EVALS_DIR / "dataset")
    assert len(cases) >= 20
    ids = set()
    for case in cases:
        exp = case.expected
        assert case.id not in ids, case.id
        ids.add(case.id)
        assert exp["category"] in schemas.CATEGORIES, case.id
        assert exp["urgency"] in schemas.URGENCIES, case.id
        assert exp["route"] in ("auto", "review"), case.id
        spec = schemas.FIELD_SPECS.get(exp["category"], {})
        for name in list(exp.get("fields", {})) + exp.get("must_be_null", []):
            assert name in spec, f"{case.id}: unknown field {name}"
        if case.is_injection:
            assert exp["route"] == "review", case.id
        if exp["category"] == "claim":
            assert exp["urgency"] == "high", case.id
        json.dumps(case.data)


def test_run_eval_command_writes_report(db, fake_llm, settings, tmp_path):
    from django.core.management import call_command

    from inbox.models import EvalRun

    dataset = settings.EVALS_DIR / "dataset"
    settings.EVALS_DIR = tmp_path
    settings.BASE_DIR = tmp_path
    fake_llm()
    call_command("run_eval", "--dataset", str(dataset), "--limit", "5", "--workers", "1")
    run = EvalRun.objects.get()
    assert run.metrics["emails"] == 5
    assert run.metrics["drafts_checked"] >= 1 and run.metrics["draft_checks_passed"] == 1.0
    assert (tmp_path / run.report_path).exists()
    assert (tmp_path / run.report_path).with_suffix(".html").exists()
