from django.urls import path

from . import views

urlpatterns = [
    path("calendar/", views.calendar_counts, name="calendar-counts"),
]
