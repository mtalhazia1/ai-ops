from django import forms

from .llm import schemas
from .models import Playbook


class PlaybookForm(forms.ModelForm):
    class Meta:
        model = Playbook
        fields = ["company_name", "signature", "tone", "facts", "never_promise"]
        widgets = {
            "signature": forms.Textarea(attrs={"rows": 4}),
            "tone": forms.Textarea(attrs={"rows": 2}),
            "facts": forms.Textarea(attrs={"rows": 8}),
            "never_promise": forms.Textarea(attrs={"rows": 5}),
        }
        help_texts = {
            "signature": "Every draft ends with this, exactly.",
            "facts": "Office hours, lead times, services. The drafter may only use facts from here and the email.",
            "never_promise": "Things replies must never say. Prices and delivery dates are also blocked in code.",
        }


class DecisionForm(forms.Form):
    final_body = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), required=False, label="Reply")
    confirm = forms.BooleanField(required=False, label="Send anyway (I checked the warnings)")


BOOL_CHOICES = [("", "unknown"), ("true", "yes"), ("false", "no")]


class TriageEditForm(forms.Form):
    """Category, urgency and the extracted fields of the current category."""

    category = forms.ChoiceField(choices=[(c, c) for c in schemas.CATEGORIES])
    urgency = forms.ChoiceField(choices=[(u, u) for u in schemas.URGENCIES])

    def __init__(self, data=None, *, triage):
        initial = {"category": triage.category, "urgency": triage.urgency}
        self.spec = schemas.FIELD_SPECS.get(triage.category, {})
        for name, kind in self.spec.items():
            value = triage.fields.get(name)
            if kind == "list":
                initial[f"f_{name}"] = ", ".join(value or [])
            elif kind == "bool":
                initial[f"f_{name}"] = "" if value is None else str(value).lower()
            else:
                initial[f"f_{name}"] = "" if value is None else value
        super().__init__(data, initial=initial)
        for name, kind in self.spec.items():
            label = name.replace("_", " ").capitalize()
            key = f"f_{name}"
            if isinstance(kind, list):
                self.fields[key] = forms.ChoiceField(choices=[("", "–")] + [(k, k) for k in kind], required=False, label=label)
            elif kind == "bool":
                self.fields[key] = forms.ChoiceField(choices=BOOL_CHOICES, required=False, label=label)
            elif kind == "date":
                self.fields[key] = forms.DateField(required=False, label=label, widget=forms.DateInput(attrs={"type": "date"}))
            elif kind == "num":
                self.fields[key] = forms.FloatField(required=False, label=label)
            elif kind == "int":
                self.fields[key] = forms.IntegerField(required=False, label=label, min_value=0)
            elif kind == "list":
                self.fields[key] = forms.CharField(required=False, label=label, help_text="comma-separated")
            else:
                self.fields[key] = forms.CharField(required=False, label=label)

    def field_rows(self):
        return [self[f"f_{name}"] for name in self.spec]

    def result(self):
        """(category, urgency, fields). Fields not used by a new category are dropped."""
        data = self.cleaned_data
        category = data["category"]
        target = schemas.FIELD_SPECS.get(category, {})
        fields = {}
        for name, kind in target.items():
            if name not in self.spec:
                fields[name] = [] if kind == "list" else None
                continue
            value = data.get(f"f_{name}")
            if kind == "list":
                fields[name] = [v.strip() for v in (value or "").split(",") if v.strip()]
            elif kind == "bool":
                fields[name] = {"true": True, "false": False}.get(value)
            elif kind == "date":
                fields[name] = value.isoformat() if value else None
            else:
                fields[name] = value if value not in ("", None) else None
        urgency = "high" if category == "claim" else data["urgency"]
        return category, urgency, fields
