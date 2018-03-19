from __future__ import absolute_import

from django.utils.translation import ugettext_lazy as _


GUIDES = {
    'issue': {
        'id': 1,
        'cue': _('Get a tour of the issue page'),
        'required_targets': ['exception'],
        'steps': [
            {
                'title': _('Stacktrace'),
                'message': _(
                    'See the sequence of function calls that led to the error, and global/local '
                    'variables for each stack frame.'),
                'target': 'exception',
            },
            {
                'title': _('Breadcrumbs'),
                'message': _(
                    'Breadcrumbs (when sent with the event) appear below the stacktrace and show '
                    'you a trail of events that happened prior to the error. They\'re '
                    'similar to traditional logs but can record more rich structured data. '
                    'When Sentry is used with web frameworks, breadcrumbs are automatically '
                    'captured for events like database calls and network requests.'),
                'target': 'breadcrumbs',
            },
            {
                'title': _('Tags'),
                'message': _(
                    'Tags are arbitrary key-value pairs sent with every event. You can filter '
                    'events by tags, like searching for all events from a specific machine, '
                    'browser, release etc. The sidebar on the right shows you the '
                    'distribution of tags for all events in this event group.'),
                'target': 'tags',
            },
            {
                'title': _('Resolve'),
                'message': _(
                    'Resolving an issue removes it from the default dashboard view of unresolved '
                    'issues. You can ask Sentry to <a href="/settings/account/notifications/"> '
                    'alert you</a> when a resolved issue re-occurs.'),
                'target': 'resolve',
            },
            {
                'title': _('Issue Number'),
                'message': _(
                    'This is a unique identifier for the issue, and can be included in a commit '
                    'message to tell Sentry to resolve the issue when the commit gets deployed. '
                    'See <a href="https://docs.sentry.io/learn/releases/">Releases</a> '
                    'to learn more.'),
                'target': 'issue_number',
            },
            {
                'title': _('Issue Tracking'),
                'message': _(
                    'Create issues in your project management tool from within Sentry. See a list '
                    'of all integrations <a href="https://docs.sentry.io/integrations/">here</a>.'),
                'target': 'issue_tracking',
            },
            {
                'title': _('Ignore, Delete and Discard'),
                'message': _(
                    'Ignoring an issue silences notifications and removes it from your feeds by '
                    'default. Deleting an issue deletes the data associated with an issue and '
                    'causes a new issue to be created if the same error happens again. Delete '
                    '& Discard (available on the medium plan and higher) deletes most of the data '
                    'associated with the issue and discards future events matching the issue '
                    'before it reaches your stream. This is useful to permanently ignore errors '
                    'you don\'t care about.'),
                'target': 'ignore_delete_discard',
            },
        ],
    },
    'releases': {
        'id': 2,
        'cue': _('What are releases?'),
        'required_targets': ['releases'],
        'steps': [
            {
                'title': _('Releases'),
                'message': _('A release is a specific version of your code deployed to an '
                             'environment. When you tell Sentry about your releases, it can '
                             'predict which commits caused an error and who might be a likely '
                             'owner.'),
                'target': 'releases',
            },
            {
                'title': _('Releases'),
                'message': _('Sentry does this by tying together commits in the release, files '
                             'touched by those commits, files observed in the stacktrace, and '
                             'authors of those files. Learn more about releases '
                             '<a href="https://docs.sentry.io/learn/releases/">here</a>.'),
            },
        ]
    },
    'members': {
        'id': 3,
        'cue': _('Tips on inviting your team'),
        'required_targets': ['member-add'],
        'steps': [
            {
                'title': _('Why invite your team?'),
                'message': _('Sentry doesn\'t just collect errors. Adding your team lets you '
                             'triage and assign issues to the right engineer.'),
                'target': 'member-add',
            },
            {
                'title': _('What is status?'),
                'message': _('You can enforce <a href="https://sentry.io/settings/organization/${org_slug}/settings/#require2FA">2-factor auth</a> or '
                             '<a href="/organizations/${org_slug}/auth/"">SSO</a> across your organization. From here, you\'ll be able to see '
                             'which members haven\'t configured them yet.'),
                'target': 'member-status',
            },
            {
                'title': _('A tip for roles'),
                'message': _('Every org should have at least two owners (in case one leaves).<br><br>'
                             'Add finance as a billing member. They\'ll get access to '
                             'invoices, so they won\'t email you for receipts.'),
                'target': 'member-role',
            },
        ]
    }
}
