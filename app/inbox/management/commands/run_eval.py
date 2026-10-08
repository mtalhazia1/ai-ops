"""python manage.py run_eval [--dataset evals/dataset] [--limit N] [--tag T] [--workers N]

Runs the production triage code (no n8n, no Gmail) over the labelled dataset and writes
evals/reports/<timestamp>.json and .html.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inbox import evaluation
from inbox.llm.client import get_llm
from inbox.llm.triage import triage
from inbox.models import EvalRun, Playbook


class Command(BaseCommand):
    help = "Evaluate triage accuracy, cost and latency on the labelled dataset."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default=str(settings.EVALS_DIR / "dataset"))
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--tag", action="append", default=[], help="only cases with this tag (repeatable)")
        parser.add_argument("--workers", type=int, default=4)
        parser.add_argument("--no-save", action="store_true", help="print metrics only; write no report")

    def handle(self, *args, **opts):
        dataset = Path(opts["dataset"])
        if not dataset.is_absolute():
            dataset = settings.BASE_DIR / dataset
        cases = evaluation.load_dataset(dataset)
        if opts["tag"]:
            cases = [c for c in cases if set(opts["tag"]) & set(c.tags)]
        if opts["limit"]:
            cases = cases[: opts["limit"]]
        if not cases:
            raise CommandError(f"no cases found in {dataset}")

        started = timezone.now()
        llm = get_llm()
        company_name = Playbook.get().company_name  # read once; worker threads stay off the DB
        self.stdout.write(f"Running {len(cases)} emails with {opts['workers']} workers...")

        def run(case):
            try:
                return case.id, triage(case.to_input(), llm, company_name).as_dict()
            except Exception as exc:  # report, don't abort the run
                return case.id, {"error": f"{type(exc).__name__}: {exc}"}

        with ThreadPoolExecutor(max_workers=max(1, opts["workers"])) as pool:
            results = dict(pool.map(run, cases))

        metrics = evaluation.score(cases, results)
        finished = timezone.now()

        self.stdout.write("")
        for line in evaluation.summary_lines(metrics):
            self.stdout.write(line)
        self.stdout.write("\nConfusion matrix (rows = expected):")
        for line in evaluation.confusion_lines(metrics):
            self.stdout.write(line)
        if metrics["failures"]:
            self.stdout.write(f"\nFailures ({len(metrics['failures'])}):")
            for f in metrics["failures"]:
                field = f" [{f['field']}]" if f.get("field") else ""
                self.stdout.write(f"  {f['id']:28} {f['kind']}{field}: expected {f.get('expected')!r}, got {f.get('actual')!r}")

        if opts["no_save"]:
            return

        report = {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "dataset_version": evaluation.dataset_version(dataset),
            "model_classify": settings.MODEL_CLASSIFY,
            "model_extract": settings.MODEL_EXTRACT,
            "metrics": metrics,
            "results": results,
        }
        reports_dir = settings.EVALS_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = started.strftime("%Y%m%d-%H%M%S")
        json_path = reports_dir / f"{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, default=str))
        json_path.with_suffix(".html").write_text(evaluation.render_html(report))
        EvalRun.objects.create(
            finished_at=finished,
            dataset_version=report["dataset_version"],
            model_classify=settings.MODEL_CLASSIFY,
            model_extract=settings.MODEL_EXTRACT,
            metrics={k: v for k, v in metrics.items() if k != "failures"},
            report_path=str(json_path.relative_to(settings.BASE_DIR)),
        )
        self.stdout.write(self.style.SUCCESS(f"\nReport: {json_path} (+ .html)"))
