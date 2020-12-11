# -*- coding: utf-8 -*-
from collections import OrderedDict
from datetime import timedelta, datetime
from heapq import *
import math
import django_filters
from guardian.core import ObjectPermissionChecker

from django.conf import settings
from django.forms.models import model_to_dict
from django.utils.timezone import now, get_default_timezone
from django.utils.translation import ugettext_lazy as _
from django.core.mail import send_mass_mail
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist

import rest_framework
from rest_framework import viewsets, serializers, filters, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from reservations.models import Reservable, ReservableSet, CustomSortOrder
from reservations.models import Resource, NResources, Reservation, UserProfile


@api_view(['GET'])
@permission_classes((AllowAny,))
def reservable_set_list_view(request):
    r = ReservableSet.objects.all()
    return Response({'reservable_sets': r},
                    template_name='reservations/reservable_set_list_view.html')


@api_view(['GET'])
@permission_classes((AllowAny,))
def reservable_type_list_view(request, reservable_set_slug=None):
    if reservable_set_slug is None:
        rs = None
        r = Reservable.objects.all()
    else:
        rs = ReservableSet.objects.get(slug=reservable_set_slug)
        r = rs.reservables.all()
    d = {
         'reservable_types': r.values_list('type', flat=True).distinct(),
         'reservable_set': rs,
    }
    return Response(
        d,
        template_name='reservations/reservable_type_list_view.html'
    )


@api_view(['GET'])
@permission_classes((AllowAny,))
def reservable_resource_view(request, reservable_set_slug=None,
                             reservable_type="room"):
    if reservable_set_slug is None:
        rs = None
        reservables = Reservable.objects.all()
    else:
        rs = ReservableSet.objects.get(slug=reservable_set_slug)
        reservables = rs.reservables.all()
    reservables = reservables.order_by('slug')
    reservables = reservables.filter(type=reservable_type)
    resources = Resource.objects.filter(
        reservable__type=reservable_type,
        reservable__reservableset_set=rs
    ).distinct().values('name', 'id')
    reservable_table = []
    for i in reservables:
        l = []
        for r in resources:
            try:
                n = NResources.objects.get(reservable=i,
                                           resource__id=r['id']).n
            except:
                n = 0
            l.append(n)
        reservable_table.append(({'id': i.id, 'name': i.slug}, l))
    d = {
         'reservable_table': reservable_table,
         'resources': resources,
    }
    return Response(
        d,
        template_name='reservations/reservable_resource_view.html'
    )


def round_time(t, delta):
    epoch = datetime.utcfromtimestamp(0)
    if settings.USE_TZ:
        tzinfo = get_default_timezone()
        epoch = epoch.replace(tzinfo=tzinfo)
    # limits:
    # minute, 5min, 15min, 30min, 1h, 6h, 1d, 1week, 1 month, 1 year
    # times below 1 week can be handled by the old function:
    if delta < timedelta(days=7):  # under 1 week - round to delta
        td1 = t - epoch
        sec1 = td1.total_seconds()
        sec_base = delta.total_seconds()
        final_delta = timedelta(seconds=sec1 - (sec1 % sec_base))
        t = epoch + final_delta
    else:
        # first, round to a day
        t = t - timedelta(
            hours=t.hour, minutes=t.minute, seconds=t.second,
            microseconds=t.microsecond)
        if delta < timedelta(days=27):  # under 1 month - round to the
                                        # begining of the week
            t = t - timedelta(days=1) * t.weekday()
        elif delta < timedelta(days=365):  # under a year - round to a month
            t = t - timedelta(days=1) * t.day
        else:  # over a year - round to a year
            t = timedelta(year=t.year, month=1, day=1)
    return t


def _parse_time_string(s):
    t = now()
    for i in ['%Y-%m-%d %H:%M:%S', "%d.%m.%Y", '%Y-%m-%d', '%Y-%m-%d %H:%M']:
        try:
            t = datetime.strptime(s, i)
            if settings.USE_TZ:
                tzinfo = get_default_timezone()
                t = t.replace(tzinfo=tzinfo)
        except Exception:
            pass
    return t

_ZOOMLEVELS = [
    # 4 hours, 5-min. intervals
    {
        'time_range': timedelta(hours=4),
        'step': timedelta(minutes=5),
        'time_format': {
            'start': "H:i",
            'end': "H:i",
            'zoom_in': None,
            'zoom_out': "D"
        }
    },
    # 1 day, 1-hour intervals
    {
        'time_range': timedelta(days=1),
        'step': timedelta(hours=1),
        'time_format':  {
            'start': "H:i",
            'end': "H:i",
            'zoom_in': "H:i",
            'zoom_out': "D"
        }
    },
    # 1 week, 1-hour intervals
    {
        'time_range': timedelta(weeks=1),
        'step': timedelta(hours=4),
        'time_format': {
            'start': "D H:i",
            'end': "H:i",
            'zoom_in': "D",
            'zoom_out': "d"
        }
    },
    # 1 month, 4-hour intervals
    {
        'time_range': timedelta(weeks=4),
        'step': timedelta(hours=4),
        'time_format': {
            'start': "d. b H:i",
            'end': "d.b H:i",
            'zoom_in': "D",
            'zoom_out': None
        }
     }
]


def _get_zoomlevels(zoom):
    zoom = min(len(_ZOOMLEVELS) - 1, max(zoom, 0))
    zoomlevel = _ZOOMLEVELS[zoom]
    zoomlevel_in = zoomlevel_out = {'time_range': None}
    if zoom > 0:
        zoomlevel_in = _ZOOMLEVELS[zoom - 1]
    if zoom < len(_ZOOMLEVELS) - 1:
        zoomlevel_out = _ZOOMLEVELS[zoom + 1]
    return zoomlevel, zoomlevel_in, zoomlevel_out


@api_view(['GET'])
@permission_classes((AllowAny,))
def time_view(request, reservable_type="room", reservable_set_slug=None):
    custom_sort_order = ""
    if request.user.is_authenticated():
        if not hasattr(request.user, 'reservations_profile'):
            # Create reservations profile for the current user
            order = CustomSortOrder.objects.get_or_create(name="default")[0]
            profile = UserProfile.objects.create(
                user=request.user,
                sort_order=order
            )
        else:
            profile = request.user.reservations_profile
        custom_sort_order = profile.sort_order.order

    if reservable_set_slug is None:
        reservables = Reservable.objects.all()
    else:
        reservables = ReservableSet.objects.get(
            slug=reservable_set_slug).reservables.all()
    reservables = reservables.filter(type=reservable_type)
    start_time = _parse_time_string(request.GET.get('start', "NONSENSE"))
    # Filter the reservables list according to id argument(s)
    # in the GET request.
    try:
        if 'id' in request.GET:
            reservable_ids = map(int, request.GET.getlist('id'))
            reservables = reservables.filter(id__in=reservable_ids)
    except Exception:
        pass
    zoom = int(request.GET.get('zoom', '1'))
    zoomlevel, zoomlevel_in, zoomlevel_out = _get_zoomlevels(zoom)
    time_range, step, label_fmts = (zoomlevel['time_range'],
                                    zoomlevel['step'],
                                    zoomlevel['time_format'])
    start = round_time(start_time, time_range)
    end = start + time_range
    time_busy_bitmap = [False]*int(math.ceil(time_range.total_seconds() / step.total_seconds()))
    full_res_list = []
    for r in reservables.order_by('slug'):
        reservations_list = []
        reservations = r.reservation_set.filter(
            end__gte=start, start__lt=end
            ).order_by('start', 'end').iterator()
        slot_start = start
        free_start = slot_start
        slot_end = start + step
        slot_n = 0
        running_reservations = []  # a heap ordered by reservation.end
        try:
            reservation = reservations.next()
        except:
            reservation = None
        while slot_end <= end:
            free_sum = timedelta(seconds=0)
            expired_reservations = []
            while (len(running_reservations) > 0 and
                   running_reservations[0][0] < slot_end):
                reservation_end, expired = heappop(running_reservations)
                if reservation_end > slot_start:
                    expired_reservations.append(expired)
            while reservation is not None and reservation.start < slot_end:
                heappush(running_reservations, (reservation.end, reservation))
                if free_start < reservation.start:
                    free_sum += reservation.start - max(free_start, slot_start)
                free_start = max(free_start, reservation.end)
                try:
                    reservation = reservations.next()
                except:
                    reservation = None
            free_sum += slot_end - min(max(free_start, slot_start), slot_end)
            reservation_ids = ([i.id for i in expired_reservations] +
                               [i[1].id for i in running_reservations])
            owners_set = set()
            for res in (expired_reservations +
                        [i[1] for i in running_reservations]):
                for o in res.owners.all():
                    owners_set.add(o)
            owners_list = [i.id for i in owners_set]
            if len(reservation_ids) > 0:
                time_busy_bitmap[slot_n] = True
            reservations_list.append((
                free_sum.total_seconds() / step.total_seconds(),
                reservation_ids, owners_list))
            slot_start = slot_end
            slot_end = slot_start + step
            slot_n += 1
        full_res_list.append((model_to_dict(r), reservations_list))
    pruned_times_list = []
    slot_start = slot_end = start
    span = 0
    i = 0
    empty_before = False
    while slot_end < end:
        slot_end += step
        span += 1
        if time_busy_bitmap[i]:
            if empty_before:
                pruned_times_list.append(
                    (None, slot_start, slot_end - step, span)
                )
                slot_start = slot_end - step
                span = 1
            pruned_times_list.append((i, slot_start, slot_end, span))
            slot_start = slot_end
            span = 0
        empty_before = not time_busy_bitmap[i]
        i += 1
    if empty_before:
        pruned_times_list.append((None, slot_start, slot_end - step, span))
        slot_start = slot_end - step
    res_list = []
    for r, rlist in full_res_list:
        reservations_list = []
        for i, start, end, span in pruned_times_list:
            if i is not None:
                reservations_list.append({
                    'start': start, 'end': end, 'span': span,
                    'free_percentage': rlist[i][0],
                    'reservations': rlist[i][1],
                    'owners': rlist[i][2]
                })
            else:
                reservations_list.append({
                    'start': start, 'end': end, 'span': span,
                    'free_percentage': 1.0,
                    'reservations': [],
                    'owners': []
                })
        res_list.append({
            'reservable': r,
            'reservations': reservations_list})
    zoom_in_list = []
    zoom_out_list = []
    time_list = [{'start': i[1],
                  'end': i[2],
                  'span': i[3]} for i in pruned_times_list]
    for t in time_list:
        for l, zoom_step in [(zoom_in_list, zoomlevel_in['time_range']),
                             (zoom_out_list, zoomlevel_out['time_range'])]:
            if zoom_step is not None:
                if len(l) == 0:
                    last_end = round_time(time_list[0]['start'], zoom_step)
                else:
                    last_end = l[-1]['last_end']
                if t['end'] > last_end:
                    l.append({'last_end': last_end, 'span': 0, 'ranges': []})
                ranges = l[-1]['ranges']
                l[-1]['span'] += 1
                while last_end < t['end']:
                    l[-1]['ranges'].append({
                        'start': last_end,
                        'end': last_end + zoom_step
                    })
                    last_end += zoom_step
                l[-1]['last_end'] = last_end
    d = {'res_list': res_list, 'zoom_in_list': zoom_in_list,
         'zoom_out_list': zoom_out_list, 'time_list': time_list,
         'label_fmts': label_fmts, 'step': step,
         'prev_time': start - time_range, 'next_time': start + time_range,
         'zoom': zoom, 'zoom_out': zoom + 1, 'zoom_in': zoom - 1,
         'reservable_set_slug': reservable_set_slug,
         'reservable_type': reservable_type,
         'custom_sort_order': custom_sort_order}
    return Response(d, template_name='reservations/time_view.html')


class ResourceViewSet(viewsets.ModelViewSet):
    model = Resource
    queryset = Resource.objects.all()


class ResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Resource


class ReservableNResourcesSerializer(serializers.ModelSerializer):
    resource = ResourceSerializer()

    class Meta:
        model = NResources
        fields = ('id', 'resource', 'n')


class ReservableSerializer(serializers.ModelSerializer):
    nresources_set = ReservableNResourcesSerializer(many=True, read_only=True)

    class Meta:
        model = Reservable
        fields = ('id', 'slug', 'type', 'name', 'nresources_set')


class ReservableFilter(filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        # Filter by kwargs
        if 'reservable_set_slug' in view.kwargs:
            queryset = queryset.filter(
                reservableset_set__slug=view.kwargs['reservable_set_slug']
            )

        # parse arguments from URL
        if 'type' in request.GET:
            queryset = queryset.filter(type__in=request.GET.getlist('type'))
        elif 'reservable_type' in view.kwargs:
            queryset = queryset.filter(type=view.kwargs['reservable_type'])
        if len(resource_slugs) != len(resource_values):
            return queryset.none()

        resource_slugs = request.GET.getlist('resource', [])
        resource_values = request.GET.getlist('value', [])
        for resource_slug, value in zip(resource_slugs, resource_values):
            queryset = queryset.filter(
                nresources__resource__slug=resource_slug,
                nresources__n__gte=value)
        return queryset


class FilteredReservableViewSet(viewsets.ModelViewSet):
    model = Reservable
    queryset = Reservable.objects.all()
    serializer_class = ReservableSerializer
    filter_backends = (ReservableFilter,)

    def get_template_names(self):
        return ['reservations/filtered-reservables.html']

    def list(self, request, *args, **kwargs):
        # Send data as extra data to template.
        data = dict()

        response = super(FilteredReservableViewSet, self).list(
            request, *args, **kwargs)
        if request.accepted_renderer.format == 'template':
            # Add a list of all resources as the first item in data
            data['resources'] = list(Resource.objects.all())
            response.data.insert(0, data)
        return response


class ReservableViewSet(viewsets.ModelViewSet):
    model = Reservable
    queryset = Reservable.objects.all()
    serializer_class = ReservableSerializer


class ReservableSetViewSet(viewsets.ModelViewSet):
    model = ReservableSet
    queryset = ReservableSet.objects.all()
    filter_backends = (filters.DjangoFilterBackend,)


class ReservationPermission(permissions.BasePermission):

    '''
    Return modified (by user) object or raise exception if serializer
    data is invalid.
    Return tupple object, reservables.
    Return None, None if no object is
    '''
    def get_modified_object(self, request, instance=None):
        obj, reservables = None, None
        if len(request.DATA) != 0:
            serializer = ReservationSerializer(
                data=request.DATA, instance=instance)
            if serializer.is_valid():
                reservables = Reservable.objects.filter(
                    id__in=request.DATA.getlist('reservables'))
                obj = serializer.object
        return obj, reservables

    def get_model_instance_from_kwargs(self, kwargs):
        '''
        Return a Reservation instance if 'pk' in contained in kwargs and
        there exists a database object with
        this primary key, None otherwise.
        '''
        instance = None
        try:
            pk = kwargs.get('pk', None)
            if pk is not None:
                instance = Reservation.objects.get(pk=pk)
        finally:
            return instance

    def has_permission(self, request, view):
        # Allow safe methods for everybody
        if (request.method in permissions.SAFE_METHODS):
            return True

        # Unauthenticated and anonymous users have no acces to
        # unsafe methods
        if not (request.user and request.user.is_authenticated()):
            return False

        if request.method == 'POST':
            obj, reservables = self.get_modified_object(request)

            # Always show add form for new objects
            if obj is None:
                return True
            return (
                self.has_reservables_permissions(reservables, request.user) and
                self.has_overlapping_permissions(
                    obj, reservables, request.user
                )
            )
        else:
            # For delete and put has_object_permissions will be called
            return True

    def has_object_permission(self, request, view, obj):
        # method: GET, PUT, DELETE
        if request.method in permissions.SAFE_METHODS:
            return True
        u = request.user
        if not u:
            raise rest_framework.exceptions.PermissionDenied(
                detail=_('Please login'))

        instance = Reservation.objects.get(pk=obj.pk)
        modified_obj, reservables = self.get_modified_object(request, instance)
        if modified_obj is None:
            modified_obj = obj
            reservables = obj.reservables

        # Users with manage permission on reservables can do anything
        if self.has_manage_permissions(reservables, u):
            return True

        return (u in obj.owners.all() and
                self.has_reservables_permissions(reservables, u) and
                self.has_overlapping_permissions(modified_obj, reservables, u))

    def has_reservables_permissions(self, reservables, user):
        checker = ObjectPermissionChecker(user)
        for r in reservables.all():
            if not checker.has_perm('reserve', r):
                raise rest_framework.exceptions.PermissionDenied(
                    detail=_('Insufficient privileges')
                )
        return True

    def has_manage_permissions(self, reservables, user):
        checker = ObjectPermissionChecker(user)
        for r in reservables.all():
            if not checker.has_perm('manage_reservations', r):
                return False
        return True

    def has_overlapping_permissions(self, reservation, reservables, user):
        # do NOT use method on reservation object! It checks
        # object.reservations which does not exist when called from
        # method has_permission.
        overlapping = reservation.get_overlapping_reservations(reservables)
        checker = ObjectPermissionChecker(user)
        for res in overlapping:
            for r in res.reservables.all():
                if r in (reservables.all() and
                         not checker.has_perm('double_reserve', r)):
                    raise rest_framework.exceptions.PermissionDenied(
                        detail=_('Double booking not allowed'))
        return True


class ReservationSerializer(serializers.ModelSerializer):
    reservables = serializers.PrimaryKeyRelatedField(
        many=True,
        read_only=True,
        label=Reservation.reservables.field.verbose_name,  # @UndefinedVariable
        #widget=autocomplete_light.ChoiceWidget(
        #    'ReservableAutocomplete',
        #    widget_attrs={'data-widget-maximum-values': 0}
        #)
    )
    owners = serializers.PrimaryKeyRelatedField(
        many=True, label=Reservation.owners.field.verbose_name,
        read_only=True,
        #widget=autocomplete_light.ChoiceWidget(
        #    'UserAutocomplete',
        #    widget_attrs={'data-widget-maximum-values': 0}
        #)
    )

    class Meta:
        model = Reservation
        fields = ['reason', 'start', 'end', 'owners',
                  'reservables', 'requirements', 'id']


class MyReservationsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        depth = 1


class MyReservationsViewSet(viewsets.ReadOnlyModelViewSet):
    model = Reservation
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = MyReservationsSerializer

    def get_queryset(self):
        return Reservation.objects.owned_by_user(
                self.request.user).order_by('-start')

    def get_template_names(self):
        return ['reservations/my_reservations.html']


class ReservationViewSet(viewsets.ModelViewSet):
    model = Reservation
    # TODO this seems to override the get_queryset below.
    queryset = Reservation.objects.all() 
    permission_classes = (ReservationPermission,)
#    filter_backends = (filters.DjangoFilterBackend,)
#    filter_class = ReservationFilter
    serializer_class = ReservationSerializer

    def get_queryset(self):
        queryset = Reservation.objects.all()
        start = self.request.query_params.get('start', None)
        end = self.request.query_params.get('end', None)
        reservables = self.request.query_params.getlist('reservables')
        reservableset = self.request.query_params.getlist('reservableset')
        if start is not None:
            queryset = queryset.filter(end__gt=start)
        if end is not None:
            queryset = queryset.filter(start__lt=end)
        if reservables:
            queryset = queryset.filter(reservables__pk__in=reservables)
        if reservableset:
            queryset = queryset.filter(reservable__reservableset__pk__in=reservableset)
        return queryset

    def pre_save(self, instance):
        # Save old values to object (if it exists)
        # if instance.id:
        #    old_instance = instance.__class__.objects.get(id=instance.id)
        #    instance.old_reason = old_instance.reason
        #    instance.old_start = old_instance.start
        #    instance.old_end = old_instance.end
        #    instance.old_owners = list(instance.owners.all())
        #    instance.old_reservables = list(instance.reservables.all())
        pass

    def post_save(self, obj, created):
        # Always add current user to the list of owners
        u = self.request.user
        obj.owners.add(u)

    def post_delete(self, instance):
        # from_email = 'urnik@fri.uni-lj.si'
        # reservables_email_representation = ','.join(instance.reservables.order_by('name').values_list('name', flat=True))    
        # owners_email_representation = u','.join([u'{0} {1}'.format(name, surname) for surname, name in instance.owners.values_list('last_name', 'first_name')])    
        # emails = set(instance.owners.exclude(id=self.request.user.id).values_list('email', flat=True))

        # email_body = Reservation.DELETED_RESERVATION_TEMPLATE.format(instance.reason, instance.start, instance.end, reservables_email_representation, owners_email_representation)
        # send_mass_mail((('Izbris rezervacije', email_body, from_email, [recipient]) for recipient in emails))
        pass

    def send_email_notification(self, instance, created):
        from_email = 'urnik@fri.uni-lj.si'
        reservation_url = reverse('reservation-detail', args=(instance.id,))

        new_managers_emails = set()
        persistent_managers_emails = set()
        removed_managers_emails = set()

        reservables_email_representation = ','.join(instance.reservables.order_by('name').values_list('name', flat=True))
        owners_email_representation = u','.join([u'{0} {1}'.format(name, surname) for surname, name in instance.owners.order_by('last_name', 'first_name').values_list('last_name', 'first_name')])    

        if created:
            new_managers_emails = set(instance.owners.exclude(id=self.request.user.id).values_list('email', flat=True))            
        else:
            old_manager_ids = set(owner.id for owner in instance.old_owners if owner.id != self.request.user.id)
            new_manager_ids = set(instance.owners.exclude(id=self.request.user.id).values_list('id', flat=True))

            persistent_managers = old_manager_ids.intersection(new_manager_ids)
            new_managers = new_manager_ids.difference(persistent_managers)        
            removed_managers = old_manager_ids.difference(persistent_managers)

            new_managers_emails = set(instance.owners.filter(id__in=new_managers).values_list('email', flat=True))
            persistent_managers_emails = set(instance.owners.filter(id__in=persistent_managers).values_list('email', flat=True))        
            removed_managers_emails = set([owner.email for owner in instance.old_owners if owner.id in removed_managers])

            old_reservables_email_representation = ','.join([reservable.name for reservable in sorted(instance.old_reservables, key=lambda reservable: reservable.name)])
            old_owners_email_representation = u','.join([u'{0} {1}'.format(owner.first_name, owner.last_name) for owner in sorted(instance.old_owners, key=lambda owner: u'{0} {1}'.format(owner.last_name, owner.first_name))])
            differences = u''
            if instance.reason != instance.old_reason:
                differences += u'Razlog: "{0}" namesto "{1}"\n'.format(instance.reason, instance.old_reason)
            if instance.start != instance.old_start:
                differences += u'Začetek: "{0}" namesto "{1}"\n'.format(instance.start, instance.old_start)
            if instance.end != instance.old_end:
                differences += u'Konec: "{0}" namesto "{1}"\n'.format(instance.end, instance.old_end)
            if owners_email_representation != old_owners_email_representation:
                differences += u'Lastniki: "{0}" namesto "{1}"\n'.format(owners_email_representation, old_owners_email_representation)
            if reservables_email_representation != old_reservables_email_representation:
                differences += u'Rezervirano: "{0}" namesto "{1}"\n'.format(reservables_email_representation, old_reservables_email_representation)

            email_persistent_managers = Reservation.PERSISTENT_OWNER_EMAIL_TEMPLATE.format(differences, reservation_url)
            send_mass_mail([('Sprememba rezervacije', email_persistent_managers, from_email, [recipient]) for recipient in persistent_managers_emails])

            email_removed_managers = Reservation.REMOVED_OWNER_EMAIL_TEMPLATE.format(instance.old_reason, instance.old_start, instance.old_end, old_reservables_email_representation, old_owners_email_representation, reservation_url)    
            send_mass_mail((('Odstranitev iz rezervacije', email_removed_managers, from_email, [recipient]) for recipient in removed_managers_emails))

        email_new_managers = Reservation.NEW_OWNER_EMAIL_TEMPLATE.format(instance.reason, instance.start, instance.end, reservables_email_representation, owners_email_representation, reservation_url)
        send_mass_mail((('Nova rezervacija', email_new_managers, from_email, [recipient]) for recipient in new_managers_emails))
