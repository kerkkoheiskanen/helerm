import django_filters
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions, serializers, status, viewsets
from rest_framework.response import Response

from metarecord.models import Action, Function, Phase, Record

from ..utils import validate_uuid4
from .base import (
    ClassificationRelationSerializer,
    DetailSerializerMixin,
    HexRelatedField,
    StructuralElementSerializer
)


class RecordSerializer(StructuralElementSerializer):
    class Meta(StructuralElementSerializer.Meta):
        model = Record
        read_only_fields = ('index',)

    name = serializers.CharField(read_only=True, source='get_name')
    action = HexRelatedField(read_only=True)
    parent = HexRelatedField(read_only=True)


class ActionSerializer(StructuralElementSerializer):
    class Meta(StructuralElementSerializer.Meta):
        model = Action
        read_only_fields = ('index',)

    name = serializers.CharField(read_only=True, source='get_name')
    phase = HexRelatedField(read_only=True)
    records = RecordSerializer(many=True)


class PhaseSerializer(StructuralElementSerializer):
    class Meta(StructuralElementSerializer.Meta):
        model = Phase
        read_only_fields = ('index',)

    name = serializers.CharField(read_only=True, source='get_name')
    function = HexRelatedField(read_only=True)
    actions = ActionSerializer(many=True)


class FunctionListSerializer(StructuralElementSerializer):
    version = serializers.IntegerField(read_only=True)
    modified_by = serializers.SerializerMethodField()
    state = serializers.CharField(read_only=True)

    classification_code = serializers.ReadOnlyField(source='get_classification_code')
    classification_title = serializers.ReadOnlyField(source='get_name')

    # TODO these three are here to maintain backwards compatibility,
    # should be removed as soon as the UI doesn't need these anymore
    function_id = serializers.ReadOnlyField(source='get_classification_code')
    # there is also Function.name field which should be hidden for other than templates when this is removed
    name = serializers.ReadOnlyField(source='get_name')
    parent = serializers.SerializerMethodField()

    classification = ClassificationRelationSerializer()

    class Meta(StructuralElementSerializer.Meta):
        model = Function
        exclude = StructuralElementSerializer.Meta.exclude + ('index', 'is_template')

    def get_fields(self):
        fields = super().get_fields()

        if self.context['view'].action == 'create':
            fields['phases'] = PhaseSerializer(many=True, required=False)
        else:
            fields['phases'] = HexRelatedField(many=True, read_only=True)

        return fields

    def _create_new_version(self, function_data):
        user = self.context['request'].user
        user_data = {'created_by': user, 'modified_by': user}

        phase_data = function_data.pop('phases', [])
        function_data.update(user_data)
        function = Function.objects.create(**function_data)

        for index, phase_datum in enumerate(phase_data, 1):
            action_data = phase_datum.pop('actions', [])
            phase_datum.update(user_data)
            phase = Phase.objects.create(function=function, index=index, **phase_datum)

            for index, action_datum in enumerate(action_data, 1):
                record_data = action_datum.pop('records', [])
                action_datum.update(user_data)
                action = Action.objects.create(phase=phase, index=index, **action_datum)

                for index, record_datum in enumerate(record_data, 1):
                    record_datum.update(user_data)
                    Record.objects.create(action=action, index=index, **record_datum)

        return function

    def get_modified_by(self, obj):
        return obj._modified_by or None

    def get_parent(self, obj):
        if obj.classification and obj.classification.parent:
            parent_functions = (
                Function.objects
                .filter(classification__uuid=obj.classification.parent.uuid)
            )
            if parent_functions.exists():
                return parent_functions[0].uuid.hex
        return None

    def validate(self, data):
        new_valid_from = data.get('valid_from')
        new_valid_to = data.get('valid_to')
        if new_valid_from and new_valid_to and new_valid_from > new_valid_to:
            raise exceptions.ValidationError(_('"valid_from" cannot be after "valid_to".'))

        if not self.instance:
            if Function.objects.filter(classification=data['classification']).exists():
                raise exceptions.ValidationError(
                    _('Classification %s already has a function.') % data['classification'].uuid.hex
                )
            if not data['classification'].function_allowed:
                raise exceptions.ValidationError(
                    _('Classification %s does not allow function creation.') % data['classification'].uuid.hex
                )

        return data

    @transaction.atomic
    def create(self, validated_data):
        user = self.context['request'].user

        if not user.has_perm(Function.CAN_EDIT):
            raise exceptions.PermissionDenied(_('No permission to create.'))

        validated_data['modified_by'] = user
        new_function = self._create_new_version(validated_data)
        new_function.create_metadata_version()

        return new_function


class FunctionDetailSerializer(FunctionListSerializer):
    version_history = serializers.SerializerMethodField()

    def get_fields(self):
        fields = super().get_fields()
        fields['phases'] = PhaseSerializer(many=True)
        return fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['classification'].required = False

        if self.partial:
            self.fields['state'].read_only = False

    def validate(self, data):
        data = super().validate(data)

        if self.partial:
            if not any(field in data for field in ('state', 'valid_from', 'valid_to')):
                raise exceptions.ValidationError(_('"state", "valid_from" or "valid_to" required.'))

            new_state = data.get('state')
            if new_state:
                self.check_state_change(self.instance.state, new_state)

                if self.instance.state == Function.DRAFT and new_state != Function.DRAFT:
                    errors = self.get_attribute_validation_errors(self.instance)
                    if errors:
                        raise exceptions.ValidationError(errors)
        else:
            classification = data['classification']

            if classification.uuid != self.instance.classification.uuid:
                raise exceptions.ValidationError(
                    _('Changing classification is not allowed. Only version can be changed.')
                )

        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        user = self.context['request'].user

        if self.partial:
            allowed_fields = {'state', 'valid_from', 'valid_to'}
            data = {field: validated_data[field] for field in allowed_fields if field in validated_data}
            if not data:
                return instance
            data['modified_by'] = user

            # ignore other fields than state, valid_from and valid_to
            # and do an actual update instead of a new version
            new_function = super().update(instance, data)

            new_function.create_metadata_version()
            return new_function

        if not user.has_perm(Function.CAN_EDIT):
            raise exceptions.PermissionDenied(_('No permission to edit.'))

        if instance.state in (Function.SENT_FOR_REVIEW, Function.WAITING_FOR_APPROVAL):
            raise exceptions.ValidationError(
                _('Cannot edit while in state "sent_for_review" or "waiting_for_approval"')
            )

        if not validated_data.get('classification'):
            validated_data['classification'] = instance.classification

        validated_data['modified_by'] = user
        new_function = self._create_new_version(validated_data)
        new_function.create_metadata_version()

        return new_function

    def check_state_change(self, old_state, new_state):
        user = self.context['request'].user

        if old_state == new_state:
            return

        valid_changes = {
            Function.DRAFT: {Function.SENT_FOR_REVIEW},
            Function.SENT_FOR_REVIEW: {Function.WAITING_FOR_APPROVAL, Function.DRAFT},
            Function.WAITING_FOR_APPROVAL: {Function.APPROVED, Function.DRAFT},
            Function.APPROVED: {Function.DRAFT},
        }

        if new_state not in valid_changes[old_state]:
            raise exceptions.ValidationError({'state': [_('Invalid state change.')]})

        state_change_required_permissions = {
            Function.SENT_FOR_REVIEW: Function.CAN_EDIT,
            Function.WAITING_FOR_APPROVAL: Function.CAN_REVIEW,
            Function.APPROVED: Function.CAN_APPROVE,
        }

        relevant_state = new_state if new_state != Function.DRAFT else old_state
        required_permission = state_change_required_permissions[relevant_state]

        if not user.has_perm(required_permission):
            raise exceptions.PermissionDenied(_('No permission for the state change.'))

    def get_version_history(self, obj):
        request = self.context['request']
        functions = Function.objects.filter_for_user(request.user).filter(uuid=obj.uuid).order_by('version')
        ret = []

        for function in functions:
            version_data = {attr: getattr(function, attr) for attr in ('state', 'version', 'modified_at')}

            if not request or function.can_view_modified_by(request.user):
                version_data['modified_by'] = function.get_modified_by_display()

            ret.append(version_data)

        return ret


class FunctionFilterSet(django_filters.FilterSet):
    class Meta:
        model = Function
        fields = ('valid_at', 'version', 'classification_code', 'information_system')

    valid_at = django_filters.DateFilter(method='filter_valid_at')
    modified_at__lt = django_filters.DateTimeFilter(field_name='modified_at', lookup_expr='lt')
    modified_at__gt = django_filters.DateTimeFilter(field_name='modified_at', lookup_expr='gt')
    classification_code = django_filters.CharFilter(field_name='classification__code')
    information_system = django_filters.CharFilter(field_name='phases__actions__records__attributes__InformationSystem',
                                                   lookup_expr='icontains')

    def filter_valid_at(self, queryset, name, value):
        # if neither date is set the function is considered not valid
        queryset = queryset.exclude(Q(valid_from__isnull=True) & Q(valid_to__isnull=True))

        # null value means there is no bound in that direction
        queryset = queryset.filter(
            (Q(valid_from__isnull=True) | Q(valid_from__lte=value)) &
            (Q(valid_to__isnull=True) | Q(valid_to__gte=value))
        )
        return queryset


class FunctionViewSet(DetailSerializerMixin, viewsets.ModelViewSet):
    queryset = Function.objects.filter(is_template=False)
    queryset = queryset.select_related('modified_by', 'classification').prefetch_related('phases')
    queryset = queryset.order_by('classification__code')
    serializer_class = FunctionListSerializer
    serializer_class_detail = FunctionDetailSerializer
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    filterset_class = FunctionFilterSet
    lookup_field = 'uuid'

    def get_queryset(self):
        queryset = self.queryset.filter_for_user(self.request.user)

        if 'version' in self.request.query_params:
            return queryset

        state = self.request.query_params.get('state')
        if state == 'approved':
            return queryset.latest_approved()

        return queryset.latest_version()

    def retrieve(self, request, *args, **kwargs):
        if not validate_uuid4(self.kwargs.get('uuid')):
            raise exceptions.ValidationError(_('Invalid UUID'))

        try:
            instance = self.get_object()
        except (Function.DoesNotExist, Http404):
            instance = None

        if not instance:
            filter_kwargs = {self.lookup_field: self.kwargs[self.lookup_field]}

            if 'version' in self.request.query_params:
                filter_kwargs = {**filter_kwargs, 'version': self.request.query_params['version']}

            qs = Function.objects.filter(**filter_kwargs)

            # When unauthenticated user is requesting object, the get_object will filter out functions
            # that are not approved. Here we are checking is there requested function with any state
            # in the database, if there are we return not authenticated. This was requested feature by
            # users and product owner to notify users that they should log in.
            if qs.exists():
                raise exceptions.NotAuthenticated

            raise exceptions.NotFound

        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user

        if not instance.can_user_delete(user):
            raise exceptions.PermissionDenied(_('No permission to delete or state is not "draft".'))

        instance.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
