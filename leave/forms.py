from django import forms

from accounts.models import ResourceRole, User

from .models import LeaveType


class _EmployeeField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return f"{user.get_full_name() or user.username} ({user.resource_role.name}, {user.team.name})"


class _LeaveTypeField(forms.ModelChoiceField):
    def label_from_instance(self, leave_type):
        return f"{leave_type.name} ({leave_type.team.name})"


class LogLeaveForm(forms.Form):
    """A manager logging leave for an employee of one of their teams."""

    employee = _EmployeeField(queryset=User.objects.none())
    leave_type = _LeaveTypeField(queryset=LeaveType.objects.none(), empty_label=None)
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    comment = forms.CharField(required=False, max_length=500)
    override_floor = forms.BooleanField(required=False)
    override_reason = forms.CharField(required=False, max_length=500)

    def __init__(self, *args, teams, manager, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = (
            User.objects.filter(is_active=True, team__in=teams, resource_role__isnull=False)
            .exclude(pk=manager.pk)  # managers book their own leave from the calendar
            .select_related("team", "resource_role")
            .order_by("team__name", "first_name", "last_name", "username")
        )
        self.fields["leave_type"].queryset = (
            LeaveType.objects.filter(team__in=teams, active=True).select_related("team").order_by("team__name", "name")
        )

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        if data.get("override_floor") and not (data.get("override_reason") or "").strip():
            self.add_error("override_reason", "Give a reason to override the floor.")
        employee, leave_type = data.get("employee"), data.get("leave_type")
        if employee and leave_type and leave_type.team_id != employee.team_id:
            self.add_error("leave_type", "That leave type belongs to a different team than the employee.")
        return data


class OverrideFloorForm(forms.Form):
    """Override the floor for one role on a date or range."""

    role = forms.ModelChoiceField(queryset=None, empty_label=None)
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    min_available = forms.IntegerField(min_value=0, max_value=999)

    def __init__(self, *args, roles, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = ResourceRole.objects.filter(pk__in=[r.pk for r in roles]).order_by("name")

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        elif start and end and (end - start).days > 366:
            self.add_error("end_date", "An override cannot cover more than a year.")
        return data


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
