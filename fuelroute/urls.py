from django.urls import path, re_path

from trips.views import RouteMapView, RoutePlanView, index

urlpatterns = [
    path("", index, name="index"),
    # trailing slash optional: POSTing without it must not hit APPEND_SLASH's redirect error
    re_path(r"^api/v1/route-plan/?$", RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/route-plan/<uuid:plan_id>/map/", RouteMapView.as_view(), name="route-map"),
]
