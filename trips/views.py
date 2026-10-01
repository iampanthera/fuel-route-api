import json
import logging

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .services.errors import ApiError, InvalidRequest
from .services.planner import get_stored_plan, plan_trip

log = logging.getLogger("trips.views")


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _penalty(value):
    if isinstance(value, bool):
        raise InvalidRequest("'stop_penalty_usd' must be a number between 0 and 1000")
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = float("nan")
    if not (0 <= num <= 1000):
        raise InvalidRequest("'stop_penalty_usd' must be a number between 0 and 1000", {"field": "stop_penalty_usd"})
    return num


def _error_response(err: ApiError) -> JsonResponse:
    resp = JsonResponse(err.to_dict(), status=err.status)
    for k, v in err.headers.items():
        resp[k] = v
    return resp


@method_decorator(csrf_exempt, name="dispatch")
class RoutePlanView(View):
    """POST {"start": ..., "finish": ...}  or  GET ?start=...&finish=..."""

    http_method_names = ["get", "post", "options", "head"]

    def get(self, request):
        return self._handle(request.GET.get("start"), request.GET.get("finish"), request.GET.get("stop_penalty_usd"))

    def post(self, request):
        try:
            payload = json.loads(request.body or b"null", parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            return _error_response(InvalidRequest("Request body must be valid JSON"))
        if not isinstance(payload, dict):
            return _error_response(InvalidRequest("Request body must be a JSON object"))
        return self._handle(payload.get("start"), payload.get("finish"), payload.get("stop_penalty_usd"))

    def _handle(self, start, finish, stop_penalty=None):
        missing = [name for name, value in (("start", start), ("finish", finish)) if value is None]
        if missing:
            return _error_response(InvalidRequest(f"Missing field(s): {', '.join(missing)}", {"fields": missing}))
        try:
            penalty = None if stop_penalty in (None, "") else _penalty(stop_penalty)
            body = plan_trip(start, finish, stop_penalty=penalty)
            body["map_url"] = self.request.build_absolute_uri(body["map_url"])
            return JsonResponse(body)
        except ApiError as err:
            return _error_response(err)
        except Exception:  # never leak internals
            log.exception("Unhandled error while planning a trip")
            return JsonResponse(
                {"error": {"code": "INTERNAL_ERROR", "message": "Unexpected server error", "details": {}}},
                status=500,
            )


class RouteMapView(View):
    http_method_names = ["get", "head"]

    def get(self, request, plan_id):
        plan = get_stored_plan(str(plan_id))
        if plan is None:
            raise Http404("Plan not found or expired; request the route again.")
        return render(request, "trips/map.html", {"plan": plan, "plan_json": plan})


def index(request):
    return render(request, "trips/index.html")
