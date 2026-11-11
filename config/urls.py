from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from feedback.forms import LoginForm
from .health import health, metrics, worker_health


urlpatterns = [
    path('health/', health, name='health'),
    path('health/worker/', worker_health, name='worker_health'),
    path('operations/metrics/', metrics, name='operations_metrics'),
    path('accounts/login/', auth_views.LoginView.as_view(template_name='registration/login.html', authentication_form=LoginForm), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
    path('', include('feedback.urls')),
]
