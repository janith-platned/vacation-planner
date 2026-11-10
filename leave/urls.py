from django.urls import path

from . import pages, views

urlpatterns = [
    path("calendar/", pages.calendar_page, name="calendar"),
    path("leave/book/", pages.book_page, name="book-leave"),
    path("leave/mine/", pages.my_leave_page, name="my-leave"),
    path("leave/<int:pk>/cancel/", pages.cancel_leave_view, name="cancel-leave"),
    path("api/calendar/", views.calendar_counts, name="calendar-counts"),
    path("api/check/", views.booking_check, name="booking-check"),
]
