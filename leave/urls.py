from django.urls import path

from . import manage_pages, pages, views

urlpatterns = [
    path("manage/approvals/", manage_pages.approvals_page, name="approvals"),
    path("manage/approvals/<int:pk>/decide/", manage_pages.decide_request_view, name="decide-request"),
    path("manage/requests/<int:pk>/modify/", manage_pages.modify_request_view, name="modify-request"),
    path("manage/requests/<int:pk>/reject/", manage_pages.reject_approved_view, name="reject-approved"),
    path("calendar/", pages.calendar_page, name="calendar"),
    path("leave/book/", pages.book_page, name="book-leave"),
    path("leave/mine/", pages.my_leave_page, name="my-leave"),
    path("leave/<int:pk>/cancel/", pages.cancel_leave_view, name="cancel-leave"),
    path("api/calendar/", views.calendar_counts, name="calendar-counts"),
    path("api/check/", views.booking_check, name="booking-check"),
]
