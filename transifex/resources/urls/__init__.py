# -*- coding: utf-8 -*-
from django.conf.urls.defaults import *
from django.conf import settings
from transifex.projects.urls import PROJECTS_URL, PROJECT_URL, PROJECT_URL_PARTIAL
from transifex.resources.views import *

# General URLs:
urlpatterns = patterns('',
    # Project resources list
    url(PROJECT_URL_PARTIAL+r'resources/(?P<offset>\d+)$',
        project_resources, name='project_resources'),
    url(PROJECT_URL_PARTIAL+r'resources/(?P<offset>\d+)/more/$',
        project_resources, name='project_resources_more', kwargs={'more':True}),
)


# Resource-specific URLs:
# URL relative to the projects app (no '/projects' prefix)
RESOURCE_URL_PARTIAL = PROJECT_URL_PARTIAL + r'resource/(?P<resource_slug>[-\w]+)/'
# URL which should be used from other addons (full with prefix). The ^ on ^p/
# must be escaped.
RESOURCE_URL = PROJECTS_URL + RESOURCE_URL_PARTIAL[1:]

# URL relative to the projects app (no '/projects' prefix)
RESOURCE_LANG_URL_PARTIAL = RESOURCE_URL_PARTIAL + r'l/(?P<lang_code>[\-_@\w]+)/'
# URL which should be used from other addons (full with prefix)
RESOURCE_LANG_URL = PROJECTS_URL + RESOURCE_LANG_URL_PARTIAL

# Use _PARTIAL since this whole file is included from inside projects/urls.py.
urlpatterns += patterns('',
    # Resources
    url(RESOURCE_URL_PARTIAL+r'$', resource_detail, name='resource_detail'),
    url(RESOURCE_URL_PARTIAL+r'edit/$', resource_edit, name='resource_edit'),
    url(RESOURCE_URL_PARTIAL+r'delete/$', resource_delete, name='resource_delete'),
    url(RESOURCE_URL_PARTIAL+'download_pot/$', get_pot_file, name='download_pot'),
    # Resources-Lang
    url(RESOURCE_LANG_URL_PARTIAL+'delete_all/$',
        resource_translations_delete, name='resource_translations_delete'),
    url(RESOURCE_LANG_URL_PARTIAL+'download/$',
        get_translation_file, name='download_translation'),
    url(RESOURCE_LANG_URL_PARTIAL+'lock_and_download/$',
        lock_and_get_translation_file, name='lock_and_download_translation'),
)
