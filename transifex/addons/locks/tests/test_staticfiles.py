# -*- coding: utf-8 -*-
import os
import unittest
from django.conf import settings
from django.db.models.loading import get_app
from staticfiles.resolvers import AppDirectoriesResolver

class TestStaticfiles(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    #FIXME: Why test the configuration of django_staticfiles in the locks app?
    def test_staticfiles(self):
        """
        Test whether django-staticfiles is properly configured.

        There are various reasons why this could fail:
         * App not loaded (not in get_apps())
         * models.py missing
         * Addon not appended to STATICFILES_PREPEND_LABEL_APPS
         * STATIC_ROOT is not absolute path
         * STATICFILES_MEDIA_DIRNAMES doesn't include 'media'
        """
        suffix = 'css/icons.css'
        for addons_root in settings.ADDONS_ROOTS:
            ref = os.path.realpath('%s/locks/media/%s' % (addons_root, suffix))
            if os.path.exists(ref):
                break
        path = 'locks/%s' % suffix
        r=AppDirectoriesResolver()
        self.assertEqual(ref, r.resolve_for_app(get_app('locks'), path, False))
        self.assertEqual(ref, r.resolve(path))

