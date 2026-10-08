from django import forms

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
