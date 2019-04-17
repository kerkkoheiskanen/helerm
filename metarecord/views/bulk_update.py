from django.core.exceptions import PermissionDenied, ObjectDoesNotExist, ValidationError
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from metarecord.models import Function
from metarecord.models.bulk_update import BulkUpdate


class BulkUpdateSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(format='hex', read_only=True)
    changes = serializers.DictField(required=False)
    is_approved = serializers.BooleanField(read_only=True)
    modified_by = serializers.SerializerMethodField()

    class Meta:
        model = BulkUpdate
        ordering = ('created_at',)
        exclude = ('created_by',)

    def get_fields(self):
        fields = super().get_fields()

        if 'request' not in self._context:
            return fields

        user = self._context['request'].user

        if not user.has_perm(Function.CAN_VIEW_MODIFIED_BY):
            del fields['modified_by']

        return fields

    def _get_user_name_display(self, user):
        return '{} {}'.format(user.first_name, user.last_name).strip()

    def get_modified_by(self, obj):
        user = self.context['request'].user

        if obj.modified_by and user.has_perm(Function.CAN_VIEW_MODIFIED_BY):
            return self._get_user_name_display(obj.modified_by)

        return None

    def create(self, validated_data):
        user = self.context['request'].user

        if not user.has_perm(BulkUpdate.CAN_ADD):
            raise PermissionDenied(_('No permission to create bulk update'))

        user_data = {'created_by': user, 'modified_by': user}
        validated_data.update(user_data)

        return super().create(validated_data)

    def update(self, instance, validated_data):
        user = self.context['request'].user

        if not user.has_perm(BulkUpdate.CAN_CHANGE):
            raise PermissionDenied(_('No permission to update bulk update'))

        user_data = {'modified_by': user}
        validated_data.update(user_data)

        return super().update(instance, validated_data)


class BulkUpdateViewSet(viewsets.ModelViewSet):
    queryset = BulkUpdate.objects.all().prefetch_related('functions').order_by('created_at')
    serializer_class = BulkUpdateSerializer
    lookup_field = 'pk'

    def get_queryset(self):
        queryset = self.queryset

        if 'include_approved' not in self.request.query_params:
            queryset = queryset.exclude(is_approved=True)

        return queryset

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user

        if not user.has_perm('metarecord.delete_bulkupdate'):
            raise PermissionDenied(_('No permission to delete bulk update'))

        instance.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        user = request.user
        instance = self.get_object()

        try:
            instance.approve(user)
        except (ObjectDoesNotExist, ValidationError) as e:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={'errors': e})

        return Response(status=status.HTTP_200_OK)
