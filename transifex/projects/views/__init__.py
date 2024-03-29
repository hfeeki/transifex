from django.contrib.syndication.views import feed

from transifex.projects.models import Project
from transifex.projects.permissions import pr_project_private_perm
from transifex.txcommon.decorators import one_perm_required_or_403

# Feeds
def slug_feed(request, slug=None, param='', feed_dict=None):
    """
    Override default feed, using custom (including nonexistent) slug.

    Provides the functionality needed to decouple the Feed's slug from
    the urlconf, so a feed mounted at "^/feed" can exist.

    See also http://code.djangoproject.com/ticket/6969.
    """
    if slug:
        url = "%s/%s" % (slug, param)
    else:
        url = param
    return feed(request, url, feed_dict)


@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'param'), anonymous_access=True)
# This is used for the feeds of a specific project
def project_feed(request, slug=None, param='', feed_dict=None):
    """
    Override default feed, using custom (including nonexistent) slug.

    Provides the functionality needed to decouple the Feed's slug from
    the urlconf, so a feed mounted at "^/feed" can exist.

    See also http://code.djangoproject.com/ticket/6969.
    """
    if slug:
        url = "%s/%s" % (slug, param)
    else:
        url = param
    return feed(request, url, feed_dict)


# Release
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
def release_feed(request, project_slug, release_slug, slug=None, param='',
    feed_dict=None,):
    param = '%s/%s' % (project_slug, release_slug)
    if slug:
        url = "%s/%s" % (slug, param)
    else:
        url = param
    return feed(request, url, feed_dict)


@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
def release_language_feed(request, project_slug, release_slug, language_code,
    slug=None, param='', feed_dict=None,):
    param = '%s/%s/%s' % (project_slug, release_slug, language_code)
    if slug:
        url = "%s/%s" % (slug, param)
    else:
        url = param
    return feed(request, url, feed_dict)