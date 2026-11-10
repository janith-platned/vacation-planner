from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Department, Region, ResourceRole, Team, User


@admin.register(User)
class AppUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Leave planner", {"fields": ("system_role", "team", "department", "resource_role", "must_change_password")}),
    )
    list_display = ("username", "first_name", "last_name", "system_role", "team", "department", "is_active")
    list_filter = ("system_role", "team", "department", "is_active")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "manager", "region", "approval_threshold")
    list_filter = ("manager", "region")


admin.site.register(Region)
admin.site.register(Department)
admin.site.register(ResourceRole)
