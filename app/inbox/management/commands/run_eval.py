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
from inbox.llm.draft import DraftInput, PlaybookData, draft_reply
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
        parser.add_argument("--no-drafts", action="store_true", help="skip drafting replies (faster, cheaper)")

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
        playbook = PlaybookData.from_model(Playbook.get())  # read once; worker threads stay off the DB
        company_name = playbook.company_name
        with_drafts = not opts["no_drafts"]
        self.stdout.write(f"Running {len(cases)} emails with {opts['workers']} workers...")

        def run(case):
            try:
                email = case.to_input()
                result = triage(email, llm, company_name)
                out = result.as_dict()
                if with_drafts and result.route == "auto":
                    d = draft_reply(DraftInput(category=result.category, fields=result.fields,
                                               missing_fields=result.missing_fields, subject=email.subject,
                                               from_name=email.from_name, lookups_available=False,
                                               source_text=f"{email.subject}\n{email.body_clean}\n{email.from_email}"),
                                    playbook, llm)
                    out["draft"] = {"ok": d.ok, "problems": d.problems, "body": d.body}
                    out["cost_usd"] = str(result.cost_usd + d.cost_usd)
                    out["latency_ms"] = result.latency_ms + d.latency_ms
                return case.id, out
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
