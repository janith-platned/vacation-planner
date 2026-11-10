from django.contrib import admin

from .models import AuditLog, Floor, Holiday, LeaveDay, LeaveRequest, LeaveType


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "team", "leave_type", "start_date", "end_date", "working_days", "status")
    list_filter = ("status", "team", "leave_type")
    # Changes go through leave.services so floors and audit stay consistent.
    readonly_fields = [f.name for f in LeaveRequest._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "on_behalf_of", "action", "entity_type", "entity_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(LeaveType)
admin.site.register(Holiday)
admin.site.register(Floor)
admin.site.register(LeaveDay)
