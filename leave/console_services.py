"""Administrator-only operations: regions, departments, teams and manager accounts."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.models import Department, Region, SystemRole, Team, User

from .exceptions import InvalidRequest, NotAllowed
from .members import generate_temp_password
from .services import _audit


def _require_admin(actor: User) -> None:
    if actor.system_role != SystemRole.ADMIN:
        raise NotAllowed("Only an administrator can do this.")


def _named(model, label: str):
    """create / rename / delete for the two simple lookup tables, Region and Department."""

    def create(*, actor: User, name: str):
        _require_admin(actor)
        name = name.strip()
        if not name:
            raise InvalidRequest(f"Give the {label} a name.")
        try:
            with transaction.atomic():
                obj = model.objects.create(name=name)
                _audit(actor=actor, action=f"{label}.add", entity=obj, after={"name": name})
        except IntegrityError as exc:
            raise InvalidRequest(f"A {label} with that name already exists.") from exc
        return obj

    def rename(*, actor: User, obj, name: str):
        _require_admin(actor)
        name = name.strip()
        if not name:
            raise InvalidRequest(f"Give the {label} a name.")
        try:
            with transaction.atomic():
                before = obj.name
                obj.name = name
                obj.save(update_fields=["name"])
                _audit(actor=actor, action=f"{label}.rename", entity=obj, before={"name": before}, after={"name": name})
        except IntegrityError as exc:
            raise InvalidRequest(f"A {label} with that name already exists.") from exc
        return obj

    def delete(*, actor: User, obj):
        _require_admin(actor)
        in_use = obj.teams.exists() if model is Region else obj.users.exists()
        if in_use:
            raise InvalidRequest(f"This {label} is still in use, so it cannot be deleted.")
        with transaction.atomic():
            _audit(actor=actor, action=f"{label}.delete", entity=obj, before={"name": obj.name})
            obj.delete()

    return create, rename, delete


create_region, rename_region, delete_region = _named(Region, "region")
create_department, rename_department, delete_department = _named(Department, "department")


def _check_manager(manager: User) -> None:
    if not manager.is_active or manager.system_role not in (SystemRole.MANAGER, SystemRole.ADMIN):
        raise InvalidRequest("Choose an active manager or administrator.")


def create_team(*, actor: User, name: str, manager: User, region: Region) -> Team:
    """New team with the default resource roles. Its leave types and floors are then set up by its manager."""
    _require_admin(actor)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the team a name.")
    _check_manager(manager)
    try:
        with transaction.atomic():
            team = Team(name=name, manager=manager, region=region)
            team.full_clean(exclude=["working_days"])
            team.save()
            _audit(actor=actor, action="team.add", entity=team, after={"name": name, "manager": manager.id, "region": region.id})
    except ValidationError as exc:
        raise InvalidRequest(" ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    except IntegrityError as exc:
        raise InvalidRequest("A team with that name already exists.") from exc
    return team


def update_team(*, actor: User, team: Team, name: str, manager: User, region: Region) -> Team:
    """Rename a team, give it another manager, or move it to another region."""
    _require_admin(actor)
    name = name.strip()
    if not name:
        raise InvalidRequest("Give the team a name.")
    _check_manager(manager)
    before = {"name": team.name, "manager": team.manager_id, "region": team.region_id}
    team.name, team.manager, team.region = name, manager, region
    try:
        with transaction.atomic():
            team.full_clean(exclude=["working_days"])
            team.save(update_fields=["name", "manager", "region"])
            _audit(actor=actor, action="team.update", entity=team, before=before, after={"name": name, "manager": manager.id, "region": region.id})
    except ValidationError as exc:
        raise InvalidRequest(" ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    except IntegrityError as exc:
        raise InvalidRequest("A team with that name already exists.") from exc
    return team


def set_reports_to(*, actor: User, manager: User, boss: User | None) -> User:
    """Choose who a manager reports to (they approve this manager's email changes)."""
    _require_admin(actor)
    if manager.system_role != SystemRole.MANAGER:
        raise InvalidRequest("Only managers report to someone.")
    if boss is not None:
        if boss.system_role not in (SystemRole.MANAGER, SystemRole.ADMIN) or not boss.is_active:
            raise InvalidRequest("Choose an active manager or administrator.")
        seen, cur = set(), boss
        while cur is not None:  # walk up the chain: no one may end up reporting to themselves
            if cur.pk == manager.pk:
                raise InvalidRequest("That would make a reporting loop.")
            if cur.pk in seen:
                break
            seen.add(cur.pk)
            cur = cur.reports_to
    before = manager.reports_to_id
    with transaction.atomic():
        manager.reports_to = boss
        manager.save(update_fields=["reports_to"])
        _audit(actor=actor, action="manager.reports_to", entity=manager, before={"reports_to": before}, after={"reports_to": boss.pk if boss else None})
    return manager


def add_manager(*, actor: User, username: str, first_name: str, last_name: str, email: str = "", reports_to: User | None = None) -> tuple[User, str]:
    """Create a manager account (no team yet) with a temporary password."""
    _require_admin(actor)
    username = username.strip()
    if not username or not first_name.strip():
        raise InvalidRequest("A username and a first name are required.")
    password = generate_temp_password()
    try:
        with transaction.atomic():
            user = User(
                username=username, first_name=first_name.strip(), last_name=last_name.strip(), email=email.strip(),
                system_role=SystemRole.MANAGER, must_change_password=True,
                reports_to=reports_to if reports_to and reports_to.system_role in (SystemRole.MANAGER, SystemRole.ADMIN) else None,
            )
            user.full_clean(exclude=["password"])
            user.set_password(password)
            user.save()
            _audit(actor=actor, action="manager.add", entity=user)
    except ValidationError as exc:
        raise InvalidRequest(" ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    except IntegrityError as exc:
        raise InvalidRequest("That username is already taken.") from exc
    return user, password
