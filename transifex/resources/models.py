# -*- coding: utf-8 -*-
"""
String Level models.
"""

import datetime, sys, re, operator
from itertools import groupby

from hashlib import md5
from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils.translation import ugettext_lazy as _
from django.utils.hashcompat import md5_constructor
from django.utils import simplejson as json
from django.contrib.auth.models import User, AnonymousUser

from transifex.languages.models import Language
from transifex.projects.models import Project
from transifex.storage.models import StorageFile
from transifex.txcommon.db.models import CompressedTextField, \
    ChainerManager, ListCharField
from transifex.txcommon.log import logger
from transifex.resources.utils import invalidate_template_cache


def _aggregate_rlstats(rlstats_query, grouping_key, total=None):
    """
    Yield AggregatedRLStats objects resulting from grouped and summed RLStats
    objects given in the ``rlstats_query``. The grouping happens per language.

    Parameters:
    rlstats_query: This is the queryset of RLStats to be aggregated
    """

    class AggregatedRLStats(object):
        pass

    # Here grouping happens by the grouping_key. If it's a foreign key
    # (resource, language) you have to make sure that __unicode__() method
    # returns something unique per key.
    grouped_rlstats = groupby(rlstats_query, key=operator.attrgetter(grouping_key))

    for key, rlstats in grouped_rlstats:
        stats = AggregatedRLStats()
        # Init attrs
        stats.translated = 0
        stats.untranslated = 0
        stats.translated_perc = 0
        stats.untranslated_perc = 0
        stats.last_update = None
        stats.last_committer = None
        stats.wordcount = 0
        stats.total = 0
        stats.object = key
        count = 0

        for rl in rlstats:
            stats.translated += rl.translated
            stats.untranslated += rl.untranslated
            stats.translated_perc += rl.translated_perc
            stats.untranslated_perc += rl.untranslated_perc
            stats.total += rl.total
            #FIXME: Add wordcount to RLStats and Translation
            #stats.wordcount += rl.wordcount
            count += 1

            if not stats.last_update or rl.last_update > stats.last_update:
                stats.last_update = rl.last_update
                stats.last_committer = rl.last_committer if rl.last_committer_id else None

        # Recalculate percentage completion
        stats.translated_perc = stats.translated_perc / count
        stats.untranslated_perc = 100 - stats.translated_perc

        if total:
            stats.total=total

        stats.number_resources = count
        yield stats


def get_source_language(resources):
    """Return the source language of a list of resources."""
    return resources[0].source_language


class ResourceQuerySet(models.query.QuerySet):

    def for_user(self, user):
        """
        Filter available resources based on the user doing the query. This
        checks permissions and filters out private resources that the user
        doesn't have access to.
        """
        return Resource.objects.filter(
            #FIXME: Adding "Project.objects.for_user(user).values('pk').query"
            # breaks some queries like
            # RLStats.objects.private(User.objects.get(username="margie")).count()
            project__in=Project.objects.for_user(user))

class Resource(models.Model):
    """
    A translatable resource, such as a document, a set of strings, etc.

    This is roughly equivalent to a POT file, string stream, or some other
    strings grouped together as a single translatable element.

    The resource is the rough equivalent to the deprecated 'Component' object,
    but with an important difference: a resource can only have one "source file"
    whereas the Component was able to encapsulate multiple ones.

    A resource is always related to a project.
    """

    # Short identifier to be used in the API URLs
    slug = models.SlugField(_('Slug'), max_length=50,
        help_text=_("A short label to be used in the URL, containing only "
                    "letters, numbers, underscores and hyphens."))
    name = models.CharField(_('Name'), max_length=255, null=False, blank=False,
        help_text=_("A descriptive name unique inside the project."))

    # i18n related fields
    source_file = models.ForeignKey(StorageFile, verbose_name=_("Source file"),
        blank=True, null=True,
        help_text=_("A local file used to extract the strings for "
                    "translation."))
    i18n_type = models.CharField(_('I18n type'), max_length=20, editable=False,
        choices=((k,settings.I18N_METHODS[k]['description']) for k,v in settings.I18N_METHODS.items()),
        help_text=_("The type of i18n method used in this resource (%s)") %
                    ', '.join(settings.TRANS_CHOICES.keys()))
    accept_translations = models.BooleanField(_('Accepting translations?'),
        blank=False, null=False, default=True,
        help_text=_('Is this resource accepting translations?'))
    total_entities = models.IntegerField(_('Total source entities'),
        blank=False, null=False, editable=False, default=0,
        help_text=_('The number of source strings in this translation'
            ' resource.'))
    wordcount = models.IntegerField(_('Number of words in source entities.'),
        blank=False, null=False, editable=False, default=0,
        help_text=_('The number of words contained in the source entities in'
            ' this translation resource.'))

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    source_language = models.ForeignKey(Language,
        verbose_name=_('Source Language'), blank=False, null=False,
        help_text=_("The source language of this Resource."))
    project = models.ForeignKey(Project, verbose_name=_('Project'),
        blank=False, null=True, related_name='resources',
        help_text=_("The project containing the translation resource."))

    # Managers
    objects = ChainerManager(ResourceQuerySet)

    def __unicode__(self):
        return u'%s: %s' % (self.project.name, self.name)

    def __repr__(self):
        return repr(u"<Resource: %s>" % self.slug)

    class Meta:
        unique_together = ('slug', 'project',)
        verbose_name = _('resource')
        verbose_name_plural = _('resources')
        ordering  = ['name',]
        order_with_respect_to = 'project'
        models.get_latest_by = 'created'


    def save(self, *args, **kwargs):
        """
        Do some etxra processing along with the actual save to db.
        """
        # If object is new (aka created=True)
        created=False
        if not self.pk:
            created=True

        # Update total_entries
        self.update_total_entities(save=False)

        # Save the object
        super(Resource, self).save(*args, **kwargs)

        # Create the team language stat objects
        if created:
            Team = models.get_model('teams', 'Team')
            for team in Team.objects.select_related('language'
                ).filter(project=self.project):
                RLStats.objects.get_or_create(resource=self,
                    language=team.language)

        invalidate_template_cache("project_resource_details",
            self.project.slug, self.slug)
        invalidate_template_cache("resource_details",
            self.project.slug, self.slug)

    def delete(self, *args, **kwargs):
        """
        Do some extra processing along with the actual delete to db.
        """
        # Import is here to avoid circular imports
        from transifex.resources.handlers import invalidate_stats_cache

        invalidate_stats_cache(self, self.source_language)
        RLStats.objects.filter(resource=self).delete()
        super(Resource, self).delete(*args, **kwargs)

    def update_total_entities(self, total_entities=None, save=True):
        """
        Return the total number of SourceEntity objects to be translated.
        """
        if total_entities:
            self.total_entities = total_entities
        else:
            self.total_entities = SourceEntity.objects.filter(
                resource=self).values('id').count()
        if save:
            self.save()

    def update_wordcount(self, save=True):
        """
        Return the number of words which need translation in this resource.

        The counting of the words uses the Translation objects of the SOURCE
        LANGUAGE as set of objects. This function does not count the plural
        strings!
        """
        wc = Translation.objects.filter(source_entity__id__in=
            SourceEntity.objects.filter(resource=self).values('id'),
            language=self.source_language).aggregate(
            Sum('wordcount'))['wordcount__sum'] or 0
        self.wordcount = wc
        if save:
            self.save()

    @models.permalink
    def get_absolute_url(self):
        return ('resource_detail', None,
            { 'project_slug': self.project.slug, 'resource_slug' : self.slug })

    @property
    def full_name(self):
        """
        Return a simple string without spaces identifying the resource.

        Can be used instead of __unicode__ to create files on disk, URLs, etc.
        """
        return "%s.%s" % (self.project.slug, self.slug)

    @property
    def entities(self):
        """Return the resource's translation entities."""
        return SourceEntity.objects.filter(resource=self)

    @property
    def available_languages(self):
        """
        All available languages for the resource. This list includes team
        languages that may have 0 translated entries.
        """
        return Language.objects.filter(
            id__in=RLStats.objects.by_resource(self).values('language').query)

    @property
    def available_languages_without_teams(self):
        """
        All languages for the resource that have at least one translation.
        """
        return Language.objects.filter(
            id__in=RLStats.objects.by_resource(self).filter(
                translated__gt=0).values('language').query)

class SourceEntityManager(models.Manager):

    def for_user(self, user):
        """
        Filter available source entities based on the user doing the query. This
        checks permissions and filters out private source entites that the user
        doesn't have access to.
        """
        return SourceEntity.objects.filter(
            resource__in=Resource.objects.for_user(user))

class SourceEntity(models.Model):
    """
    A representation of a source string which is translated in many languages.

    The SourceEntity is pointing to a specific Resource and it is uniquely
    defined by the string, context and resource fields (so they are unique
    together).
    """
    string = models.TextField(_('String'), blank=False, null=False,
        help_text=_("The actual string content of source string."))
    string_hash = models.CharField(_('String Hash'), blank=False, null=False,
        max_length=32, editable=False,
        help_text=_("The hash of the translation string used for indexing"))
    context = ListCharField(_('Context'), max_length=255,
        null=False, default="",
        help_text=_("A list of description of the source string. This field "
                    "specifies the context of the source string inside the "
                    "resource."))
    position = models.IntegerField(_('Position'), blank=True, null=True,
        help_text=_("The position of the source string in the Resource."
                    "For example, the specific position of a msgid field in a "
                    "po template (.pot) file in gettext."))
    #TODO: Decision for the following
    occurrences = models.TextField(_('Occurrences'), max_length=1000,
        blank=True, editable=False, null=True,
        help_text=_("The occurrences of the source string in the project code."))
    flags = models.TextField(_('Flags'), max_length=100,
        blank=True, editable=False,
        help_text=_("The flags which mark the source string. For example, if"
                    "there is a python formatted string this is marked as "
                    "\"#, python-format\" in gettext."))
    developer_comment = models.TextField(_('Comments'), max_length=1000,
        blank=True, editable=False,
        help_text=_("The comment of the developer."))

    # Used for comments added from Lotte
    developer_comment_extra = models.TextField(_('Extra comments'), 
        max_length=1000, blank=True, editable=False,
        help_text=_("Another comment of the developer"))

    pluralized = models.BooleanField(_('Pluralized'), blank=False,
        null=False, default=False,
        help_text=_("Whether the entity is pluralized."))

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    # A source string must always belong to a resource
    resource = models.ForeignKey(Resource, verbose_name=_('Resource'),
        blank=False, null=False, related_name='source_entities',
        help_text=_("The translation resource which owns the source string."))

    objects = SourceEntityManager()

    def __unicode__(self):
        return self.string

    class Meta:
        unique_together = (('string_hash', 'context', 'resource'),)
        verbose_name = _('source string')
        verbose_name_plural = _('source strings')
        ordering = ['last_update',]
        get_latest_by = 'created'

    def save(self, *args, **kwargs):
        """
        Do some exra processing before the actual save to db.
        """
        context = self.context_string
        # This is for sqlite support since None objects are treated as strings
        # containing 'None'
        if not context or context == 'None':
            context = ""

        # Calculate new hash
        self.string_hash = md5_constructor(':'.join([self.string,
            context]).encode('utf-8')).hexdigest()

        super(SourceEntity, self).save(*args, **kwargs)

    @property
    def context_string(self):
        """Return context field as a colon concatenated string"""
        if self.context:
            return u':'.join(self.context)
        return u''

    def get_translation(self, lang_code, rule=5):
        """Return the current active translation for this entity."""
        try:
            return self.translations.get(language__code=lang_code, rule=rule)
        except Translation.DoesNotExist:
            return None


class TranslationManager(models.Manager):
    def by_source_entity_and_language(self, string,
            source_code='en', target_code=None):
        """
        Return the results of searching, based on a specific source string and
        maybe on specific source and/or target language.
        """
        source_entities = []

        source_entities = SourceEntity.objects.filter(string=string,)

        # If no target language given search on any target language.
        if target_code:
            language = Language.objects.by_code_or_alias(target_code)
            results = self.filter(
                        source_entity__in=source_entities, language=language)
        else:
            results = self.filter(source_entity__in=source_entities)
        return results

    def by_string_and_language(self, string, user, source_code='en', target_code=None):
        """
        Search translation for source strings queries and only in Public projects!
        """
        query = models.Q()
        for term in string.split(' '):
            query &= models.Q(string__icontains=term)

        source_language = Language.objects.by_code_or_alias(source_code)

        # If no target language given search on any target language.
        if target_code:
            language = Language.objects.by_code_or_alias(target_code)
            results =  self.filter(language=language,
                source_entity__resource__project__in=Project.objects.for_user(user),
                source_entity__id__in=self.filter(query, language=source_language).values_list(
                    'source_entity', flat=True))
        else:
            results =  self.filter(
                source_entity__resource__project__in=Project.objects.for_user(user),
                source_entity__id__in=self.filter(query, language=source_language).values_list(
                    'source_entity', flat=True))
        return results

    def source_strings(self, resources):
        """Return all source strings that correspond to the specified resources.

        Args:
            resources: An iterable of Resource objects.

        Returns:
            A queryset that returns all source strings (ie strings in the source
            language) for the resources.
        """
        source_language = get_source_language(resources)
        return self.filter(
            source_entity__resource__in=resources,
            language=source_language,
            rule=5
        )

    def untranslated_source_strings(self, resources, language):
        """Return the source strings which have not been translated in the specified
        language.

        Args:
            resources: An iterable of Resource objects.
            language: The language to search for untranslated strings.

        Returns:
            A queryset which returns all untranslated source strings in the specified
            language.
        """
        source_language = get_source_language(resources)
        all_se_ids = frozenset(SourceEntity.objects.filter(
            resource__in=resources
        ).values_list('id', flat=True))
        translated_se_ids = frozenset(self.filter(
            source_entity__resource__in=resources,
            language=language, rule=5
        ).values_list('source_entity_id', flat=True))
        untranslated_se_ids = all_se_ids - translated_se_ids
        return self.filter(
            source_entity__id__in=untranslated_se_ids,
            language=source_language, rule=5
        )

    def translated_source_strings(self, resources, language):
        """Return the source strings which have been translated in the specified
        language.

        Args:
            resources: An iterable of Resource objects.
            language: The language to search for translated strings.

        Returns:
            A queryset which returns all translated source strings in the specified
            language.
        """
        source_language = get_source_language(resources)
        translated_se_ids = frozenset(self.filter(
            source_entity__resource__in=resources,
            language=language, rule=5
        ).values_list('source_entity_id', flat=True))
        return self.filter(
            source_entity__id__in=translated_se_ids,
            language=source_language, rule=5
        )

    def user_translated_strings(self, resources, language, users):
        """Return the source strings which have been transalted in the specified language
        by the specified users.

        Args:
            resources: An iterable of Resource objects.
            language: The language to look for translations in.
            users: An iterable of user ids.

        Returns:
            A queryset that returns all source strigns which have been translated in
            `language` by `users`.
        """
        source_language = get_source_language(resources)
        user_translated_se_ids = frozenset(self.filter(
            language=language, rule=5,
            user__id__in=users,
            source_entity__resource__in=resources
        ).values_list('source_entity_id', flat=True))
        return self.filter(
            source_entity__id__in=user_translated_se_ids,
            language=source_language, rule=5,
        )


class Translation(models.Model):
    """
    The representation of a live translation for a given source string.

    This model encapsulates all the necessary fields for the translation of a
    source string in a specific target language. It also contains a set of meta
    fields for the context of this translation.
    """

    string = models.TextField(_('String'), blank=False, null=False,
        help_text=_("The actual string content for translation."))
    string_hash = models.CharField(_('String Hash'), blank=False, null=False,
        max_length=32, editable=False,
        help_text=_("The hash of the translation string used for indexing"))
    rule = models.IntegerField(_('Plural rule'), blank=False,
        null=False, default=5,
        help_text=_("Number related to the plural rule of the translation. "
                    "It's 0=zero, 1=one, 2=two, 3=few, 4=many and 5=other. "
                    "For translations that have no plural forms, "
                    "the rule must be 5 (other)."))
    wordcount = models.PositiveIntegerField(_("Wordcount for this string."),
        blank=False, null=False, default=0, help_text="The number of words "
        "contained in this translation string.")

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    # A source string must always belong to a resource
    source_entity = models.ForeignKey(SourceEntity,
        verbose_name=_('Source String'),
        related_name='translations',
        blank=False, null=False,
        help_text=_("The source string this translation string translates."))

    language = models.ForeignKey(Language,
        verbose_name=_('Target Language'),blank=False, null=True,
        help_text=_("The language in which this translation string is written."))

    user = models.ForeignKey(User,
        verbose_name=_('Committer'), blank=False, null=True,
        help_text=_("The user who committed the specific translation."))

    #TODO: Managers
    objects = TranslationManager()

    def __unicode__(self):
        return self.string

    class Meta:
        unique_together = (('source_entity', 'language', 'rule'),)
        verbose_name = _('translation string')
        verbose_name_plural = _('translation strings')
        ordering  = ['last_update',]
        get_latest_by = 'last_update'

    def save(self, *args, **kwargs):
        """
        Do some exra processing before the actual save to db.
        """
        # encoding happens to support unicode characters
        self.string_hash = md5(self.string.encode('utf-8')).hexdigest()
        self._update_wordcount()
        super(Translation, self).save(*args, **kwargs)

    def _update_wordcount(self):
        """
        Return the number of words for this translation string.
        """
        # use None to split at any whitespace regardless of length
        # so for instance double space counts as one space
        self.wordcount = len(self.string.split(None))

    @property
    def source_translation(self):
        """
        Return the Translation object related to the source language of the
        self.source_entity.resource. The returned object of this method contains
        the original content used to translated the current 'self' object.
        If the 'self' object is the actual source language translation, None
        is returned instead.
        """
        # Tweaking the translation rule, because the source translation might
        # not have the same number of plural rules.
        source_language = self.source_entity.resource.source_language
        if not self.source_entity.pluralized or \
            (self.source_entity.pluralized and
            self.rule in source_language.get_pluralrules_numbers()):
            rule = self.rule
        else:
            rule = 5

        try:
            if source_language != self.language:
                return Translation.objects.get(language=source_language,
                    rule=rule, source_entity=self.source_entity)
        except Translation.DoesNotExist:
            pass


class RLStatsQuerySet(models.query.QuerySet):

    def for_user(self, user):
        """
        Return a queryset matching projects plus private projects that the
        given user has access to.
        """
        return self.filter(
            resource__in=Resource.objects.for_user(user).values('pk').query).distinct()

    def private(self):
        """
        Return a queryset matching only RLStats associated with private
        projects.
        """
        resources = Resource.objects.filter(project__private=True)
        return self.filter(resource__in=resources.values('pk').query).distinct()

    def public(self):
        """
        Return a queryset matching only RLStats associated with non-private
        projects.
        """
        resources = Resource.objects.filter(project__private=False)
        return self.filter(resource__in=resources.values('pk').query).distinct()


    def by_project(self, project):
        """
        Return a queryset matching all RLStats associated with a given
        ``project``.
        """
        return self.filter(resource__project=project).distinct()

    def by_resource(self, resource):
        """
        Return a queryset matching all RLStats associated with a given
        ``resource``.
        """
        return self.filter(resource=resource).order_by('-translated_perc')

    def by_resources(self, resources):
        """
        Return a queryset matching all RLStats associated with the given
        ``resources``.
        """
        return self.filter(resource__in=resources).distinct()

    def by_language(self, language):
        """
        Return a queryset matching RLStats associated with a given ``language``.
        """
        return self.filter(language=language).distinct()

    def by_release(self, release):
        """
        Return a queryset matching RLStats associated with a given ``release``.
        """
        return self.filter(resource__in=release.resources.values('pk').query).distinct()

    def by_release_and_language(self, release, language):
        """
        Return a queryset matching RLStats associated with the given
        ``release`` and ``language``.

        """
        return self.by_language(language).by_resources(
            release.resources.values('pk').query)

    def by_project_and_language(self, project, language):
        """
        Return a queryset matching RLStats associated with the given
        ``project`` and ``language``.
        """
        return self.by_language(language).by_resources(
            project.resources.values('pk').query)

    def by_release_aggregated(self, release):
        """
        Aggregate stats for a ``release``.

        RLStats from several resources are grouped by language.
        """
        total = Resource.objects.filter(releases=release).aggregate(
            total=Sum('total_entities'))['total']

        # In order to do grouping by language, we first need to order by
        # language. We use language__code just to be safe and not depend on the
        # __unicode__ method of the language object
        return _aggregate_rlstats(self.by_release(release).order_by('language__code'),
            'language', total)

    def by_project_aggregated(self, project):
        """
        Aggregate stats for a ``project``.

        RLStats from a project are grouped by resources.
        """
        total = Resource.objects.filter(project=project).aggregate(
            total=Sum('total_entities'))['total']

        # In order to do grouping by resource we first need to order by
        # resource. We use resource__slug just to be safe and not depend on the
        # __unicode__ method of the resource object. Maybe this should be
        # changed to use resource.id or something that is truly unique instead
        # of the slug?
        return _aggregate_rlstats(self.by_project(project).order_by('resource__slug'),
            'resource', total)

class RLStats(models.Model):
    """
    Resource-Language statistics object.
    """

    # Fields
    translated = models.PositiveIntegerField(_("Translated Entities"),
        blank=False, null=False, default=0, help_text="The number of "
        "translated entities in a language for a specific resource.")
    translated_wordcount = models.PositiveIntegerField(_("Wordcount for Translated Entities"),
        blank=False, null=False, default=0, help_text="The number of words "
        "containing translated entities in a language for a specific resource.")
    untranslated = models.PositiveIntegerField(_("Untranslated Entities"),
        blank=False, null=False, default=0, help_text="The number of "
        "untranslated entities in a language for a specific resource.")
    last_update = models.DateTimeField(_("Last Update"), auto_now=True,
        default=None, help_text="The datetime that this language was last "
        "updated.")
    last_committer = models.ForeignKey(User, blank=False, null=True,
        default=None, verbose_name=_('Last Committer'), help_text="The user "
        "associated with the last change for this language.")

    # Foreign Keys
    resource = models.ForeignKey(Resource, blank=False, null=False,
        verbose_name="Resource", help_text="The resource to which the "
        "statistics are associated.")
    language = models.ForeignKey(Language, blank=False, null=False,
        verbose_name="Language", help_text="The language to which these "
        "statistics refer.")

    # Normalized fields
    translated_perc = models.PositiveIntegerField(default=0, editable=False)
    untranslated_perc = models.PositiveIntegerField(default=0, editable=False)

    #objects = generate_chainer_manager(RLStatsManager)
    objects = ChainerManager(RLStatsQuerySet)

    def __unicode__(self):
        return "%s stats for %s" % ( self.resource.slug, self.language.code)

    class Meta:
        unique_together = ('resource', 'language',)
        ordering  = ['translation_perc',]
        order_with_respect_to = 'resource'

    @property
    def total(self):
        return self.translated + self.untranslated

    @property
    def untranslated_wordcount(self):
        return self.resource.wordcount - self.translated_wordcount

    def save(self, update=True, *args, **kwargs):
        if update:
            self.update(user=None, save=False)
        super(RLStats, self).save(*args, **kwargs)

    def _calculate_perc(self):
        """Update normalized percentage statistics fields."""
        try:
            total = self.total
            self.translated_perc = self.translated * 100 / total
            self.untranslated_perc = 100 - self.translated_perc
        except ZeroDivisionError:
            self.translated_perc = 0
            self.untranslated_perc = 0

    def _calculate_translated_wordcount(self):
        """Calculate wordcount of translated/untranslated entries"""
        wc = 0
        translated = SourceEntity.objects.filter(
            id__in=Translation.objects.filter(language=self.language,
            source_entity__resource=self.resource).distinct().values_list(
            'source_entity', flat=True))
        wordcount = Translation.objects.filter(source_entity__in=translated,
            language=self.resource.source_language).aggregate(Sum('wordcount'))['wordcount__sum']
        self.translated_wordcount = wordcount or 0

    def _calculate_translated(self):
        """
        Calculate translated/untranslated entities.
        """
        total = SourceEntity.objects.values('id').filter(
            resource=self.resource).count()
        translated = Translation.objects.values('id').filter(rule=5,
            language=self.language, source_entity__resource=self.resource
            ).distinct().count()
        untranslated = total - translated
        self.translated = translated
        self.untranslated = untranslated

    def update(self, user=None, save=True):
        """
        Update the RLStat object
        """
        self._calculate_translated()
        self._calculate_translated_wordcount()
        self._calculate_perc()
        if user:
            self._update_now(user)
        if save:
            self.save(update=False)

    def _update_now(self, user=None):
        """
        Update the last update and last committer.
        """
        self.last_update = datetime.datetime.now()
        if user:
            self.last_committer = user

class Template(models.Model):
    """
    Source file template for a specific resource.

    This model holds the source file template in a compressed textfield to save
    space in the database. All translation strings are changed with the md5
    hashes of the SourceEntity string which enables us to do a quick search and
    replace each time we want to recreate the file.
    """

    content = CompressedTextField(null=False, blank=False,
        help_text=_("This is the actual content of the template"))
    resource = models.OneToOneField(Resource,
        verbose_name="Resource",unique=True,
        blank=False, null=False,related_name="source_file_template",
        help_text=_("This is the template of the imported source file which is"
            " used to export translation files from the db to the user."))

    class Meta:
        verbose_name = _('Template')
        verbose_name_plural = _('Templates')
        ordering = ['resource']
