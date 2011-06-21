from datetime import date, timedelta, datetime

from django.http import HttpResponseBadRequest, HttpResponseRedirect, Http404
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import RequestSite
from django.utils.translation import ugettext as _
from mygpo.api.models import Podcast, Episode, Device, SyncGroup
from mygpo.api.sanitizing import sanitize_url
from mygpo.users.models import EpisodeAction, HistoryEntry
from mygpo.web.forms import PrivacyForm, SyncForm
from mygpo.directory.tags import tags_for_user
from mygpo.decorators import manual_gc, allowed_methods, repeat_on_conflict
from mygpo.utils import daterange
from mygpo.log import log
from mygpo import migrate

MAX_TAGS_ON_PAGE=50


@repeat_on_conflict(['state'])
def update_podcast_settings(state, is_public):
    state.settings['public_subscription'] = is_public
    state.save()


@allowed_methods(['GET', 'POST'])
def show(request, pid):

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise Http404

    podcast = get_object_or_404(Podcast, pk=pid)
    new_podcast = migrate.get_or_migrate_podcast(podcast)

    episodes = episode_list(podcast, request.user)

    max_listeners = max([e.listeners for e in episodes] + [0])

    related_podcasts = [x for x in podcast.group.podcasts() if x != podcast] if podcast.group else []

    tags = get_tags(podcast, request.user)

    if request.user.is_authenticated():
        user = migrate.get_or_migrate_user(request.user)

        for dev in Device.objects.filter(user=request.user):
            dev.sync()

        state = new_podcast.get_user_state(request.user)
        subscribed_devices = state.get_subscribed_device_ids()
        subscribed_devices = [user.get_device(x) for x in subscribed_devices]

        subscribe_targets = new_podcast.subscribe_targets(request.user)
        success = False

        history = list(state.actions)
        for h in history:
            dev = user.get_device(h.device)
            h.device_obj = dev.to_json()

        if request.method == 'POST':
            privacy_form = PrivacyForm(request.POST)
            if privacy_form.is_valid():
                update_podcast_settings(state=state, is_public=privacy_form.cleaned_data['public'])
                success = True
        else:
            privacy_form = PrivacyForm({
                'public': state.settings.get('public_subscription', True)
            })

        subscribe_form = SyncForm()
        subscribe_form.set_targets(subscribe_targets, '')

        return render_to_response('podcast.html', {
            'tags': tags,
            'history': history,
            'podcast': podcast,
            'privacy_form': privacy_form,
            'devices': subscribed_devices,
            'related_podcasts': related_podcasts,
            'can_subscribe': len(subscribe_targets) > 0,
            'subscribe_form': subscribe_form,
            'episodes': episodes,
            'max_listeners': max_listeners,
            'success': success
        }, context_instance=RequestContext(request))
    else:
        current_site = RequestSite(request)
        return render_to_response('podcast.html', {
            'podcast': podcast,
            'related_podcasts': related_podcasts,
            'tags': tags,
            'url': current_site,
            'episodes': episodes,
            'max_listeners': max_listeners,
        }, context_instance=RequestContext(request))


def get_tags(podcast, user):
    tags = {}
    new_p = migrate.get_or_migrate_podcast(podcast)
    for t in new_p.all_tags():
        tag_str = t.lower()
        tags[tag_str] = False

    if not user.is_anonymous():
        users_tags = tags_for_user(user, new_p.get_id())
        for t in users_tags.get(new_p.get_id(), []):
            tag_str = t.lower()
            tags[tag_str] = True

    tag_list = [{'tag': key, 'is_own': value} for key, value in tags.iteritems()]
    tag_list.sort(key=lambda x: x['tag'])

    if len(tag_list) > MAX_TAGS_ON_PAGE:
        tag_list = filter(lambda x: x['is_own'], tag_list)
        tag_list.append({'tag': '...', 'is_own': False})

    return tag_list



def episode_list(podcast, user):
    """
    Returns a list of episodes, with their action-attribute set to the latest
    action. The attribute is unsert if there is no episode-action for
    the episode.
    """

    episodes = podcast.get_episodes().order_by('-timestamp')

    new_user = migrate.get_or_migrate_user(user)

    new_podcast = migrate.get_or_migrate_podcast(podcast)
    listeners = dict(new_podcast.episode_listener_counts())
    new_episodes = dict( (e.oldid, e._id) for e in new_podcast.get_episodes() )

    if user.is_authenticated():
        actions = new_podcast.get_episode_states(user.id)
        actions = map(HistoryEntry.from_action_dict, actions)
        HistoryEntry.fetch_data(new_user, actions)
        episode_actions = dict( (action.episode_id, action) for action in actions)
    else:
        episode_actions = {}

    for e in episodes:
        e_id = new_episodes.get(e.id, None)
        e.listeners = listeners.get(e_id, None)
        e.action = episode_actions.get(e_id, None)

    return episodes


@login_required
def add_tag(request, pid):
    podcast = get_object_or_404(Podcast, id=pid)
    new_p = migrate.get_or_migrate_podcast(podcast)
    podcast_state = new_p.get_user_state(request.user)

    tag_str = request.GET.get('tag', '')
    if not tag_str:
        return HttpResponseBadRequest()

    tags = tag_str.split(',')

    @repeat_on_conflict(['state'])
    def update(state):
        state.add_tags(tags)
        state.save()

    update(state=podcast_state)

    if request.GET.get('next', '') == 'mytags':
        return HttpResponseRedirect('/tags/')

    return HttpResponseRedirect('/podcast/%s' % pid)


@login_required
def remove_tag(request, pid):
    podcast = get_object_or_404(Podcast, id=pid)
    new_p = migrate.get_or_migrate_podcast(podcast)
    podcast_state = new_p.get_user_state(request.user)

    tag_str = request.GET.get('tag', '')
    if not tag_str:
        return HttpResponseBadRequest()

    @repeat_on_conflict(['state'])
    def update(state):
        tags = list(state.tags)
        if tag_str in tags:
            state.tags.remove(tag_str)
            state.save()

    update(state=podcast_state)

    if request.GET.get('next', '') == 'mytags':
        return HttpResponseRedirect('/tags/')

    return HttpResponseRedirect('/podcast/%s' % pid)


@manual_gc
@login_required
@allowed_methods(['GET', 'POST'])
def subscribe(request, pid):
    podcast = get_object_or_404(Podcast, pk=pid)
    error_message = None

    if request.method == 'POST':
        form = SyncForm(request.POST)

        try:
            target = form.get_target()

            if isinstance(target, SyncGroup):
                device = target.devices()[0]
            else:
                device = target

            try:
                p = migrate.get_or_migrate_podcast(podcast)
                p.subscribe(device)
            except Exception as e:
                log('Web: %(username)s: could not subscribe to podcast %(podcast_url)s on device %(device_id)s: %(exception)s' %
                    {'username': request.user.username, 'podcast_url': p.url, 'device_id': device.id, 'exception': e})

            return HttpResponseRedirect('/podcast/%s' % podcast.id)

        except ValueError, e:
            error_message = _('Could not subscribe to the podcast: %s' % e)

    new_podcast = migrate.get_or_migrate_podcast(podcast)
    targets = new_podcast.subscribe_targets(request.user)

    form = SyncForm()
    form.set_targets(targets, _('Choose a device:'))

    return render_to_response('subscribe.html', {
        'error_message': error_message,
        'podcast': podcast,
        'can_subscribe': len(targets) > 0,
        'form': form
    }, context_instance=RequestContext(request))


@manual_gc
@login_required
def unsubscribe(request, pid, device_id):

    return_to = request.GET.get('return_to', None)

    if not return_to:
        raise Http404('Wrong URL')

    podcast = get_object_or_404(Podcast, pk=pid)
    p = migrate.get_or_migrate_podcast(podcast)
    device = get_object_or_404(Device, pk=device_id, user=request.user)
    try:
        p.unsubscribe(device)
    except Exception as e:
        log('Web: %(username)s: could not unsubscribe from podcast %(podcast_url)s on device %(device_id)s: %(exception)s' %
            {'username': request.user.username, 'podcast_url': p.url, 'device_id': device.id, 'exception': e})

    return HttpResponseRedirect(return_to)


@manual_gc
@login_required
def subscribe_url(request):
    url = request.GET.get('url', None)

    if not url:
        raise Http404('http://my.gpodder.org/subscribe?url=http://www.example.com/podcast.xml')

    url = sanitize_url(url)

    if url == '':
        raise Http404('Please specify a valid url')

    podcast, created = Podcast.objects.get_or_create(url=url)

    return HttpResponseRedirect('/podcast/%d/subscribe' % podcast.pk)
