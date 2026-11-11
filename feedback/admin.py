from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Category, OrgUnit, Queue, RoutingRule, User
from .services import revise_routing_rule


@admin.register(User)
class FeedbackUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Рабочие полномочия", {"fields": ("role", "extra_roles", "org_unit", "restricted_access")}),)
    list_display = ("username", "first_name", "last_name", "role", "is_active")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "active")
    search_fields = ("name",)


@admin.register(Queue)
class QueueAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "owner", "restricted")
    filter_horizontal = ("members",)


@admin.register(RoutingRule)
class RoutingRuleAdmin(admin.ModelAdmin):
    list_display = ("category", "request_type", "queue", "priority", "version", "active")
    readonly_fields = ("version", "supersedes")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request,obj) and (obj is None or obj.active)

    def save_model(self, request, obj, form, change):
        if change:
            previous = RoutingRule.objects.get(pk=obj.pk)
            condition_fields = ("category_id", "request_type", "affected_unit_id", "queue_id", "priority")
            if any(getattr(previous, key) != getattr(obj, key) for key in condition_fields):
                replacement = revise_routing_rule(actor=request.user,rule_id=obj.pk,data={key:getattr(obj,key) for key in (*condition_fields,"active")})
                obj.pk,obj.version,obj.supersedes_id = replacement.pk,replacement.version,replacement.supersedes_id
                obj._state.adding = False
                return
        super().save_model(request,obj,form,change)


admin.site.register(OrgUnit)
admin.site.site_header = "Модус Трейд — настройки"
admin.site.site_title = "Настройки Модус Трейд"
