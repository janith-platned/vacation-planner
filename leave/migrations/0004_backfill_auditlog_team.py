from django.db import migrations

# Entries written before AuditLog.team existed: work out the team from the entity they describe.
# Rows whose entity is gone (for example a deleted holiday) stay NULL and are visible to administrators only.
TEAM_FIELD = {"LeaveRequest": "team_id", "Floor": "team_id", "Holiday": "team_id", "LeaveType": "team_id",
              "ResourceRole": "team_id", "User": "team_id"}


def backfill(apps, schema_editor):
    AuditLog = apps.get_model("leave", "AuditLog")
    models = {
        "LeaveRequest": apps.get_model("leave", "LeaveRequest"),
        "Floor": apps.get_model("leave", "Floor"),
        "Holiday": apps.get_model("leave", "Holiday"),
        "LeaveType": apps.get_model("leave", "LeaveType"),
        "ResourceRole": apps.get_model("accounts", "ResourceRole"),
        "User": apps.get_model("accounts", "User"),
    }
    Team = apps.get_model("accounts", "Team")
    for entity_type, model in models.items():
        field = TEAM_FIELD[entity_type]
        ids = AuditLog.objects.filter(team__isnull=True, entity_type=entity_type).values_list("entity_id", flat=True).distinct()
        for pk, team_id in model.objects.filter(pk__in=list(ids)).values_list("pk", field):
            if team_id:
                AuditLog.objects.filter(team__isnull=True, entity_type=entity_type, entity_id=pk).update(team_id=team_id)
    team_ids = set(Team.objects.values_list("pk", flat=True))
    for pk in team_ids:
        AuditLog.objects.filter(team__isnull=True, entity_type="Team", entity_id=pk).update(team_id=pk)


class Migration(migrations.Migration):
    dependencies = [("leave", "0003_auditlog_team"), ("accounts", "0003_team_notify_employee_updates_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
