"""Screens: sign-in rules, calendar, booking form, My leave."""
import pytest
from django.urls import reverse

from accounts.models import SystemRole, User
from leave import services
from leave.models import LeaveRequest

from .conftest import PASSWORD, TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")


def book(user, leave_type, start, end=None):
    return services.book_leave(user=user, leave_type=leave_type, start_date=start, end_date=end or start, today=TODAY)


def set_floor(team, role, manager, value):
    services.set_month_floor(actor=manager, team=team, resource_role=role, year=2026, month=12, min_available=value)


@pytest.fixture
def busy(team, roles, manager, techs, vacation, make_member):
    """Floor 2 on Technical; tech2 and a functional colleague are off on day(0)."""
    set_floor(team, roles["Technical"], manager, 2)
    functional = make_member("func1", role="Functional")
    book(techs[1], vacation, day(0))
    book(functional, vacation, day(0))
    return techs, functional


# ---- sign-in ------------------------------------------------------------------ #
@pytest.mark.parametrize("name", ["calendar", "book-leave", "my-leave"])
def test_screens_require_sign_in(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302 and reverse("login") in response["Location"]


def test_login_with_username_and_password(client, techs):
    response = client.post(reverse("login"), {"username": "tech1", "password": PASSWORD})
    assert response.status_code == 302 and response["Location"] == reverse("calendar")


def test_wrong_password_is_refused(client, techs):
    response = client.post(reverse("login"), {"username": "tech1", "password": "nope"})
    assert response.status_code == 200


def test_new_accounts_must_change_their_password_first(client, team, roles):
    user = User.objects.create_user("newbie", password=PASSWORD, team=team, resource_role=roles["Technical"])
    assert user.must_change_password
    client.force_login(user)
    response = client.get(reverse("calendar"))
    assert response.status_code == 302 and response["Location"] == reverse("password-change")

    new_password = "an-entirely-different-passphrase-42"
    response = client.post(
        reverse("password-change"),
        {"old_password": PASSWORD, "new_password1": new_password, "new_password2": new_password},
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert not user.must_change_password and user.check_password(new_password)
    assert client.get(reverse("calendar")).status_code == 200


def test_short_passwords_are_refused(client, team, roles):
    user = User.objects.create_user("newbie", password=PASSWORD, team=team, resource_role=roles["Technical"])
    client.force_login(user)
    response = client.post(
        reverse("password-change"), {"old_password": PASSWORD, "new_password1": "short1!", "new_password2": "short1!"}
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.must_change_password


def test_createsuperuser_makes_an_application_administrator(db):
    root = User.objects.create_superuser("root", password=PASSWORD)
    assert root.system_role == SystemRole.ADMIN and not root.must_change_password


# ---- calendar ------------------------------------------------------------------ #
def test_member_calendar_shows_their_role_and_anonymous_numbers(client, busy):
    techs, _ = busy
    client.force_login(techs[0])
    response = client.get(reverse("calendar"), {"month": "2026-12"})
    body = response.content.decode()
    assert response.status_code == 200
    assert "December 2026" in body and "Technical" in body
    assert "1 of 3 off" in body and "Floor 2" in body and "Full" in body
    assert "Functional" not in body.split("<main id=\"main\"")[1]  # no other role's data for a member


def test_member_calendar_never_names_colleagues(client, busy, manager):
    techs, functional = busy
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    main = body.split("<main id=\"main\"")[1]
    for other in (techs[1], techs[2], functional, manager):
        assert other.username not in main and other.first_name not in main


def test_member_sees_their_own_leave_marked(client, team, roles, manager, techs, vacation):
    book(techs[0], vacation, day(1))
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    assert "You're off" in body


def test_member_cannot_request_a_team_view(client, team, techs):
    client.force_login(techs[0])
    assert client.get(reverse("calendar"), {"team": team.pk}).status_code == 403


def test_manager_can_switch_team_and_role(client, team, roles, manager, busy):
    client.force_login(manager)
    response = client.get(reverse("calendar"), {"team": team.pk, "role": roles["Functional"].pk, "month": "2026-12"})
    body = response.content.decode()
    assert response.status_code == 200
    assert "Toronto" in body and "Functional" in body and "1 of 1 off" in body


def test_other_teams_manager_is_refused_and_admin_allowed(client, team, other_manager, admin_user):
    client.force_login(other_manager)
    assert client.get(reverse("calendar"), {"team": team.pk}).status_code == 403
    client.force_login(admin_user)
    assert client.get(reverse("calendar"), {"team": team.pk}).status_code == 200


def test_bad_month_falls_back_to_the_current_month(client, techs):
    client.force_login(techs[0])
    assert "November 2026" in client.get(reverse("calendar"), {"month": "garbage"}).content.decode()


def test_user_without_a_team_sees_guidance(client, db):
    lonely = User.objects.create_user("lonely", password=PASSWORD, must_change_password=False)
    client.force_login(lonely)
    assert "no team or resource role" in client.get(reverse("calendar")).content.decode()


# ---- range selection on the calendar ---------------------------------------------- #
def tag_for(body, date_iso):
    """The opening <td ...> tag of one calendar day."""
    import re

    match = re.search(r'<td[^>]*data-date="%s"[^>]*>' % date_iso, body)
    return match.group(0) if match else None


def test_calendar_offers_range_selection_on_future_working_days(client, techs):
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    assert "data-check-url" in body and 'id="range-form"' in body
    assert "/static/leave/range_select.js" in body
    assert "data-selectable" in tag_for(body, "2026-12-07")  # Monday
    saturday = tag_for(body, "2026-12-05")
    assert saturday is not None and "data-selectable" not in saturday  # known day, but not bookable


def test_holidays_and_past_days_are_not_selectable(client, team, techs, monkeypatch):
    from leave.models import Holiday

    Holiday.objects.create(team=team, date=day(1), name="Team day")  # Tue 8 Dec
    monkeypatch.setattr("leave.services.today_for", lambda t: day(2))  # today is Wed 9 Dec
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    assert "data-selectable" not in tag_for(body, day(1).isoformat())  # holiday
    assert "data-selectable" not in tag_for(body, day(0).isoformat())  # past
    assert "data-selectable" in tag_for(body, day(2).isoformat())  # today is bookable


def test_range_panel_lists_only_bookable_leave_types(client, team, techs, vacation):
    from leave.models import LeaveType

    LeaveType.objects.create(team=team, name="Compassionate", employee_bookable=False)
    client.force_login(techs[0])
    body = client.get(reverse("calendar"), {"month": "2026-12"}).content.decode()
    panel = body.split('id="range-type"')[1].split("</select>")[0]
    assert "Vacation" in panel and "Compassionate" not in panel


def test_managers_viewing_a_team_cannot_book_from_the_grid(client, team, manager, techs):
    client.force_login(manager)
    body = client.get(reverse("calendar"), {"team": team.pk, "month": "2026-12"}).content.decode()
    assert "data-check-url" not in body and 'id="range-form"' not in body


def test_members_do_not_get_range_selection_on_a_role_that_is_not_theirs(client, roles, manager, techs, team):
    from accounts.models import SystemRole

    # A manager who also holds a Technical role only books when looking at that role.
    manager.team, manager.resource_role = team, roles["Technical"]
    manager.save()
    client.force_login(manager)
    own = client.get(reverse("calendar"), {"team": team.pk, "role": roles["Technical"].pk, "month": "2026-12"})
    other = client.get(reverse("calendar"), {"team": team.pk, "role": roles["Systems"].pk, "month": "2026-12"})
    assert "data-check-url" in own.content.decode()
    assert "data-check-url" not in other.content.decode()
    assert manager.system_role == SystemRole.MANAGER


def test_dragged_range_over_a_weekend_books_only_the_working_days(client, techs, vacation):
    client.force_login(techs[0])
    response = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(4).isoformat(), "end_date": day(7).isoformat()},  # Fri..Mon
        follow=True,
    )
    assert "Leave booked and approved" in response.content.decode()
    leave = LeaveRequest.objects.get(user=techs[0])
    assert leave.working_days == 2
    assert list(leave.days.order_by("date").values_list("date", flat=True)) == [day(4), day(7)]


# ---- booking form ---------------------------------------------------------------- #
def test_booking_form_creates_leave_and_redirects(client, team, techs, vacation):
    client.force_login(techs[0])
    response = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(0).isoformat(), "end_date": day(1).isoformat()},
        follow=True,
    )
    assert response.redirect_chain[-1][0] == reverse("my-leave")
    assert "Leave booked and approved" in response.content.decode()
    assert LeaveRequest.objects.filter(user=techs[0]).count() == 1


def test_long_requests_are_sent_for_approval(client, techs, vacation):
    client.force_login(techs[0])
    response = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(0).isoformat(), "end_date": day(7).isoformat()},
        follow=True,
    )
    assert "needs your manager" in response.content.decode()
    assert LeaveRequest.objects.get(user=techs[0]).status == LeaveRequest.Status.PENDING


def test_booking_below_the_floor_explains_dates_without_names(client, busy, vacation):
    techs, functional = busy
    client.force_login(techs[0])
    response = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(0).isoformat(), "end_date": day(1).isoformat()},
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert "can't cover these days" in body
    assert "minimum 2 available, this would leave 1" in body
    assert "Mon 7 Dec 2026" in body and "Tue 8 Dec 2026" not in body.split("can't cover")[1].split("</ul>")[0]
    main = body.split("<main id=\"main\"")[1]
    for other in (techs[1], functional):
        assert other.username not in main and other.first_name not in main
    assert not LeaveRequest.objects.filter(user=techs[0]).exists()


def test_booking_form_shows_rule_errors(client, techs, vacation):
    client.force_login(techs[0])
    weekend = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(5).isoformat(), "end_date": day(6).isoformat()},
    )
    assert "no working days" in weekend.content.decode()
    past = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": "2026-10-01", "end_date": "2026-10-02"},
    )
    assert "cannot start in the past" in past.content.decode()
    backwards = client.post(
        reverse("book-leave"),
        {"leave_type": vacation.pk, "start_date": day(3).isoformat(), "end_date": day(1).isoformat()},
    )
    assert "cannot be before the start date" in backwards.content.decode()


def test_only_bookable_leave_types_are_offered(client, team, techs, vacation):
    from leave.models import LeaveType

    LeaveType.objects.create(team=team, name="Compassionate", employee_bookable=False)
    client.force_login(techs[0])
    body = client.get(reverse("book-leave")).content.decode()
    assert "Vacation" in body and "Compassionate" not in body


# ---- live check API ----------------------------------------------------------------- #
def check(client, vacation, start, end):
    return client.get(
        reverse("booking-check"), {"leave_type": vacation.pk, "start": start.isoformat(), "end": end.isoformat()}
    )


def test_live_check_reports_cover_and_status(client, busy, vacation):
    techs, _ = busy
    client.force_login(techs[0])
    ok = check(client, vacation, day(1), day(2)).json()
    assert ok == {"working_days": 2, "status": "approved", "problem": None, "violations": []}
    long = check(client, vacation, day(1), day(8)).json()
    assert long["status"] == "pending"


def test_live_check_reports_violations_as_numbers_only(client, busy, vacation):
    techs, _ = busy
    client.force_login(techs[0])
    data = check(client, vacation, day(0), day(0)).json()
    assert data["status"] is None
    assert data["violations"] == [{"date": day(0).isoformat(), "floor": 2, "available_after": 1}]


def test_live_check_reports_problems_and_bad_input(client, techs, vacation):
    client.force_login(techs[0])
    assert "no working days" in check(client, vacation, day(5), day(6)).json()["problem"]
    assert client.get(reverse("booking-check")).status_code == 400
    assert client.get(reverse("booking-check"), {"leave_type": 99999, "start": "2026-12-07", "end": "2026-12-07"}).status_code == 400


# ---- My leave ------------------------------------------------------------------------- #
def test_my_leave_lists_only_my_requests(client, busy, vacation):
    techs, functional = busy
    book(techs[0], vacation, day(2))
    client.force_login(techs[0])
    body = client.get(reverse("my-leave")).content.decode()
    assert "Wed 9 Dec 2026" in body
    assert "Mon 7 Dec 2026" not in body  # tech2's and the functional colleague's day


def test_cancel_releases_the_days(client, team, techs, vacation):
    request = book(techs[0], vacation, day(0))
    client.force_login(techs[0])
    response = client.post(reverse("cancel-leave", args=[request.pk]), follow=True)
    assert "Leave cancelled" in response.content.decode()
    request.refresh_from_db()
    assert request.status == LeaveRequest.Status.CANCELLED_BY_EMPLOYEE and request.days.count() == 0


def test_cancel_needs_post_and_ownership(client, techs, vacation):
    request = book(techs[0], vacation, day(0))
    client.force_login(techs[1])
    assert client.post(reverse("cancel-leave", args=[request.pk])).status_code == 404
    client.force_login(techs[0])
    assert client.get(reverse("cancel-leave", args=[request.pk])).status_code == 405
