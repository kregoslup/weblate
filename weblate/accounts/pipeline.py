# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2017 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
from __future__ import unicode_literals

import json
import re
import time
import unicodedata

from django.conf import settings
from django.shortcuts import redirect
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.utils.encoding import force_text
from django.utils.http import is_safe_url
from django.utils.translation import ugettext as _

from six.moves.urllib.request import Request, urlopen

from social_core.pipeline.partial import partial
from social_core.exceptions import AuthMissingParameter, AuthAlreadyAssociated

from social_django.models import Code

from weblate.accounts.notifications import (
    send_notification_email, notify_account_activity
)
from weblate.accounts.templatetags.authnames import get_auth_name
from weblate.accounts.models import VerifiedEmail
from weblate.utils import messages
from weblate.utils.validators import clean_fullname
from weblate import USER_AGENT

USERNAME_RE = r'^[\w.@+-]+$'
USERNAME_MATCHER = re.compile(USERNAME_RE)
STRIP_MATCHER = re.compile(r'[^\w\s.@+-]')
CLEANUP_MATCHER = re.compile(r'[-\s]+')


def get_github_email(access_token):
    """Get real email from GitHub"""

    request = Request('https://api.github.com/user/emails')
    request.timeout = 1.0
    request.add_header('User-Agent', USER_AGENT)
    request.add_header(
        'Authorization',
        'token {0}'.format(access_token)
    )
    handle = urlopen(request)
    data = json.loads(handle.read().decode('utf-8'))
    email = None
    for entry in data:
        # Skip not verified ones
        if not entry['verified']:
            continue
        email = entry['email']
        if entry['primary']:
            break
    return email


@partial
def reauthenticate(strategy, backend, user, social, uid, **kwargs):
    """Force authentication when adding new association."""
    if strategy.request.session.pop('reauthenticate_done', False):
        return
    if user and not social and user.has_usable_password():
        strategy.request.session['reauthenticate'] = {
            'backend': backend.name,
            'backend_verbose': get_auth_name(backend.name),
            'uid': uid,
            'done': False,
        }
        return redirect('confirm')


@partial
def require_email(backend, details, user=None, is_new=False, **kwargs):
    """Force entering email for backends which don't provide it."""

    if backend.name == 'github':
        email = get_github_email(kwargs['response']['access_token'])
        if email is not None:
            details['email'] = email

    # Remove any pending email validation codes
    if details.get('email') and backend.name == 'email':
        Code.objects.filter(email=details['email']).delete()

    if user and user.email:
        # Force validation of new email address
        if backend.name == 'email':
            return {'is_new': True}

        return

    elif is_new and not details.get('email'):
        return redirect('register')


def send_validation(strategy, backend, code, partial_token):
    """Send verification email."""
    # We need to have existing session
    if not strategy.request.session.session_key:
        strategy.request.session.create()
    strategy.request.session['registration-email-sent'] = True

    template = 'activation'
    if strategy.request.session.pop('password_reset', False):
        template = 'reset'

    url = '{0}?verification_code={1}&partial_token={2}'.format(
        reverse('social:complete', args=(backend.name,)),
        code.code,
        partial_token,
    )

    send_notification_email(
        None,
        code.email,
        template,
        info=url,
        context={
            'url': url
        }
    )


@partial
def password_reset(strategy, backend, user, social, details, weblate_action,
                   current_partial, **kwargs):
    """Set unusable password on reset."""
    if (strategy.request is not None and
            user is not None and
            weblate_action == 'reset'):
        user.set_unusable_password()
        user.save(update_fields=['password'])
        notify_account_activity(
            user,
            strategy.request,
            'reset',
            method=get_auth_name(backend.name),
            name=social.uid
        )
        # Remove partial pipeline, we do not need it
        strategy.clean_partial_pipeline(current_partial.token)
        # Store user ID
        strategy.request.session['perform_reset'] = user.pk
        # Set short session expiry
        strategy.request.session.set_expiry(90)
        # Redirect to form to change password
        return redirect('password_reset')


def verify_open(strategy, backend, user=None, **kwargs):
    """Check whether it is possible to create new user."""
    # Check whether registration is open
    if not user and not settings.REGISTRATION_OPEN:
        raise AuthMissingParameter(backend, 'disabled')

    # Avoid adding associations to demo user
    if user and settings.DEMO_SERVER and user.username == 'demo':
        raise AuthMissingParameter(backend, 'demo')

    # Ensure it's still same user
    request = strategy.request
    if request.user.pk != request.session.get('social_auth_user'):
        raise AuthMissingParameter(backend, 'user')


def cleanup_next(strategy, **kwargs):
    # This is mostly fix for lack of next validation in Python Social Auth
    # see https://github.com/python-social-auth/social-core/issues/62
    url = strategy.session_get('next')
    if url and not is_safe_url(url):
        strategy.session_set('next', None)


def store_params(strategy, user, **kwargs):
    """Store Weblate specific parameters in the pipeline."""
    # Registering user
    if user and user.is_authenticated:
        registering_user = user.pk
    else:
        registering_user = None

    # Pipeline action
    if strategy.request.session.get('password_reset', False):
        action = 'reset'
    else:
        action = 'activation'

    return {
        'weblate_action': action,
        'registering_user': registering_user,
        'weblate_expires': int(time.time() + settings.AUTH_TOKEN_VALID),
    }


def verify_username(strategy, backend, details, user=None, **kwargs):
    """Verified whether username is still free.

    It can happen that user has registered several times or other user has
    taken the username meanwhile.
    """
    if user or 'username' not in details:
        return
    if User.objects.filter(username__iexact=details['username']).exists():
        raise AuthAlreadyAssociated(
            backend,
            _('This username is already taken. Please choose another.')
        )


def revoke_mail_code(strategy, details, **kwargs):
    """Revmove old mail validation code for Python Social Auth.

    PSA keeps them around, but we really don't need them again.
    """
    data = strategy.request_data()
    if details['email'] and 'verification_code' in data:
        try:
            code = strategy.storage.code.objects.get(
                code=data['verification_code'],
                email=details['email'],
                verified=True
            )
            code.delete()
        except strategy.storage.code.DoesNotExist:
            return


def ensure_valid(strategy, backend, user, registering_user, weblate_action,
                 weblate_expires, **kwargs):
    """Ensure the activation link is still."""
    # Didn't the link expire?
    if weblate_expires < time.time():
        raise AuthMissingParameter(backend, 'expires')

    # We allow password reset for unauthenticated users
    if weblate_action == 'reset':
        if strategy.request.user.is_authenticated:
            messages.warning(
                strategy.request,
                _('You can not complete password reset while logged in!')
            )
            messages.warning(
                strategy.request,
                _('The registration link has been invalidated.')
            )
            raise AuthMissingParameter(backend, 'user')
        return

    # Add email/register should stay on same user
    if user and user.is_authenticated:
        current_user = user.pk
    else:
        current_user = None

    if current_user != registering_user:
        if registering_user is None:
            messages.warning(
                strategy.request,
                _('You can not complete registration while logged in!')
            )
        else:
            messages.warning(
                strategy.request,
                _('You can confirm your registration only while logged in!')
            )
        messages.warning(
            strategy.request,
            _('The registration link has been invalidated.')
        )

        raise AuthMissingParameter(backend, 'user')


def store_email(strategy, backend, user, social, details, **kwargs):
    """Store verified email."""
    if 'email' not in details or details['email'] is None:
        raise AuthMissingParameter(backend, 'email')
    verified, dummy = VerifiedEmail.objects.get_or_create(social=social)
    if verified.email != details['email']:
        verified.email = details['email']
        verified.save()


def notify_connect(strategy, backend, user, social, new_association=False,
                   is_new=False, **kwargs):
    """Notify about adding new link."""
    if user and not is_new:
        if new_association:
            action = 'auth-connect'
        else:
            action = 'login'
        notify_account_activity(
            user,
            strategy.request,
            action,
            method=get_auth_name(backend.name),
            name=social.uid
        )


def user_full_name(strategy, details, user=None, **kwargs):
    """Update user full name using data from provider."""
    if user:
        full_name = details.get('fullname', '').strip()

        if (not full_name and
                ('first_name' in details or 'last_name' in details)):
            first_name = details.get('first_name', '')
            last_name = details.get('last_name', '')

            if first_name and first_name not in last_name:
                full_name = '{0} {1}'.format(first_name, last_name)
            elif first_name:
                full_name = first_name
            else:
                full_name = last_name

        full_name = clean_fullname(full_name)

        # The Django User model limit is 30 chars, this should
        # be raised if we switch to custom User model
        if len(full_name) > 30:
            full_name = full_name[:30]

        if full_name and full_name != user.first_name:
            user.first_name = full_name
            strategy.storage.user.changed(user)


def slugify_username(value):
    """Clean up username

    This is based on Django slugify with exception of lowercasing

    - Converts to ascii
    - Removes not wanted chars
    - Merges whitespaces and - into single -
    """
    value = unicodedata.normalize(
        'NFKD', force_text(value)
    ).encode(
        'ascii', 'ignore'
    ).decode('ascii')

    # Return username if it matches our standards
    if USERNAME_MATCHER.match(value):
        return value

    value = STRIP_MATCHER.sub('', value).strip()
    return CLEANUP_MATCHER.sub('-', value)


def cycle_session(strategy, *args, **kwargs):
    # Change key for current session
    strategy.request.session.cycle_key()


def adjust_primary_mail(strategy, entries, user, *args, **kwargs):
    """Fix primary mail on disconnect."""
    verified = VerifiedEmail.objects.filter(
        social__user=user,
    ).exclude(
        social__in=entries
    )
    if verified.filter(email=user.email).exists():
        return

    user.email = verified[0].email
    user.save()
    messages.warning(
        strategy.request,
        _(
            'Your email no longer belongs to verified account, '
            'it has been changed to {0}.'
        ).format(
            user.email
        )
    )


def notify_disconnect(strategy, backend, entries, user, **kwargs):
    """Store verified email."""
    for social in entries:
        notify_account_activity(
            user,
            strategy.request,
            'auth-disconnect',
            method=get_auth_name(backend.name),
            name=social.uid
        )
