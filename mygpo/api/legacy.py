#
# This file is part of my.gpodder.org.
#
# my.gpodder.org is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# my.gpodder.org is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public
# License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with my.gpodder.org. If not, see <http://www.gnu.org/licenses/>.
#

from django.http import HttpResponse
from django.contrib.auth.models import User
from mygpo.api.opml import Importer, Exporter
from mygpo.api.models import Subscription, Podcast, SubscriptionAction, Device, SUBSCRIBE_ACTION, UNSUBSCRIBE_ACTION
from datetime import datetime
from django.utils.datastructures import MultiValueDictKeyError
from django.db import IntegrityError
from mygpo.logging import log

LEGACY_DEVICE_NAME = 'Legacy Device'
LEGACY_DEVICE_UID  = 'legacy'

def upload(request):
    try:
        emailaddr = request.POST['username']
        password  = request.POST['password']
        action    = request.POST['action']
        protocol  = request.POST['protocol']
        opml      = request.FILES['opml'].read()
    except MultiValueDictKeyError:
        return HttpResponse("@PROTOERROR", mimetype='text/plain')

    user = auth(emailaddr, password)
    if (not user):
        return HttpResponse('@AUTHFAIL', mimetype='text/plain')

    d, created = Device.objects.get_or_create(user=user, uid=LEGACY_DEVICE_UID,
        defaults = {'type': 'unknown', 'name': LEGACY_DEVICE_NAME})

    existing = Subscription.objects.filter(user=user, device=d)

    existing_urls = [e.podcast.url for e in existing]

    i = Importer(opml)
    podcast_urls = [p['url'] for p in i.items]

    new = [item['url'] for item in i.items if item['url'] not in existing_urls]
    rem = [e.podcast.url for e in existing if e.podcast.url not in podcast_urls]

    #remove duplicates
    new = list(set(new))
    rem = list(set(rem))

    for n in new:
        p, created = Podcast.objects.get_or_create(url=n)

        try:
            SubscriptionAction.objects.create(podcast=p,action=SUBSCRIBE_ACTION, timestamp=datetime.now(), device=d)
        except IntegrityError, e:
            log('/upload: error while adding subscription: user: %s, podcast: %s, error: %s' % (user.id, p.id, e))

    for r in rem:
        p, created = Podcast.objects.get_or_create(url=r)
        try:
            SubscriptionAction.objects.create(podcast=p, action=UNSUBSCRIBE_ACTION, timestamp=datetime.now(), device=d)
        except IntegrityError, e:
            log('/upload: error while removing subscription: user: %s, podcast: %s, error: %s' % (user.id, p.id, e))

    return HttpResponse('@SUCCESS', mimetype='text/plain')

def getlist(request):
    emailaddr = request.GET.get('username', None)
    password = request.GET.get('password', None)

    user = auth(emailaddr, password)
    if user is None:
        return HttpResponse('@AUTHFAIL', mimetype='text/plain')

    d, created = Device.objects.get_or_create(user=user, uid=LEGACY_DEVICE_UID,
        defaults = {'type': 'unknown', 'name': LEGACY_DEVICE_NAME})

    podcasts = [s.podcast for s in d.get_subscriptions()]

    # FIXME: Get username and set a proper title (e.g. "thp's subscription list")
    title = 'Your subscription list'
    exporter = Exporter(title)

    opml = exporter.generate(podcasts)

    return HttpResponse(opml, mimetype='text/xml')

def auth(emailaddr, password):
    if emailaddr is None or password is None:
        return None

    try:
        user = User.objects.get(email__exact=emailaddr)
    except User.DoesNotExist:
        return None

    if not user.check_password(password):
        return None

    return user

