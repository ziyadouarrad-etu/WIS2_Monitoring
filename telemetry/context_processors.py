from .views import _is_admin


def admin_panel(request):
    return {'is_admin': _is_admin(request.user)}
