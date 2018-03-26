from __future__ import absolute_import, print_function

import logging

from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from sentry.app import locks
from sentry.auth.provider import MigratingIdentityId
from sentry.auth.exceptions import IdentityNotValid
from sentry.models import (
    AuditLogEntry, AuditLogEntryEvent, AuthIdentity, AuthProvider, Organization, OrganizationMember,
    OrganizationMemberTeam, User, UserEmail
)
from sentry.tasks.auth import email_missing_links
from sentry.utils import auth, metrics
from sentry.utils.redis import clusters
from sentry.utils.hashlib import md5_text
from sentry.utils.http import absolute_uri
from sentry.utils.retries import TimedRetryPolicy
from sentry.web.forms.accounts import AuthenticationForm
from sentry.web.helpers import render_to_response
import sentry.utils.json as json

from . import manager

OK_LINK_IDENTITY = _('You have successfully linked your account to your SSO provider.')

OK_SETUP_SSO = _(
    'SSO has been configured for your organization and any existing members have been sent an email to link their accounts.'
)

ERR_UID_MISMATCH = _('There was an error encountered during authentication.')

ERR_NOT_AUTHED = _('You must be authenticated to link accounts.')

ERR_INVALID_IDENTITY = _('The provider did not return a valid user identity.')


class RedisBackedState(object):
    # Expire the pipeline after 10 minutes of inactivity.
    EXPIRATION_TTL = 10 * 60

    def __init__(self, request):
        self.__dict__['request'] = request

    @property
    def _client(self):
        return clusters.get('default').get_local_client_for_key(self.auth_key)

    @property
    def auth_key(self):
        return self.request.session.get('auth_key')

    def regenerate(self, initial_state):
        auth_key = 'auth:pipeline:{}'.format(uuid4().hex)

        self.request.session['auth_key'] = auth_key
        self.request.session.modified = True

        value = json.dumps(initial_state)
        self._client.setex(auth_key, self.EXPIRATION_TTL, value)

    def clear(self):
        if not self.auth_key:
            return

        self._client.delete(self.auth_key)
        del self.request.session['auth_key']
        self.request.session.modified = True

    def is_valid(self):
        return self.auth_key and self._client.get(self.auth_key)

    def get_state(self):
        if not self.auth_key:
            return None

        state_json = self._client.get(self.auth_key)
        if not state_json:
            return None

        return json.loads(state_json)

    def __getattr__(self, key):
        state = self.get_state()
        return state[key] if state else None

    def __setattr__(self, key, value):
        state = self.get_state()
        if not state:
            return

        state[key] = value
        self._client.setex(self.auth_key, self.EXPIRATION_TTL, json.dumps(state))


class AuthHelper(object):
    """
    Helper class which is passed into AuthView's.

    Designed to link provider and views as well as manage the state and
    pipeline.

    Auth has several flows:

    1. The user is going through provider setup, thus enforcing that they link
       their current account to the new auth identity.
    2. The user is anonymous and creating a brand new account.
    3. The user is anonymous and logging into an existing account.
    4. The user is anonymous and creating a brand new account, but may have an
       existing account that should/could be merged.
    5. The user is authenticated and creating a new identity, thus associating
       it with their current account.
    6. The user is authenticated and creating a new identity, but not linking
       it with their account (thus creating a new account).
    """
    # logging in or registering
    FLOW_LOGIN = 1
    # configuring the provider
    FLOW_SETUP_PROVIDER = 2

    @classmethod
    def get_for_request(cls, request):
        state = RedisBackedState(request)
        if not state.is_valid():
            return None

        organization_id = state.org_id
        if not organization_id:
            logging.info('Invalid SSO data found')
            return None

        flow = state.flow
        auth_provider_id = state.auth_provider
        provider_key = state.provider_key
        if auth_provider_id:
            auth_provider = AuthProvider.objects.get(id=auth_provider_id)
        elif provider_key:
            auth_provider = None

        organization = Organization.objects.get(id=state.org_id)

        return cls(
            request, organization, flow, auth_provider=auth_provider, provider_key=provider_key
        )

    def __init__(self, request, organization, flow, auth_provider=None, provider_key=None):
        assert provider_key or auth_provider

        self.request = request
        self.auth_provider = auth_provider
        self.organization = organization
        self.flow = flow
        self.state = RedisBackedState(request)

        if auth_provider:
            provider = auth_provider.get_provider()
        elif provider_key:
            provider = manager.get(provider_key)
        else:
            raise NotImplementedError

        self.provider = provider
        if flow == self.FLOW_LOGIN:
            self.pipeline = provider.get_auth_pipeline()
        elif flow == self.FLOW_SETUP_PROVIDER:
            self.pipeline = provider.get_setup_pipeline()
        else:
            raise NotImplementedError

        # we serialize the pipeline to be [AuthView().get_ident(), ...] which
        # allows us to determine if the pipeline has changed during the auth
        # flow or if the user is somehow circumventing a chunk of it
        self.signature = md5_text(' '.join(av.get_ident() for av in self.pipeline)).hexdigest()

    def pipeline_is_valid(self):
        if not self.state.is_valid():
            return False
        if self.state.flow not in (self.FLOW_LOGIN, self.FLOW_SETUP_PROVIDER):
            return False
        return self.state.signature == self.signature

    def init_pipeline(self):
        self.state.regenerate({
            'uid': self.request.user.id if self.request.user.is_authenticated() else None,
            'auth_provider': self.auth_provider.id if self.auth_provider else None,
            'provider_key': self.provider.key,
            'org_id': self.organization.id,
            'step_index': 0,
            'signature': self.signature,
            'flow': self.flow,
            'data': {},
        })

    def get_redirect_url(self):
        return absolute_uri(reverse('sentry-auth-sso'))

    def clear_session(self):
        self.state.clear()

    def current_step(self):
        """
        Render the current step.
        """
        step_index = self.state.step_index

        if step_index == len(self.pipeline):
            return self.finish_pipeline()

        return self.pipeline[step_index].dispatch(
            request=self.request,
            helper=self,
        )

    def next_step(self):
        """
        Render the next step.
        """
        self.state.step_index += 1
        return self.current_step()

    def finish_pipeline(self):
        data = self.fetch_state()

        try:
            identity = self.provider.build_identity(data)
        except IdentityNotValid:
            return self.error(ERR_INVALID_IDENTITY)

        if self.state.flow == self.FLOW_LOGIN:
            # create identity and authenticate the user
            response = self._finish_login_pipeline(identity)
        elif self.state.flow == self.FLOW_SETUP_PROVIDER:
            response = self._finish_setup_pipeline(identity)

        return response

    @transaction.atomic
    def _handle_attach_identity(self, identity, member=None):
        """
        Given an already authenticated user, attach or re-attach an identity.
        """
        auth_provider = self.auth_provider
        request = self.request
        user = request.user
        organization = self.organization

        try:
            try:
                # prioritize identifying by the SSO provider's user ID
                auth_identity = AuthIdentity.objects.get(
                    auth_provider=auth_provider,
                    ident=identity['id'],
                )
            except AuthIdentity.DoesNotExist:
                # otherwise look for an already attached identity
                # this can happen if the SSO provider's internal ID changes
                auth_identity = AuthIdentity.objects.get(
                    auth_provider=auth_provider,
                    user=user,
                )
        except AuthIdentity.DoesNotExist:
            auth_identity = AuthIdentity.objects.create(
                auth_provider=auth_provider,
                user=user,
                ident=identity['id'],
                data=identity.get('data', {}),
            )
            auth_is_new = True
        else:
            now = timezone.now()

            # TODO(dcramer): this might leave the user with duplicate accounts,
            # and in that kind of situation its very reasonable that we could
            # test email addresses + is_managed to determine if we can auto
            # merge
            if auth_identity.user != user:
                # it's possible the user has an existing identity, let's wipe it out
                # so that the new identifier gets used (other we'll hit a constraint)
                # violation since one might exist for (provider, user) as well as
                # (provider, ident)
                AuthIdentity.objects.exclude(
                    id=auth_identity.id,
                ).filter(
                    auth_provider=auth_provider,
                    user=user,
                ).delete()

                # since we've identify an identity which is no longer valid
                # lets preemptively mark it as such
                try:
                    other_member = OrganizationMember.objects.get(
                        user=auth_identity.user_id,
                        organization=organization,
                    )
                except OrganizationMember.DoesNotExist:
                    pass
                else:
                    setattr(other_member.flags, 'sso:invalid', True)
                    setattr(other_member.flags, 'sso:linked', False)
                    other_member.save()

            auth_identity.update(
                user=user,
                ident=identity['id'],
                data=self.provider.update_identity(
                    new_data=identity.get('data', {}),
                    current_data=auth_identity.data,
                ),
                last_verified=now,
                last_synced=now,
            )
            auth_is_new = False

        if member is None:
            try:
                member = OrganizationMember.objects.get(
                    user=user,
                    organization=organization,
                )
            except OrganizationMember.DoesNotExist:
                member = OrganizationMember.objects.create(
                    organization=organization,
                    role=organization.default_role,
                    user=user,
                    flags=getattr(OrganizationMember.flags, 'sso:linked'),
                )

                default_teams = auth_provider.default_teams.all()
                for team in default_teams:
                    OrganizationMemberTeam.objects.create(
                        team=team,
                        organizationmember=member,
                    )

                AuditLogEntry.objects.create(
                    organization=organization,
                    actor=user,
                    ip_address=request.META['REMOTE_ADDR'],
                    target_object=member.id,
                    target_user=user,
                    event=AuditLogEntryEvent.MEMBER_ADD,
                    data=member.get_audit_log_data(),
                )
        if getattr(member.flags, 'sso:invalid') or not getattr(member.flags, 'sso:linked'):
            setattr(member.flags, 'sso:invalid', False)
            setattr(member.flags, 'sso:linked', True)
            member.save()

        if auth_is_new:
            AuditLogEntry.objects.create(
                organization=organization,
                actor=user,
                ip_address=request.META['REMOTE_ADDR'],
                target_object=auth_identity.id,
                event=AuditLogEntryEvent.SSO_IDENTITY_LINK,
                data=auth_identity.get_audit_log_data(),
            )

            messages.add_message(
                request,
                messages.SUCCESS,
                OK_LINK_IDENTITY,
            )

        return auth_identity

    def _handle_new_user(self, identity):
        auth_provider = self.auth_provider

        user = User.objects.create(
            username=uuid4().hex,
            email=identity['email'],
            name=identity.get('name', '')[:200],
        )

        try:
            with transaction.atomic():
                auth_identity = AuthIdentity.objects.create(
                    auth_provider=auth_provider,
                    user=user,
                    ident=identity['id'],
                    data=identity.get('data', {}),
                )
        except IntegrityError:
            auth_identity = AuthIdentity.objects.get(
                auth_provider=auth_provider,
                ident=identity['id'],
            )
            auth_identity.update(
                user=user,
                data=identity.get('data', {}),
            )

        self._handle_new_membership(auth_identity)

        return auth_identity

    def _handle_new_membership(self, identity):
        auth_provider = self.auth_provider
        organization = self.organization
        request = self.request
        user = identity.user

        om = OrganizationMember.objects.create(
            organization=organization,
            role=organization.default_role,
            user=user,
            flags=getattr(OrganizationMember.flags, 'sso:linked'),
        )

        default_teams = auth_provider.default_teams.all()
        for team in default_teams:
            OrganizationMemberTeam.objects.create(
                team=team,
                organizationmember=om,
            )

        AuditLogEntry.objects.create(
            organization=organization,
            actor=user,
            ip_address=request.META['REMOTE_ADDR'],
            target_object=om.id,
            target_user=om.user,
            event=AuditLogEntryEvent.MEMBER_ADD,
            data=om.get_audit_log_data(),
        )

        return om

    def _get_login_form(self, existing_user=None):
        request = self.request
        return AuthenticationForm(
            request,
            request.POST if request.POST.get('op') == 'login' else None,
            initial={
                'username': existing_user.username if existing_user else None,
            },
        )

    def _get_display_name(self, identity):
        return identity.get('name') or identity.get('email')

    def _get_identifier(self, identity):
        return identity.get('email') or identity.get('id')

    def _find_existing_user(self, email):
        return User.objects.filter(
            id__in=UserEmail.objects.filter(email__iexact=email).values('user'),
            is_active=True,
        ).first()

    def _handle_unknown_identity(self, identity):
        """
        Flow is activated upon a user logging in to where an AuthIdentity is
        not present.

        The flow will attempt to answer the following:

        - Is there an existing user with the same email address? Should they be
          merged?

        - Is there an existing user (via authentication) that shoudl be merged?

        - Should I create a new user based on this identity?
        """
        request = self.request
        op = request.POST.get('op')
        if not request.user.is_authenticated():
            # TODO(dcramer): its possible they have multiple accounts and at
            # least one is managed (per the check below)
            try:
                acting_user = self._find_existing_user(identity['email'])
            except IndexError:
                acting_user = None
            login_form = self._get_login_form(acting_user)
        else:
            acting_user = request.user

        verified_email = acting_user and acting_user.emails.filter(
            is_verified=True,
            email__iexact=identity['email'],
        ).exists()

        # If they already have an SSO account and the identity provider says
        # the email matches we go ahead and let them merge it. This is the
        # only way to prevent them having duplicate accounts, and because
        # we trust identity providers, its considered safe.
        if acting_user and identity.get('email_verified') and verified_email:
            # we only allow this flow to happen if the existing user has
            # membership, otherwise we short circuit because it might be
            # an attempt to hijack membership of another organization
            has_membership = OrganizationMember.objects.filter(
                user=acting_user,
                organization=self.organization,
            ).exists()
            if has_membership:
                if not auth.login(
                    request,
                    acting_user,
                    after_2fa=request.build_absolute_uri(),
                    organization_id=self.organization.id
                ):
                    return HttpResponseRedirect(auth.get_login_redirect(self.request))
                # assume they've confirmed they want to attach the identity
                op = 'confirm'
            else:
                # force them to create a new account
                acting_user = None

        if op == 'confirm' and request.user.is_authenticated():
            auth_identity = self._handle_attach_identity(identity)
        elif op == 'newuser':
            auth_identity = self._handle_new_user(identity)
        elif op == 'login' and not request.user.is_authenticated():
            # confirm authentication, login
            op = None
            if login_form.is_valid():
                # This flow is special.  If we are going through a 2FA
                # flow here (login returns False) we want to instruct the
                # system to return upon completion of the 2fa flow to the
                # current URL and continue with the dialog.
                #
                # If there is no 2fa we don't need to do this and can just
                # go on.
                if not auth.login(
                    request,
                    login_form.get_user(),
                    after_2fa=request.build_absolute_uri(),
                    organization_id=self.organization.id
                ):
                    return HttpResponseRedirect(auth.get_login_redirect(self.request))
            else:
                auth.log_auth_failure(request, request.POST.get('username'))
        else:
            op = None

        if not op:
            if request.user.is_authenticated():
                return self.respond(
                    'sentry/auth-confirm-link.html', {
                        'identity': identity,
                        'existing_user': request.user,
                        'identity_display_name': self._get_display_name(identity),
                        'identity_identifier': self._get_identifier(identity)
                    }
                )

            return self.respond(
                'sentry/auth-confirm-identity.html', {
                    'existing_user': acting_user,
                    'identity': identity,
                    'login_form': login_form,
                    'identity_display_name': self._get_display_name(identity),
                    'identity_identifier': self._get_identifier(identity)
                }
            )

        user = auth_identity.user
        user.backend = settings.AUTHENTICATION_BACKENDS[0]

        # XXX(dcramer): this is repeated from above
        if not auth.login(
            request,
            user,
            after_2fa=request.build_absolute_uri(),
            organization_id=self.organization.id
        ):
            return HttpResponseRedirect(auth.get_login_redirect(self.request))

        self.clear_session()

        return HttpResponseRedirect(auth.get_login_redirect(self.request))

    def _handle_existing_identity(self, auth_identity, identity):
        # TODO(dcramer): this is very similar to attach
        now = timezone.now()
        auth_identity.update(
            data=self.provider.update_identity(
                new_data=identity.get('data', {}),
                current_data=auth_identity.data,
            ),
            last_verified=now,
            last_synced=now,
        )

        try:
            member = OrganizationMember.objects.get(
                user=auth_identity.user,
                organization=self.organization,
            )
        except OrganizationMember.DoesNotExist:
            # this is likely the case when someone was removed from the org
            # but still has access to rejoin
            member = self._handle_new_membership(auth_identity)
        else:
            if getattr(member.flags, 'sso:invalid') or not getattr(member.flags, 'sso:linked'):
                setattr(member.flags, 'sso:invalid', False)
                setattr(member.flags, 'sso:linked', True)
                member.save()

        user = auth_identity.user
        user.backend = settings.AUTHENTICATION_BACKENDS[0]

        if not auth.login(
            self.request,
            user,
            after_2fa=self.request.build_absolute_uri(),
            organization_id=self.organization.id
        ):
            return HttpResponseRedirect(auth.get_login_redirect(self.request))

        self.clear_session()
        metrics.incr('sso.login-success', tags={'provider': self.provider.key})

        return HttpResponseRedirect(auth.get_login_redirect(self.request))

    @transaction.atomic
    def _finish_login_pipeline(self, identity):
        """
        The login flow executes both with anonymous and authenticated users.

        Upon completion a few branches exist:

        If the identity is already linked, the user should be logged in
        and redirected immediately.

        Otherwise, the user is presented with a confirmation window. That window
        will show them the new account that will be created, and if they're
        already authenticated an optional button to associate the identity with
        their account.
        """
        auth_provider = self.auth_provider
        user_id = identity['id']

        lock = locks.get(
            'sso:auth:{}:{}'.format(
                auth_provider.id,
                md5_text(user_id).hexdigest(),
            ),
            duration=5,
        )
        with TimedRetryPolicy(5)(lock.acquire):
            try:
                auth_identity = AuthIdentity.objects.select_related('user').get(
                    auth_provider=auth_provider,
                    ident=user_id,
                )
            except AuthIdentity.DoesNotExist:
                auth_identity = None

            # Handle migration of identity keys
            if not auth_identity and isinstance(user_id, MigratingIdentityId):
                try:
                    auth_identity = AuthIdentity.objects.select_related('user').get(
                        auth_provider=auth_provider,
                        ident=user_id.legacy_id,
                    )
                    auth_identity.update(ident=user_id.id)
                except AuthIdentity.DoesNotExist:
                    auth_identity = None

            if not auth_identity:
                return self._handle_unknown_identity(identity)

            # If the User attached to this AuthIdentity is not active,
            # we want to clobber the old account and take it over, rather than
            # getting logged into the inactive account.
            if not auth_identity.user.is_active:

                # Current user is also not logged in, so we have to
                # assume unknown.
                if not self.request.user.is_authenticated():
                    return self._handle_unknown_identity(identity)

                auth_identity = self._handle_attach_identity(identity)

            return self._handle_existing_identity(auth_identity, identity)

    @transaction.atomic
    def _finish_setup_pipeline(self, identity):
        """
        The setup flow creates the auth provider as well as an identity linked
        to the active user.
        """
        request = self.request
        if not request.user.is_authenticated():
            return self.error(ERR_NOT_AUTHED)

        if request.user.id != self.state.uid:
            return self.error(ERR_UID_MISMATCH)

        data = self.fetch_state()
        config = self.provider.build_config(data)

        try:
            om = OrganizationMember.objects.get(
                user=request.user,
                organization=self.organization,
            )
        except OrganizationMember.DoesNotExist:
            return self.error(ERR_UID_MISMATCH)

        self.auth_provider = AuthProvider.objects.create(
            organization=self.organization,
            provider=self.provider.key,
            config=config,
        )

        self._handle_attach_identity(identity, om)

        auth.mark_sso_complete(request, self.organization.id)

        AuditLogEntry.objects.create(
            organization=self.organization,
            actor=request.user,
            ip_address=request.META['REMOTE_ADDR'],
            target_object=self.auth_provider.id,
            event=AuditLogEntryEvent.SSO_ENABLE,
            data=self.auth_provider.get_audit_log_data(),
        )

        email_missing_links.delay(self.organization.id, request.user.id, self.provider.key)

        messages.add_message(
            self.request,
            messages.SUCCESS,
            OK_SETUP_SSO,
        )

        self.clear_session()

        next_uri = reverse(
            'sentry-organization-auth-provider-settings', args=[
                self.organization.slug,
            ]
        )
        return HttpResponseRedirect(next_uri)

    def respond(self, template, context=None, status=200):
        default_context = {
            'organization': self.organization,
        }
        if context:
            default_context.update(context)

        return render_to_response(template, default_context, self.request, status=status)

    def error(self, message):
        redirect_uri = '/'

        if self.state.flow == self.FLOW_LOGIN:
            # create identity and authenticate the user
            redirect_uri = reverse('sentry-auth-organization', args=[self.organization.slug])

        elif self.state.flow == self.FLOW_SETUP_PROVIDER:
            redirect_uri = reverse(
                'sentry-organization-auth-settings', args=[self.organization.slug]
            )

        metrics.incr('sso.error', tags={
            'provider': self.provider.key,
            'flow': self.state.flow
        })

        messages.add_message(
            self.request,
            messages.ERROR,
            u'Authentication error: {}'.format(message),
        )

        return HttpResponseRedirect(redirect_uri)

    def bind_state(self, key, value):
        data = self.state.data
        data[key] = value

        self.state.data = data

    def fetch_state(self, key=None):
        return self.state.data if key is None else self.state.data.get(key)
