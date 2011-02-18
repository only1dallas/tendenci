import calendar
from datetime import datetime
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.template.loader import render_to_string
from django.template.defaultfilters import date as date_filter

from haystack.query import SearchQuerySet

from base.http import Http403
from site_settings.utils import get_setting
from events.models import Event, RegistrationConfiguration, \
    Registration, Registrant, Speaker, Organizer, Type, PaymentMethod
from events.forms import EventForm, Reg8nForm, Reg8nEditForm, \
    PlaceForm, SpeakerForm, OrganizerForm, TypeForm, MessageAddForm
from events.search_indexes import EventIndex
from events.utils import save_registration, email_registrants
from perms.models import ObjectPermission
from perms.utils import get_administrators
from perms.utils import has_perm, get_notice_recipients
from event_logs.models import EventLog
from invoices.models import Invoice
from meta.models import Meta as MetaTags
from meta.forms import MetaForm
import re


try:
    from notification import models as notification
except:
    notification = None


def index(request, id=None, template_name="events/view.html"):

    if not id:
        return HttpResponseRedirect(reverse('event.month'))

    event = get_object_or_404(Event, pk=id)

    if has_perm(request.user, 'events.view_event', event):

        EventLog.objects.log(
            event_id=175000,  # view event
            event_data='%s (%d) viewed by %s' %
                (event._meta.object_name, event.pk, request.user),
            description='%s viewed' % event._meta.object_name,
            user=request.user,
            request=request,
            instance=event
        )

        try:
            speaker = event.speaker_set.all().order_by('pk')[0]
        except:
            speaker = None

        try:
            organizer = event.organizer_set.all().order_by('pk')[0]
        except:
            organizer = None

        return render_to_response(template_name, {
            'event': event,
            'speaker': speaker,
            'organizer': organizer,
            'now': datetime.now(),
            },
            context_instance=RequestContext(request))
    else:
        raise Http403


def search(request, template_name="events/search.html"):
    query = request.GET.get('q', None)
    events = Event.objects.search(query, user=request.user)

    EventLog.objects.log(
        event_id=174000,  # searched event
        event_data='%s searched by %s' % ('Event', request.user),
        description='Event searched',
        user=request.user,
        request=request,
        source='events',
    )

    return render_to_response(
        template_name,
        {'events': events, 'now': datetime.now()},
        context_instance=RequestContext(request)
    )


def icalendar(request):
    import re
    from events.utils import get_vevents
    p = re.compile(r'http(s)?://(www.)?([^/]+)')
    d = {}

    d['site_url'] = get_setting('site', 'global', 'siteurl')
    match = p.search(d['site_url'])
    if match:
        d['domain_name'] = match.group(3)
    else:
        d['domain_name'] = ""

    ics_str = "BEGIN:VCALENDAR\n"
    ics_str += "PRODID:-//Schipul Technologies//Schipul Codebase 5.0 MIMEDIR//EN\n"
    ics_str += "VERSION:2.0\n"
    ics_str += "METHOD:PUBLISH\n"
    
    # function get_vevents in events.utils
    ics_str += get_vevents(request, d)
    
    ics_str += "END:VCALENDAR\n"
    
    response = HttpResponse(ics_str)
    response['Content-Type'] = 'text/calendar'
    if d['domain_name']:
        file_name = '%s.ics' % (d['domain_name'])
    else:
        file_name = "event.ics"
    response['Content-Disposition'] = 'attachment; filename=%s' % (file_name)
    return response
    

def print_view(request, id, template_name="events/print-view.html"):
    event = get_object_or_404(Event, pk=id)    

    EventLog.objects.log(
        event_id =  175001, # print-view event-id
        event_data = '%s (%d) viewed [print] by %s' % (event._meta.object_name, event.pk, request.user),
        description = '%s viewed [print]' % event._meta.object_name,
        user = request.user,
        request = request,
        instance = event
    )

    if has_perm(request.user,'events.view_event',event):
        return render_to_response(template_name, {'event': event}, 
            context_instance=RequestContext(request))
    else:
        raise Http403
    
@login_required
def edit(request, id, form_class=EventForm, template_name="events/edit.html"):
    event = get_object_or_404(Event, pk=id)

    # tried get_or_create(); but get a keyword argument :(
    try: # look for a speaker
        speaker = event.speaker_set.all()[0]
    except: # else: create a speaker
        speaker = Speaker()
        speaker.save()
        speaker.event = [event]
        speaker.save()

    # tried get_or_create(); but get a keyword argument :(
    try: # look for an organizer
        organizer = event.organizer_set.all()[0]
    except: # else: create an organizer
        organizer = Organizer()
        organizer.save()
        organizer.event = [event]
        organizer.save()

    if has_perm(request.user,'events.change_event',event):    
        if request.method == "POST":

            form_event = form_class(request.POST, instance=event, user=request.user)
            if form_event.is_valid():
                event = form_event.save(commit=False)
                event.creator_username = request.user.username
                event.owner_username = request.user.username
                event.owner = request.user

                # set up user permission
                event.allow_user_view, event.allow_user_edit = form_event.cleaned_data['user_perms']
                
                # assign permissions
                ObjectPermission.objects.remove_all(event)
                ObjectPermission.objects.assign_group(form_event.cleaned_data['group_perms'], event)
                ObjectPermission.objects.assign(event.creator, event) 
                
                event.save()

                EventLog.objects.log(
                    event_id =  172000, # edit event
                    event_data = '%s (%d) edited by %s' % (event._meta.object_name, event.pk, request.user),
                    description = '%s edited' % event._meta.object_name,
                    user = request.user,
                    request = request,
                    instance = event,
                )

                # location validation
                form_place = PlaceForm(request.POST, instance=event.place, prefix='place')
                if form_place.is_valid():
                    place = form_place.save() # save place
                    event.place = place
                    event.save() # save event

                # speaker validation
                form_speaker = SpeakerForm(request.POST, instance=speaker, prefix='speaker')
                if form_speaker.is_valid():
                    speaker = form_speaker.save(commit=False)                   
                    speaker.event = [event]
                    speaker.save()

                # organizer validation
                form_organizer = OrganizerForm(request.POST, instance=organizer, prefix='organizer')
                if form_organizer.is_valid():
                    organizer = form_organizer.save(commit=False)                   
                    organizer.event = [event]
                    organizer.save()

                # registration configuration validation
                form_regconf = Reg8nEditForm(request.POST, instance=event.registration_configuration, prefix='regconf')
                if form_regconf.is_valid():
                    regconf = form_regconf.save() # save registration configuration
                    event.registration_configuration = regconf
                    event.save() # save event

                forms = [
                    form_event,
                    form_place,
                    form_speaker,
                    form_organizer,
                    form_regconf,
                ]

                if all([form.is_valid() for form in forms]):
                    messages.add_message(request, messages.INFO, 'Successfully updated %s' % event)
                    # notification to administrator(s) and module recipient(s)
                    
                    recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')
                    if recipients and notification:
                        notification.send_emails(recipients, 'event_added', {
                            'event':event,
                            'user':request.user,
                            'registrants_paid':event.registrants(with_balance=False),
                            'registrants_pending':event.registrants(with_balance=True),
                            'SITE_GLOBAL_SITEDISPLAYNAME': get_setting('site', 'global', 'sitedisplayname'),
                            'SITE_GLOBAL_SITEURL': get_setting('site', 'global', 'siteurl'),
                        })
                    return HttpResponseRedirect(reverse('event', args=[event.pk]))
        else:

            form_event = form_class(instance=event, user=request.user)
            form_place = PlaceForm(instance=event.place, prefix='place')
            form_speaker = SpeakerForm(instance=speaker, prefix='speaker')
            form_organizer = OrganizerForm(instance=organizer, prefix='organizer')
            form_regconf = Reg8nEditForm(instance=event.registration_configuration, prefix='regconf')

        # response
        return render_to_response(template_name, {
            'event': event,
            'multi_event_forms':[form_event,form_place,form_speaker,form_organizer,form_regconf],
            'form_event':form_event,
            'form_place':form_place,
            'form_speaker':form_speaker,
            'form_organizer':form_organizer,
            'form_regconf':form_regconf,
            }, 
            context_instance=RequestContext(request))
    else:
        raise Http403

@login_required
def edit_meta(request, id, form_class=MetaForm, template_name="events/edit-meta.html"):

    # check permission
    event = get_object_or_404(Event, pk=id)
    if not has_perm(request.user,'events.change_event',event):
        raise Http403

    defaults = {
        'title': event.get_title(),
        'description': event.get_description(),
        'keywords': event.get_keywords(),
        'canonical_url': event.get_canonical_url(),
    }
    event.meta = MetaTags(**defaults)

    if request.method == "POST":
        form = form_class(request.POST, instance=event.meta)
        if form.is_valid():
            event.meta = form.save() # save meta
            event.save() # save relationship
            
            messages.add_message(request, messages.INFO, 'Successfully updated meta for %s' % event)
             
            return HttpResponseRedirect(reverse('event', args=[event.pk]))
    else:
        form = form_class(instance=event.meta)

    return render_to_response(template_name, {'event': event, 'form':form}, 
        context_instance=RequestContext(request))

@login_required
def add(request, form_class=EventForm, template_name="events/add.html"):
    if has_perm(request.user,'events.add_event'):
        if request.method == "POST":

            form_event = form_class(request.POST, user=request.user)
            if form_event.is_valid():           
                event = form_event.save(commit=False)
                event.creator = request.user
                event.creator_username = request.user.username
                event.owner = request.user
                event.owner_username = request.user.username

                # tried get_or_create();
                try: # look for a speaker
                    speaker = event.speaker_set.all()[0]
                except: # else: create a speaker
                    speaker = Speaker()

                try: # look for an organizer
                    organizer = event.organizer_set.all()[0]
                except: # else: create an organizer
                    organizer = Organizer()

                form_place = PlaceForm(request.POST, 
                    instance=event.place, prefix='place')
                form_speaker = SpeakerForm(request.POST, 
                    instance=speaker, prefix='speaker')
                form_organizer = OrganizerForm(request.POST, 
                    instance=organizer, prefix='organizer')
                form_regconf = Reg8nEditForm(request.POST, 
                    instance=event.registration_configuration, prefix='regconf')

                forms = [
                    form_event,
                    form_place,
                    form_speaker,
                    form_organizer,
                    form_regconf,
                ]

                if all([form.is_valid() for form in forms]):

                    # pks have to exist; before making relationships
                    place = form_place.save()
                    regconf = form_regconf.save()
                    speaker = form_speaker.save()
                    organizer = form_organizer.save()
                    event.save()

                    # update supplemental
                    speaker.event = [event]
                    organizer.event = [event]

                    speaker.save() # save again
                    organizer.save() # save again

                    # update event
                    event.place = place
                    event.registration_configuration = regconf
                    
                    event.save() # save again

                    # event security
                    event.allow_user_view, event.allow_user_edit = form_event.cleaned_data['user_perms']
                    ObjectPermission.objects.assign_group(form_event.cleaned_data['group_perms'], event)
                    ObjectPermission.objects.assign(event.creator, event) 

                    EventLog.objects.log(
                        event_id =  171000, # add event
                        event_data = '%s (%d) added by %s' % (event._meta.object_name, event.pk, request.user),
                        description = '%s added' % event._meta.object_name,
                        user = request.user,
                        request = request,
                        instance = event
                    )

                    messages.add_message(request, messages.INFO, 'Successfully added %s' % event)
                    # notification to administrator(s) and module recipient(s)
                    recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')
                    if recipients and notification:
                        notification.send_emails(recipients, 'event_added', {
                            'event':event,
                            'user':request.user,
                            'registrants_paid':event.registrants(with_balance=False),
                            'registrants_pending':event.registrants(with_balance=True),
                            'SITE_GLOBAL_SITEDISPLAYNAME': get_setting('site', 'global', 'sitedisplayname'),
                            'SITE_GLOBAL_SITEURL': get_setting('site', 'global', 'siteurl'),
                        })

                    return HttpResponseRedirect(reverse('event', args=[event.pk]))

                return render_to_response(template_name, {
                    'multi_event_forms':[form_event,form_place,form_speaker,form_organizer,form_regconf],
                    'form_event':form_event,
                    'form_place':form_place,
                    'form_speaker':form_speaker,
                    'form_organizer':form_organizer,
                    'form_regconf':form_regconf,
                    }, 
                    context_instance=RequestContext(request))
        else:
            reg_inits = {
                'early_dt': datetime.now(),
                'regular_dt': datetime.now(),
                'late_dt': datetime.now(),
             }

            form_event = form_class(user=request.user)
            form_place = PlaceForm(prefix='place')
            form_speaker = SpeakerForm(prefix='speaker')
            form_organizer = OrganizerForm(prefix='organizer')
            form_regconf = Reg8nEditForm(initial=reg_inits, prefix='regconf')

            # response
            return render_to_response(template_name, {
                'multi_event_forms':[form_event,form_place,form_speaker,form_organizer,form_regconf],
                'form_event':form_event,
                'form_place':form_place,
                'form_speaker':form_speaker,
                'form_organizer':form_organizer,
                'form_regconf':form_regconf,
                }, 
                context_instance=RequestContext(request))
    else:
        raise Http403
    
@login_required
def delete(request, id, template_name="events/delete.html"):
    event = get_object_or_404(Event, pk=id)

    if has_perm(request.user,'events.delete_event'):   
        if request.method == "POST":

            eventlog = EventLog.objects.log(
                event_id =  173000, # delete event
                event_data = '%s (%d) deleted by %s' % (event._meta.object_name, event.pk, request.user),
                description = '%s deleted' % event._meta.object_name,
                user = request.user,
                request = request,
                instance = event
            )

            messages.add_message(request, messages.INFO, 'Successfully deleted %s' % event)
            if notification: notification.send_emails(get_administrators(),'event_deleted', {'object': event, 'request': request})

            # send email to admins
            recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')
            if recipients and notification:
                notification.send_emails(recipients,'event_deleted', {
                    'event':event,
                    'user':request.user,
                    'registrants_paid':event.registrants(with_balance=False),
                    'registrants_pending':event.registrants(with_balance=True),
                    'eventlog_url': reverse('event_log', args=[eventlog.pk]),
                    'SITE_GLOBAL_SITEDISPLAYNAME': get_setting('site', 'global', 'sitedisplayname'),
                    'SITE_GLOBAL_SITEURL': get_setting('site', 'global', 'siteurl'),
                })

            event.delete()

            return HttpResponseRedirect(reverse('event.search'))
    
        return render_to_response(template_name, {'event': event}, 
            context_instance=RequestContext(request))
    else:
        raise Http403# Create your views here.

def register(request, event_id=0, form_class=Reg8nForm):
        event = get_object_or_404(Event, pk=event_id)

        # check if event allows registration
        if not event.registration_configuration and \
           event.registration_configuration.enabled:
            raise Http404

        # choose template
        if event.registration_configuration.price <= 0:
            template_name = "events/reg8n/register-free.html"
        else:
            template_name = "events/reg8n/register-priced.html"

        # if logged in; use their info to register
        if request.user.is_authenticated():
            user, email = request.user, request.user.email
        else:
            user, email = None, ''

        # get registrants for this event
        registrants = Registrant.objects.filter(
            registration__event = event,
        )

        # if person is registered; show them their confirmation
        if registrants.filter(email=email).exists():
            return HttpResponseRedirect(
                reverse('event.registration_confirmation',
                args=(event_id, registrants.filter(email=email)[0].hash)),
            )

        # pull registrants based off "payment required"
        if event.registration_configuration.payment_required:
            registrants = registrants.filter(registration__invoice__balance__lte = 0)

        bad_scenarios = [
            event.end_dt < datetime.now(), # event has passed
            event.registration_configuration.late_dt < datetime.now(), # registration period has passed
            event.registration_configuration.early_dt > datetime.now(), # registration period has not started
            event.registration_configuration.limit <= registrants.count(), # registration limit exceeded
        ]

        if any(bad_scenarios):
            return HttpResponseRedirect(reverse('event', args=(event_id,)))

        if request.method == "POST":
            form = form_class(event_id, request.POST, user=user)
            if form.is_valid():
                price = form.cleaned_data['price']
                payment_method = form.cleaned_data['payment_method']

                reg_defaults = {
                    'user': user,
                    'email': form.cleaned_data.get("email", email),
                    'first_name': form.cleaned_data.get("first_name", ''),
                    'last_name': form.cleaned_data.get("last_name", ''),
                    'company_name': form.cleaned_data.get("company_name", ''),
                    'event': event,
                    'payment_method': payment_method,
                    'price': price,
                }

                # create registration record; then take payment
                # this allows someone to be registered with an outstanding balance
                # gets or creates records
                reg8n, reg8n_created = save_registration(**reg_defaults)

                site_label = get_setting('site', 'global', 'sitedisplayname')
                site_url = get_setting('site', 'global', 'siteurl')
                self_reg8n = get_setting('module', 'users', 'selfregistration')

                if reg8n_created:
                    if notification:
                        notification.send_emails(
                            [reg_defaults['email']],
                            'event_registration_confirmation', 
                            {   'site_label': site_label,
                                'site_url': site_url,
                                'self_reg8n': self_reg8n,
                                'reg8n': reg8n,
                                'event': event,
                                'price': price,
                             },
                            True, # notice object created in DB
                        )

                if reg8n.payment_method and (reg8n.payment_method.label).lower() == 'credit card' and reg8n_created:

                    invoice = Invoice.objects.get(
                        object_type = ContentType.objects.get(app_label=reg8n._meta.app_label, model=reg8n._meta.module_name),
                        object_id = reg8n.id,
                    )

                    response = HttpResponseRedirect(reverse(
                        'payments.views.pay_online', 
                        args=[invoice.id, invoice.guid]
                    ))
                else:

                    try:
                        # should always work
                        registrant = Registrant.objects.get(registration__event=event, email=user_email)
                    except:
                        registrant = None

                    if registrant:
                        if registrant.cancel_dt:
                            messages.add_message(
                                request, 
                                messages.INFO, 
                                'Your registration was canceled on %s' % date_filter(reg8n.cancel_dt))
                        elif not reg8n_created:
                            messages.add_message(
                                request, 
                                messages.INFO, 
                                'You were already registered on %s' % date_filter(reg8n.create_dt))
                    
                    response = HttpResponseRedirect(reverse(
                        'event.registration_confirmation', 
                        args=(event_id, reg8n.registrant.hash)
                    ))


            else: # else form is invalid
                response = render_to_response(template_name, {'event':event, 'form':form}, 
                context_instance=RequestContext(request))

                log_defaults = {
                    'event_id' : 431000,
                    'event_data': '%s (%d) added by %s' % (event._meta.object_name, event.pk, request.user),
                    'description': '%s registered for event %s' % (request.user, event.get_absolute_url()),
                    'user': request.user,
                    'request': request,
                    'instance': event,
                }
                EventLog.objects.log(**log_defaults)

        else: # else request.method != "POST"
            if request.user.is_authenticated():
                payment_method = PaymentMethod.objects.get(pk=3)

                if free:
                    reg_defaults = {
                       'user': user,
                       'email': user.email,
                       'first_name': user.first_name,
                       'last_name': user.last_name,
                       'company_name': '',
                       'event': event,
                       'payment_method': payment_method,
                       'price': '0.00',
                    }
                     # if free event; then register w/o payment method
                    reg8n, created = save_registration(**reg_defaults)
                    response = HttpResponseRedirect(reverse(
                        'event.registration_confirmation', 
                        args=(event_id, reg8n.registrant.hash)
                    ))
                else:
                # else prompt registrant w/ registration-form
                    form = form_class(event_id, user=request.user)
                    response = render_to_response(template_name, {'event':event, 'form':form}, 
                        context_instance=RequestContext(request))
            else: # not authenticated
                
                form = form_class(event_id, user=request.user)
                response = render_to_response(template_name, {'event':event, 'form':form}, 
                    context_instance=RequestContext(request))

        return response

def cancel_registration(request, event_id=0, reg8n_id=0, hash='', template_name="events/reg8n/cancel.html"):
    event = get_object_or_404(Event, pk=event_id)
    if reg8n_id:
        try:
            registrant = Registrant.objects.get(
                registration__event = event,
                pk = reg8n_id,
            )

            # check permission
            if not has_perm(request.user, 'events.view_registrant', registrant):
                raise Http403
        except:
            raise Http404

    elif hash:
        sqs = SearchQuerySet()
        sqs = sqs.models(Registrant)
        sqs = sqs.filter(event_pk=event.pk)
        sqs = sqs.auto_query(sqs.query.clean(hash))
        sqs = sqs.order_by("-update_dt")

        try:
            registrant = sqs[0].object
        except:
            raise Http404

    if request.method == "POST":
        # TODO: invoice updates
        registrant.cancel_dt = datetime.now()
        registrant.save()

        recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')
        if recipients and notification:
            notification.send_emails(recipients, 'event_registration_cancelled', {
                'event':event,
                'user':request.user,
                'registrants_paid':event.registrants(with_balance=False),
                'registrants_pending':event.registrants(with_balance=True),
                'SITE_GLOBAL_SITEDISPLAYNAME': get_setting('site', 'global', 'sitedisplayname'),
                'SITE_GLOBAL_SITEURL': get_setting('site', 'global', 'siteurl'),
            })

        # back to invoice
        return HttpResponseRedirect(
            reverse('event.registration_confirmation', args=[event.pk, registrant.hash]))

    return render_to_response(template_name, {
        'event': event,
        'registrant':registrant,
        'hash': hash
        }, 
        context_instance=RequestContext(request))

def month_view(request, year=None, month=None, type=None, template_name='events/month-view.html'):
    from datetime import date
    from events.utils import next_month, prev_month

    if type: # redirect to /events/month/ if type does not exist
        if not Type.objects.search('slug:%s' % type):
            return HttpResponseRedirect(reverse('event.month'))

    # default/convert month and year
    if month and year:
        month, year = int(month), int(year)
    else:
        month, year = datetime.now().month, datetime.now().year

    calendar.setfirstweekday(calendar.SUNDAY)
    Calendar = calendar.Calendar

    next_month, next_year = next_month(month, year)
    prev_month, prev_year = prev_month(month, year)

    # remove any params that aren't set (e.g. type)
    next_month_params = [i for i in (next_year, next_month, type) if i]
    prev_month_params = [i for i in (prev_year, prev_month, type) if i]

    next_month_url = reverse('event.month', args=next_month_params)
    prev_month_url = reverse('event.month', args=prev_month_params)

    month_names = calendar.month_name[month-1:month+2]
    weekdays = calendar.weekheader(10).split()
    cal = Calendar(calendar.SUNDAY).monthdatescalendar(year, month)

    types = Type.objects.all().order_by('name')

    return render_to_response(template_name, {
        'cal':cal, 
        'month':month,
        'prev_month_url':prev_month_url,
        'next_month_url':next_month_url,
        'month_names':month_names,
        'year':year,
        'weekdays':weekdays,
        'types':types,
        'type':type,
        'date': date,
        }, 
        context_instance=RequestContext(request))

def day_view(request, year=None, month=None, day=None, template_name='events/day-view.html'):

    kwargs = {
        # i'm being explicit about each date part
        # because I do not want to include the time
        'start_dt__day': day,
        'start_dt__month': month,
        'start_dt__year': year,
    }

    events = Event.objects.filter(**kwargs).order_by('start_dt')
    
    return render_to_response(template_name, {
        'events': events,
        'date': datetime(year=int(year), month=int(month), day=int(day)),
        'now':datetime.now()
        }, 
        context_instance=RequestContext(request))

@login_required
def types(request, template_name='events/types/index.html'):
    from django.forms.models import modelformset_factory
    TypeFormSet = modelformset_factory(Type, form=TypeForm, extra=2, can_delete=True)

    if request.method == 'POST':
        formset = TypeFormSet(request.POST)
        if formset.is_valid():
            formset.save()

            # log "added" event_types
            for event_type in formset.new_objects:
                EventLog.objects.log(**{
                    'event_id' : 271000,
                    'event_data': '%s (%d) added by %s' % (event_type._meta.object_name, event_type.pk, request.user),
                    'description': '%s added' % event_type._meta.object_name,
                    'user': request.user,
                    'request': request,
                    'instance': event_type,
                })

            # log "changed" event_types
            for event_type, changed_data in formset.changed_objects:
                EventLog.objects.log(**{
                    'event_id' : 272000,
                    'event_data': '%s (%d) edited by %s' % (event_type._meta.object_name, event_type.pk, request.user),
                    'description': '%s edited' % event_type._meta.object_name,
                    'user': request.user,
                    'request': request,
                    'instance': event_type,
                })

            # log "deleted" event_types
            for event_type in formset.deleted_objects:
                EventLog.objects.log(**{
                    'event_id' : 273000,
                    'event_data': '%s deleted by %s' % (event_type._meta.object_name, request.user),
                    'description': '%s deleted' % event_type._meta.object_name,
                    'user': request.user,
                    'request': request,
                    'instance': event_type,
                })

    formset = TypeFormSet()

    return render_to_response(template_name, {'formset': formset}, 
        context_instance=RequestContext(request))

@login_required
def registrant_search(request, event_id=0, template_name='events/registrants/search.html'):
    query = request.GET.get('q', None)

    event = get_object_or_404(Event, pk=event_id)
    registrants = Registrant.objects.search(
        query, user=request.user, event=event,).order_by("-update_dt")

    return render_to_response(template_name, {'event':event, 'registrants':registrants}, 
        context_instance=RequestContext(request))

@login_required
def registrant_roster(request, event_id=0, roster_view='', template_name='events/registrants/roster.html'):
    from django.db.models import Sum
    event = get_object_or_404(Event, pk=event_id)
    query = ''

    if not roster_view: # default to total page
        return HttpResponseRedirect(reverse('event.registrant.roster.total', args=[event.pk]))

    # is:paid or is:non-paid
    if 'paid' in roster_view:
        query = '"is:%s"' % roster_view

    query = '%s "is:confirmed"' % query
    registrants = Registrant.objects.search(
            query, user=request.user, event=event)

    registrations = Registration.objects.filter(event=event)

    total_sum = 0
    if roster_view != 'paid':
        total_sum = Invoice.objects.filter(
            object_id__in=registrations,
            object_type=ContentType.objects.get_for_model(
                Registration)).distinct().aggregate(Sum('total'))['total__sum'] or float()

    balance_sum = Invoice.objects.filter(
        object_id__in=registrations,
        object_type=ContentType.objects.get_for_model(Registration),
        tender_date__isnull=False).distinct().aggregate(Sum('balance'))['balance__sum'] or float()

    num_registrants_who_payed = event.registrants(with_balance=False).count()
    num_registrants_who_owe = event.registrants(with_balance=True).count()

    return render_to_response(template_name, {
        'event':event, 
        'registrants':registrants,
        'balance_sum':balance_sum,
        'total_sum':total_sum,
        'num_registrants_who_payed':num_registrants_who_payed,
        'num_registrants_who_owe':num_registrants_who_owe,
        'roster_view':roster_view,
        },
        context_instance=RequestContext(request))

@login_required
def registrant_details(request, id=0, hash='', template_name='events/registrants/details.html'):
    registrant = get_object_or_404(Registrant, pk=id)

    if has_perm(request.user,'registrants.view_registrant',registrant):
        return render_to_response(template_name, {'registrant': registrant}, 
            context_instance=RequestContext(request))
    else:
        raise Http403

def registration_confirmation(request, id=0, reg8n_id=0, hash='', 
    template_name='events/reg8n/register-confirm.html'):
    """ Registration confirmation """

    event = get_object_or_404(Event, pk=id)

    if reg8n_id:

        # URL is obvious
        # permission check

        try:
            registrant = Registrant.objects.get(
                registration__event = event,
                pk = reg8n_id,
            )

            # check permission
            if not has_perm(request.user, 'events.view_registrant', registrant):
                raise Http403

            registration = registrant.registration

        except:
            raise Http404

    elif hash:
        sqs = SearchQuerySet()
        sqs = sqs.models(Registrant)
        sqs = sqs.filter(event_pk=event.pk)
        sqs = sqs.auto_query(sqs.query.clean(hash))
        sqs = sqs.order_by("-update_dt")

        try:
            registrant = sqs[0].object
            registration = registrant.registration
        except:
            raise Http404

    return render_to_response(template_name, {
        'event':event,
        'registrant':registrant,
        'registration':registration,
        }, 
        context_instance=RequestContext(request))
    
@login_required
def message_add(request, event_id, form_class=MessageAddForm, template_name='events/message/add.html'):
    from emails.models import Email
    event = get_object_or_404(Event, pk=event_id)
    if not has_perm(request.user,'events.change_event',event): raise Http403

    if request.method == "POST":
        email = Email()
        form = form_class(event.id, request.POST, instance=email)

        if form.is_valid():

            email.sender = get_setting('site', 'global', 'siteemailnoreplyaddress')
            email.sender = email.sender or request.user.email

            email.sender_display = request.user.get_full_name()
            email.reply_to = request.user.email
            email.recipient = request.user.email
            email.content_type = "text/html"
            email.subject = '%s notice from %s' % (event.title, get_setting('site', 'global', 'sitedisplayname'))
            email.save(request.user)
            subject = email.subject

            registrant_kwargs = {}
            registrant_kwargs['payment_status'] = form.cleaned_data['payment_status']
            email_registrants(event, email, **registrant_kwargs)

            registrant_kwargs['summary'] = '<font face=""Arial"" color=""#000000"">'
            registrant_kwargs['summary'] += 'Emails sent as a result of Calendar Event Notification</font><br><br>'
            registrant_kwargs['summary'] += '<font face=""Arial"" color=""#000000"">'
            registrant_kwargs['summary'] += '<br><br>Email Sent Appears Below in Raw Format'
            registrant_kwargs['summary'] += '</font><br><br>'
            registrant_kwargs['summary'] += email.body

            # send summary
            email.subject = 'SUMMARY: %s' % email.subject
            email.body = registrant_kwargs['summary']
            email.recipient = request.user.email
            email.send()
            
            # send another copy to the site webmaster
            email.recipient = get_setting('site', 'global', 'sitewebmasteremail')
            if email.recipient:
                email.subject = 'WEBMASTER SUMMARY: %s' % email.subject
                email.body = '<h2>Site Webmaster Notification of Calendar Event Send</h2>%s' % email.body
                email.send()

            EventLog.objects.log(**{
                'event_id' : 131101,
                'event_data': '%s (%d) sent by %s to event registrants' % (email._meta.object_name, email.pk, request.user),
                'description': '%s (%d) sent to event registrants of %s' % (email._meta.object_name, email.pk, event.title),
                'user': request.user,
                'request': request,
                'instance': email,
            })
            
            messages.add_message(request, messages.INFO, 'Successfully sent email "%s" to event registrants for event "%s".' % (subject, event.title))
            
            return HttpResponseRedirect(reverse('event', args=([event_id])))
        
    else:
        openingtext = render_to_string('events/message/opening-text.txt', {'event': event}, 
            context_instance=RequestContext(request))
        form = form_class(event.id, initial={'body': openingtext})
    
    return render_to_response(template_name, {
        'event':event,
        'form': form
        },context_instance=RequestContext(request))


def registrant_export(request, event_id, roster_view=''):
    """
    Export all registration for a specific event
    """
    event = get_object_or_404(Event, pk=event_id)

    # if they can edit it, they can export it
    if not has_perm(request.user,'events.change_event',event):
        raise Http403

    import xlwt
    from ordereddict import OrderedDict
    from decimal import Decimal

    # create the excel book and sheet
    book = xlwt.Workbook(encoding='utf8')
    sheet = book.add_sheet('Registrants')

    if roster_view == 'non-paid':
        registrants = event.registrants(with_balance=True)
        file_name = event.title.strip().replace(' ','-')
        file_name = 'Event-%s-Non-Paid.xls' % re.sub(r'[^a-zA-Z0-9._]+', '', file_name)
    elif roster_view == 'paid':
        registrants = event.registrants(with_balance=False)
        file_name = event.title.strip().replace(' ','-')
        file_name = 'Event-%s-Paid.xls' % re.sub(r'[^a-zA-Z0-9._]+', '', file_name)
    else:
        registrants = event.registrants()
        file_name = event.title.strip().replace(' ','-')
        file_name = 'Event-%s-Total.xls' % re.sub(r'[^a-zA-Z0-9._]+', '', file_name)

    # the key is what the column will be in the
    # excel sheet. the value is the database lookup
    # Used OrderedDict to maintain the column order
    registrant_mappings = OrderedDict([
        ('first_name', 'first_name'),
        ('last_name', 'last_name'),
        ('phone', 'phone'),
        ('email', 'email'),
        ('registration_id', 'registration__pk'),
        ('invoice_id', 'registration__invoice__pk'),
        ('registration price', 'registration__amount_paid'),
        ('payment method', 'registration__payment_method__label'),
        ('balance', 'registration__invoice__balance'),
        ('company', 'company_name'),
        ('address', 'address'),
        ('city', 'city'),
        ('state', 'state'),
        ('zip', 'zip'),
        ('country', 'country'),
        ('date', 'create_dt'),
    ])
    registrant_lookups = registrant_mappings.values()

    # Append the heading to the list of values that will
    # go into the excel sheet
    values_list = []
    values_list.insert(0, registrant_mappings.keys())

    # excel date styles
    balance_owed_style = xlwt.easyxf('font: color-index red, bold on')
    default_style = xlwt.Style.default_style
    datetime_style = xlwt.easyxf(num_format_str='mm/dd/yyyy hh:mm')
    date_style = xlwt.easyxf(num_format_str='mm/dd/yyyy')

#    if registrations:
#        # bulk of the work happens here
#        # loop through all the registrations and append the output
#        # of values_list django method to the values_list list
#        for registration in registrations:
#            registrants = registration.registrant_set.all()
#            registrants = registrants.exclude(cancel_dt__isnull=False)
#            registrants = registrants.values_list(*registrant_lookups)
#            for registrant in registrants:
#                values_list.append(registrant)

    for registrant in registrants.values_list(*registrant_lookups):
        values_list.append(registrant)

    # Write the data enumerated to the excel sheet
    for row, row_data in enumerate(values_list):
        for col, val in enumerate(row_data):
            # styles the date/time fields
            if isinstance(val, datetime):
                style = datetime_style
            elif isinstance(val, date):
                style = date_style
            else:
                style = default_style
                
            # style the invoice balance column
            if col == 7:
                balance = val
                if not val:
                    balance = 0
                if val is None:
                    balance = 0
                if isinstance(balance,Decimal) and balance > 0:
                    style = balance_owed_style

            sheet.write(row, col, val, style=style)

    response = HttpResponse(mimetype='application/vnd.ms-excel')
    response['Content-Disposition'] = 'attachment; filename=%s' % file_name
    book.save(response)
    return response



