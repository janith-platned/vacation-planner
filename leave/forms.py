from django import forms

from .models import LeaveType


class ManagerModifyForm(forms.Form):
    """A manager changing the dates of someone's approved leave."""

    start_date = forms.DateField()
    end_date = forms.DateField()
    comment = forms.CharField(required=False, max_length=500)
    override_floor = forms.BooleanField(required=False)
    override_reason = forms.CharField(required=False, max_length=500)

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        if data.get("override_floor") and not (data.get("override_reason") or "").strip():
            self.add_error("override_reason", "Give a reason to override the floor.")
        return data


class BookLeaveForm(forms.Form):
    leave_type = forms.ModelChoiceField(queryset=LeaveType.objects.none(), empty_label=None)
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leave_type"].queryset = LeaveType.objects.filter(
            team=user.team, active=True, employee_bookable=True
        )

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return data
