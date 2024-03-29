# -*- coding: utf-8 -*-
from datetime import date
import re
from polib import escape, unescape
from django.conf import settings
from django.core.urlresolvers import reverse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Count, Q
from django.db.models.loading import get_model
from django.http import (HttpResponseRedirect, HttpResponse, Http404,
                         HttpResponseForbidden, HttpResponseBadRequest)
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.utils import simplejson
from django.utils.translation import ugettext as _
from django.utils.html import escape
from django.views.generic import list_detail
from django.db import transaction
from authority.views import permission_denied

from actionlog.models import action_logging
from transifex.txcommon.log import logger
from transifex.languages.models import Language
from transifex.projects.models import Project
from transifex.projects.permissions import *
from transifex.projects.permissions.project import ProjectPermission
from transifex.resources.models import Translation, Resource, SourceEntity, \
        get_source_language
from transifex.resources.handlers import invalidate_stats_cache
from transifex.resources.formats.validators import create_error_validators, \
        create_warning_validators, ValidationError
from transifex.teams.models import Team
from transifex.txcommon.decorators import one_perm_required_or_403
from transifex.txcommon.models import get_profile_or_user
import httplib

# Temporary
from transifex.txcommon import notifications as txnotification

try:
    from transifex.addons.gtranslate import is_gtranslate_allowed
except ImportError, e:
    # gtranslate addon is not available
    # create a dummy function that always returns True
    def is_gtranslate_allowed(project):
        return True


class LotteBadRequestError(Exception):
    pass

#Languages suported by google-spellcheck as mentioned at
#http://www.google.com/support/toolbar/bin/answer.py?hl=en&answer=32703
SPELLCHECK_SUPPORTED_LANGS = ['da', 'de', 'en', 'en_US', 'es', 'fi', 'fr', 'it', 'nl', 'pl', 'pt', 'pt_BR', 'ru', 'sv']
Suggestion = get_model('suggestions', 'Suggestion')

from signals import lotte_init, lotte_done, lotte_save_translation

# Restrict access only to : (The checks are done in the view's body)
# 1)those belonging to the specific language team (coordinators or members)
# 2)project maintainers
# 3)global submitters (perms given through access control tab)
# 4)superusers
@login_required
def translate(request, project_slug, lang_code, resource_slug=None,
                     *args, **kwargs):
    """
    Main lotte view.
    """

    # Permissions handling
    # Project should always be available
    project = get_object_or_404(Project, slug=project_slug)
    team = Team.objects.get_or_none(project, lang_code)
    check = ProjectPermission(request.user)
    if not check.submit_translations(team or project) and not\
        check.maintain(project):
        return permission_denied(request)

    resources = []
    if resource_slug:
        resource_list = [get_object_or_404(Resource, slug=resource_slug,
            project=project)]
    else:
        resource_list = Resource.objects.filter(project=project)

        # Return a page explaining that the project has multiple source langs and
        # cannot be translated as a whole.
        if resource_list.values('source_language').distinct().count() > 1:
            messages.info(request,
                          "There are multiple source languages for this project. "
                          "You will only be able to translate resources for one "
                          "source language at a time.")
            return HttpResponseRedirect(reverse('project_detail',
                                        args=[project_slug]),)

    # Filter resources that are not accepting translations
    for resource in resource_list:
        if resource.accept_translations:
            resources.append(resource)

    # If no resource accepting translations, raise a 403
    if not resources:
        return permission_denied(request)

    target_language = Language.objects.by_code_or_alias_or_404(lang_code)

    # If it is an attempt to edit the source language, redirect the user to
    # resource_detail and show him a message explaining the reason.
    if target_language == get_source_language(resources):
        messages.error(request,
                       "Cannot edit the source language because this would "
                       "result in translation mismatches! If you want to "
                       "update the source strings consider using the transifex "
                       "command-line client.")
        if resource_slug:
            return HttpResponseRedirect(reverse('resource_detail',
                                                args=[project_slug,
                                                      resource_slug]),)
        else:
            return HttpResponseRedirect(reverse('project_detail',
                                                args=[project_slug]),)

    total_strings = SourceEntity.objects.filter(
        resource__in = resources).count()

    translated_strings = Translation.objects.filter(
        source_entity__resource__in = resources,
        language = target_language,
        source_entity__pluralized=False,
        rule = 5).count()

    # Include counting of pluralized entities
    for pluralized_entity in SourceEntity.objects.filter(resource__in = resources,
                                                         pluralized=True):
        plurals_translated = Translation.objects.filter(
            language=target_language,
            source_entity=pluralized_entity).count()
        if plurals_translated == len(target_language.get_pluralrules()):
            translated_strings += 1

    if len(resources) > 1:
        translation_resource = None
    else:
        translation_resource = resources[0]

    contributors = User.objects.filter(pk__in=Translation.objects.filter(
        source_entity__resource__in = resources,
        language = target_language,
        rule = 5).values_list("user", flat=True))

    lotte_init.send(None, request=request, resources=resources,
        language=target_language)

    return render_to_response("translate.html",
        { 'project' : project,
          'resource' : translation_resource,
          'target_language' : target_language,
          'translated_strings': translated_strings,
          'untranslated_strings': total_strings - translated_strings,
          'contributors': contributors,
          'resources': resources,
          'resource_slug': resource_slug,
          'languages': Language.objects.all(),
          'gtranslate': is_gtranslate_allowed(project),
          'spellcheck_supported_langs' : SPELLCHECK_SUPPORTED_LANGS,
        },
        context_instance = RequestContext(request))

@login_required
def exit(request, project_slug, lang_code, resource_slug=None, *args, **kwargs):
    """
    Exiting Lotte
    """

    # Permissions handling
    # Project should always be available
    project = get_object_or_404(Project, slug=project_slug)
    team = Team.objects.get_or_none(project, lang_code)
    check = ProjectPermission(request.user)
    if not check.submit_translations(team or project) and not\
        check.maintain(project):
        return permission_denied(request)

    language = Language.objects.by_code_or_alias(lang_code)

    resources = []
    if resource_slug:
        resources = Resource.objects.filter(slug=resource_slug, project=project)
        if not resources:
            raise Http404
        url = reverse('resource_detail', args=[project_slug, resource_slug])
    else:
        resources = Resource.objects.filter(project=project)
        url = reverse('project_detail', args=[project_slug])

    if request.POST.get('updated', None) == 'true':
        modified = True
        # ActionLog & Notification
        for resource in resources:
            nt = 'project_resource_translated'
            context = {'project': project,
                       'resource': resource,
                       'language': language}
            object_list = [project, resource, language]
            action_logging(request.user, object_list, nt, context=context)
            if settings.ENABLE_NOTICES:
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
    else:
        modified = False

    lotte_done.send(None, request=request, resources=resources,
        language=language, modified=modified)

    if request.is_ajax():
        json = simplejson.dumps(dict(redirect=url))
        return HttpResponse(json, mimetype='application/json')

    return HttpResponseRedirect(url)


# Restrict access only for private projects
# Allow even anonymous access on public projects
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
def view_strings(request, project_slug, lang_code, resource_slug=None,
                 *args, **kwargs):
    """
    View for observing the translations strings on a specific language.
    """

    resource = get_object_or_404(Resource,
        slug = resource_slug,
        project__slug = project_slug
    )
    try:
        target_language = Language.objects.by_code_or_alias(lang_code)
    except Language.DoesNotExist:
        raise Http404

    total_strings = SourceEntity.objects.filter(
        resource=resource).count()

    translated_strings = Translation.objects.filter(
        source_entity__resource=resource,
        language=target_language,
        rule=5).count()

    return render_to_response("view_strings.html",
        { 'project' : resource.project,
          'resource' : resource,
          'target_language' : target_language,
          'translated_strings': translated_strings,
          'untranslated_strings': total_strings - translated_strings,
        },
        context_instance = RequestContext(request))


#FIXME: Find a more clever way to do it, to avoid putting placeholders.
SORTING_DICT=( 'id', 'id', 'string')

# Restrict access only for private projects since this is used to fetch stuff!
# Allow even anonymous access on public projects
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
def stringset_handling(request, project_slug, lang_code, resource_slug=None,
                     *args, **kwargs):
    """
    Function to serve AJAX data to the datatable holding the translating
    stringset.
    """
    resources = []
    if resource_slug:
        try:
            resources = [ Resource.objects.get(slug=resource_slug,
                                    project__slug = project_slug) ]
        except Resource.DoesNotExist:
            raise Http404
    else:
        resources = Resource.objects.filter(project__slug = project_slug)

    try:
        language = Language.objects.by_code_or_alias(lang_code)
    except Language.DoesNotExist:
        raise Http404

    # FIXME Do we need to check for non-POST requests and return an error?
    return _get_stringset(request.POST, resources, language)


def _get_stringset(post_data, resources, language, *args, **kwargs):
    """Return the source strings for the specified resources and language
    based on the filters active in the request.

    Filters are: translated|untranslated, specific user and specific
    resources, which must be a subset of the resources argument. Also, the
    user can select to search for a term, sort the columns and show more
    languages other than the selected one.
    """
    # Find a way to determine the source language of multiple resources #FIXME
    source_language = get_source_language(resources)
    try:
        source_strings = _get_source_strings_for_request(
            post_data, resources, source_language, language
        )
    except LotteBadRequestError, e:
        logger.warning("Error in lotte filters: %s" % e.message, excinfo=True)
        return HttpResponseBadRequest()

    translated_strings = Translation.objects.filter(
        source_entity__resource__in=resources,
        language=language)

    if not isinstance(source_strings, list):
        more_languages = []
        if post_data and post_data.has_key('more_languages'):
            # rsplit is used to remove the trailing ','
            more_languages = post_data.get('more_languages').rstrip(',').split(',')

        # keyword filtering
        sSearch = post_data.get('sSearch','')
        if not sSearch == '':
            query = Q()
            for term in sSearch.split(' '):
                query &= Q(string__icontains=term)
            source_strings = source_strings.filter(query)

        # sorting
        scols = post_data.get('iSortingCols', '0')
        for i in range(0,int(scols)):
            if post_data.has_key('iSortCol_'+str(i)):
                col = int(post_data.get('iSortCol_'+str(i)))
                if post_data.has_key('sSortDir_'+str(i)) and \
                    post_data['sSortDir_'+str(i)] == 'asc':
                    source_strings=source_strings.order_by(SORTING_DICT[col])
                else:
                    source_strings=source_strings.order_by(SORTING_DICT[col]).reverse()

        # for statistics
        total = source_strings.count()
    else:
        total = 0

    # for items displayed
    try:
        dlength = int(post_data.get('iDisplayLength','50'))
        dstart = int(post_data.get('iDisplayStart','0'))
    except ValueError, e:
        return HttpResponseBadRequest()

    # NOTE: It's important to keep the translation string matching inside this
    # iteration to prevent extra un-needed queries. In this iteration only the
    # strings displayed are calculated, saving a lot of resources.
    json = simplejson.dumps({
        'sEcho': post_data.get('sEcho','1'),
        'iTotalRecords': total,
        'iTotalDisplayRecords': total,
        'aaData': [
            [
                # 1. Translation object's "id"
                s.id,
                # 2. SourceEntity object's "string" content
                s.source_entity.string,
                # 3. Get all the necessary source strings, including plurals and
                # similar langs, all in a dictionary (see also below)
                _get_source_strings(s, source_language, language.code, more_languages),
                # 4. Get all the Translation strings mapped with plural rules
                # in a single dictionary (see docstring of function)
                _get_strings(translated_strings, language.code, s.source_entity),
                # 5. A number which indicates the number of Suggestion objects
                # attached to this row of the table.
                Suggestion.objects.filter(source_entity=s.source_entity, language__code=language.code).count(),
                # 6. save buttons and hidden context (ready to inject snippet)
                # It includes the following content, wrapped in span tags:
                # * SourceEntity object's "context" value
                # * SourceEntity object's "id" value
                ('<span class="i16 save buttonized_simple" id="save_' + str(counter) + '" style="display:none;border:0" title="' + _("Save the specific change") + '"></span>'
                 '<span class="i16 spellcheck buttonized_simple" id="spellcheck_' + str(counter) + '" style="display:none;border:0" title="' + _("Check spelling") + '"></span>'
                 '<span class="i16 undo buttonized_simple" id="undo_' + str(counter) + '" style="display:none;border:0" title="' + _("Undo to initial text") + '"></span>'
                 '<span class="context" id="context_' + str(counter) + '" style="display:none;">' + escape(str(s.source_entity.context_string.encode('UTF-8'))) + '</span>'
                 '<span class="source_id" id="sourceid_' + str(counter) + '"style="display:none;">' + str(s.source_entity.id) + '</span>'),
            ] for counter,s in enumerate(source_strings[dstart:dstart+dlength])
        ],
        })
    return HttpResponse(json, mimetype='application/json')


def _get_source_strings_for_request(post_data, resources, source_language, language):
    """Return the source strings that correspond to the filters in the request.

    Use powers of two for each possible filter, so that we can get a unique
    number for each possible combination. Use that number as index to call
    the specialized for the combination function.
    This allows to optimize queries based on the specific filters applied
    and bypass the database for combinations which are guaranteed to return
    empty results.
    """
    # FIXME Is this possible?
    if not post_data:
        return Translation.objects.filter(
            source_entity__resource__in=resources,
            language=source_language,
            rule=5
        )

    if 'resource_filters' in post_data:
        requested_resources = set(
            post_data['resource_filters'].rstrip(',').split(',')
        )
        resources = filter(lambda r: r in requested_resources, resources)

    # FIXME handle exceptions
    index = 0
    if 'filters' in post_data:
        # Handle 'translated'/'untranslated' filter
        select = post_data['filters'].rstrip(',').split(',')
        if len(select) == 2 and 'translated' and 'untranslated' in select:
            index += 3
        elif select[0] == 'translated' and len(select) == 1:
            index += 1
        elif select[0] == 'untranslated' and len(select) == 1:
            index += 2
        else:
            raise LotteBadRequestError('Invalid filter: %s' % select[0])

    users = None
    if 'user_filters' in post_data:
        try:
            users = map(int, post_data['user_filters'].rstrip(',').split(','))
        except ValueError, e:
            raise LotteBadRequestError(
                "Invalid user id specified: %s" % post_data['user_filters']
            )
        index += 4

    querysets = [
        _get_all_source_strings,
        _get_untranslated_source_strings,
        _get_translated_source_strings,
        _get_none_source_strings,
        _get_user_filtered_source_strings,
        _get_user_filtered_source_strings,
        _get_none_source_strings,
        _get_none_source_strings,
    ]

    return querysets[index](
        resources=resources,
        language=language,
        users=users
    )


def _get_all_source_strings(resources, *args, **kwargs):
    """Return all source strings for the resources."""
    return Translation.objects.source_strings(resources)


def _get_untranslated_source_strings(resources, language, *args, **kwargs):
    """
    Get only the source strigns that haven't been translated
    in the specified language.
    """
    return Translation.objects.untranslated_source_strings(resources, language)


def _get_translated_source_strings(resources, language, *args, **kwargs):
    """
    Get only the source strigns that haven't been translated
    in the specified language.
    """
    return Translation.objects.translated_source_strings(resources, language)


def _get_none_source_strings(*args, **kwargs):
    """Return an empty set.

    There are combinations that return emty sets, so let's optimize those
    cases and return an empty set without querying the database.
    """
    return []


def _get_user_filtered_source_strings(resources, users, language, *args, **kwargs):
    """Return all source strings created/edited by the specified users."""
    return Translation.objects.user_translated_strings(resources, language, users)


def _get_source_strings(source_string, source_language, lang_code, more_languages):
    """
    Get all the necessary source strings, including plurals and similar langs.

    Returns a dictionary with the keys:
    'source_strings' : {"one":<string>, "two":<string>, ... , "other":<string>}
    'similar_lang_strings' :
        {"lang1": {"one":<string>, ... , "other":<string>},
         "lang2": {"one":<string>, "two":<string>, ... , "other":<string>}}
    """
    source_entity = source_string.source_entity
    # This is the rule 5 ('other')
    source_strings = { "other":source_string.string }
    # List that will contain all the similar translations
    similar_lang_strings = {}

    if source_entity.pluralized:
        # These are the remaining plural forms of the source string.
        plural_strings = Translation.objects.filter(
            source_entity = source_entity,
            language = source_language).exclude(rule=5).order_by('rule')
        for pl_string in plural_strings:
            plural_name = source_language.get_rule_name_from_num(pl_string.rule)
            source_strings[plural_name] = pl_string.string

    # for each similar language fetch all the translation strings
    for lang_id in more_languages:
        l = Language.objects.get(pk=lang_id)
        similar_lang_strings[l.name] = {}
        for t in Translation.objects.filter(source_entity=source_entity, language=l).order_by('rule'):
            plural_name = source_language.get_rule_name_from_num(t.rule)
            similar_lang_strings[l.name][plural_name] = t.string
    return { 'source_strings' : source_strings,
             'similar_lang_strings' : similar_lang_strings }


def _get_strings(query, target_lang_code, source_entity):
    """
    Helper function for returning all the Translation strings or an empty dict.

    Used in the list concatenation above to preserve code sanity.
    Returns a dictionary in the following form:
    {"zero":<string>, "one":<string>, ... , "other":<string>},
    where the 'zero', 'one', ... are the plural names of the corresponding
    plural forms.
    """
    # It includes the plural translations, too!
    translation_strings = {}
    target_language = Language.objects.by_code_or_alias(target_lang_code)
    if source_entity.pluralized:
        translations = query.filter(source_entity=source_entity).order_by('rule')
        # Fill with empty strings to have the Untranslated entries!
        for rule in target_language.get_pluralrules():
            translation_strings[rule] = ""
        for translation in translations:
            plural_name = target_language.get_rule_name_from_num(translation.rule)
            translation_strings[plural_name] = translation.string
    else:
        try:
            translation_strings["other"] = query.get(source_entity=source_entity,
                                                     rule=5).string
        except Translation.DoesNotExist:
            translation_strings["other"] = ""
    return translation_strings


# Restrict access only to : (The checks are done in the view's body)
# 1)those belonging to the specific language team (coordinators or members)
# 2)project maintainers
# 3)global submitters (perms given through access control tab)
# 4)superusers
# CAUTION!!! WE RETURN 404 instead of 403 for security reasons
@login_required
def push_translation(request, project_slug, lang_code, *args, **kwargs):
    """
    Client pushes an id and a translation string.

    Id is considered to be of the source translation string and the string is
    in the target_lang.

    FIXME: Document in detail the form of the 'strings' POST variable.
    """

    # Permissions handling
    # Project should always be available
    project = get_object_or_404(Project, slug=project_slug)
    team = Team.objects.get_or_none(project, lang_code)
    check = ProjectPermission(request.user)
    if not check.submit_translations(team or project) and not\
        check.maintain(project):
        return permission_denied(request)

    if not request.POST:
        return HttpResponseBadRequest()

    data = simplejson.loads(request.raw_post_data)
    strings = data["strings"]

    try:
        target_language = Language.objects.by_code_or_alias(lang_code)
    except Language.DoesNotExist:
        raise Http404

    # This dictionary will hold the results of the save operation and will map
    # status code for each translation pushed, to indicate the result on each
    # translation push separately.
    push_response_dict = {}

    # Form the strings dictionary, get as Json object
    # The fields are the following:
    # id-> source_entity id
    # translations-> translation strings (includes all plurals)
    # context-> source_entity context
    # occurrence-> occurrence (not yet well supported)
    # Iterate through all the row data that have been sent.
    for row in strings:
        source_id = int(row['id'])
        try:
            source_string = Translation.objects.select_related().get(
                id=source_id, source_entity__resource__project=project
            )
        except Translation.DoesNotExist:
            # TODO: Log or inform here
            push_response_dict[source_id] = { 'status':400,
                 'message':_("Source string cannot be identified in the DB")}
            # If the source_string cannot be identified in the DB then go to next
            # translation pair.
            continue

        if not source_string.source_entity.resource.accept_translations:
            push_response_dict[source_id] = { 'status':400,
                 'message':_("The resource of this source string is not "
                    "accepting translations.") }

        # If the translated source string is pluralized check that all the
        # source language supported rules have been filled in, else return error
        # and donot save the translations.
        if source_string.source_entity.pluralized:
            error_flag = False
            for rule in target_language.get_pluralrules():
                if rule in row['translations'] and row['translations'][rule] != "":
                    continue
                else:
                    error_flag = True
            if error_flag:
                error_flag = False
                # Check also if all of them are "". If yes, delete all the plurals!
                for rule in target_language.get_pluralrules():
                    if rule in row['translations'] and row['translations'][rule] == "":
                        continue
                    else:
                        error_flag = True
            if error_flag:
                push_response_dict[source_id] = { 'status':400,
                    'message':(_("Cannot save unless plural translations are either "
                               "completely specified or entirely empty!"))}
                # Skip the save as we hit on an error.
                continue
        try:
            msgs = _save_translation(
                source_string, row['translations'],
                target_language, request.user
            )
            if not msgs:
                push_response_dict[source_id] = {'status': 200}
            else:
                push_response_dict[source_id] = {
                    'status': 200, 'message': msgs[-1]
                }
        except LotteBadRequestError, e:
            push_response_dict[source_id] = {
                'status': 400, 'message': e.message
            }
        except Exception, e:
            logger.error(
                "Unexpected exception raised: %s" % e.message, exc_info=True
            )
            push_response_dict[source_id] = {
                'status': 400, 'message': e.message
            }

    json_dict = simplejson.dumps(push_response_dict)
    return HttpResponse(json_dict, mimetype='application/json')


@transaction.commit_on_success
def _save_translation(source_string, translations, target_language, user):
    """Save a translation string to the database.

    This functions handle a signle source entity translation
    (could be pluralized).

    Currently, the function only returns warning strings.
    There is no message for success.

    Args:
        source_string: A Translation object of the string in the source
            language.
        translations: A (rule, string) tuple.
        target_language: The language the string is translated to.
        user: The translator.
    Returns:
        A list if strings to display to the user.
    Raises:
        An LotteBadRequestError exception in case of errors.
    """
    source_id = source_string.pk
    resource = source_string.source_entity.resource
    source_language = resource.source_language
    warnings = []

    for rule, target_string in translations.items():
        rule = target_language.get_rule_num_from_name(rule)
        if rule != 5:
            # fetch correct source string for plural rule
            try:
                source_string = Translation.objects.get(
                    source_entity=source_string.source_entity,
                    language=source_language, rule=rule
                )
            except Translation.DoesNotExist:
                # target language has extra plural forms
                pass
        ss = unescape(source_string.string)
        tr = unescape(target_string)

        # check for errors
        try:
            for ErrorValidator in create_error_validators(resource.i18n_type):
                v = ErrorValidator(source_language, target_language, rule)
                v(ss, tr)
        except ValidationError, e:
            raise LotteBadRequestError(e.message)
        # check for warnings
        for WarningValidator in create_warning_validators(resource.i18n_type):
            v = WarningValidator(source_language, target_language, rule)
            try:
                v(ss, tr)
            except ValidationError, e:
                warnings.append(e.message)
        try:
            # TODO: Implement get based on context and/or on context too!
            translation_string = Translation.objects.get(
                source_entity=source_string.source_entity,
                language = target_language,
                source_entity__resource = resource, rule = rule
            )

            # FIXME: Maybe we don't want to permit anyone to delete!!!
            # If an empty string has been issued then we delete the translation.
            if target_string == "":
                translation_string.delete()
            else:
                translation_string.string = target_string
                translation_string.user = user
                translation_string.save()

            _add_copyright(source_string, target_language, user)
            invalidate_stats_cache(resource, target_language, user=user)
        except Translation.DoesNotExist:
            # Only create new if the translation string sent, is not empty!
            if target_string != "":
                Translation.objects.create(
                    source_entity=source_string.source_entity, user=user,
                    language=target_language, rule=rule, string=target_string
                )
                _add_copyright(source_string, target_language, user)
                invalidate_stats_cache(resource, target_language, user=user)
            else:
                # In cases of pluralized translations, sometimes only one
                # translation will exist and the rest plural forms will be
                # empty. If the user wants to delete all of them, we need
                # to let by the ones that don't already have a translation.
                if not source_string.source_entity.pluralized:
                    raise LotteBadRequestError(
                        _("The translation string is empty")
                    )
        # catch-all. if we don't save we _MUST_ inform the user
        except Exception, e:
            msg = _(
                "Error occurred while trying to save translation: %s", e.message
            )
            logger.error(msg, exc_info=True)
            raise LotteBadRequestError(msg)
    return warnings


def _add_copyright(source_string, target_language, user):
    profile_user = get_profile_or_user(user)
    firstname = profile_user.first_name
    surname = profile_user.last_name
    from transifex.addons.copyright.handlers import save_copyrights
    lotte_save_translation.connect(save_copyrights)
    lotte_save_translation.send(
        None, resource=source_string.source_entity.resource,
        language=target_language,
        copyrights=([(
                    ''.join([firstname, ' ', surname,
                             ' <', user.email, '>']),
                    [str(date.today().year)]
        ), ])
    )


# Restrict access only for private projects since this is used to fetch stuff
# Allow even anonymous access on public projects
def tab_details_snippet(request, entity_id, lang_code):
    """Return a template snippet with entity & translation details."""

    source_entity = get_object_or_404(SourceEntity, pk=entity_id)

    check = ProjectPermission(request.user)
    if not check.private(source_entity.resource.project):
        return permission_denied(request)

    language = get_object_or_404(Language, code=lang_code)
    translation = source_entity.get_translation(language.code)

    return list_detail.object_detail(request,
        queryset=SourceEntity.objects.all(),
        object_id=entity_id,
        template_name="tab_details_snippet.html",
        template_object_name='source_entity',
        extra_context={"translation": translation,
            "project": source_entity.resource.project})


# Restrict access only for private projects since this is used to fetch stuff
# Allow even anonymous access on public projects
def tab_suggestions_snippet(request, entity_id, lang_code):
    """Return a template snippet with entity & translation details."""

    source_entity = get_object_or_404(SourceEntity, pk=entity_id)

    check = ProjectPermission(request.user)
    if not check.private(source_entity.resource.project):
        return permission_denied(request)

    current_translation = source_entity.get_translation(lang_code)

    return render_to_response("tab_suggestions_snippet.html", {
        'source_entity': source_entity,
        'lang_code': lang_code,
        'current_translation': current_translation
        },
    context_instance = RequestContext(request))


# Restrict access only to :
# 1)project maintainers
# 2)superusers
@one_perm_required_or_403(pr_resource_translations_delete,
                          (Project, "slug__exact", "project_slug"))
def delete_translation(request, project_slug=None, resource_slug=None,
                        lang_code=None):
    """
    Delete a list of translations according to the post request.
    """

    if not request.POST:
        return HttpResponseBadRequest()

    project = get_object_or_404(Project, slug=project_slug)

    resource = get_object_or_404(Resource, slug=resource_slug, project=project)
    language = get_object_or_404(Language, code=lang_code)
    data = simplejson.loads(request.raw_post_data)
    to_delete = data["to_delete"]
    ids = []
    # Ensure that there are no empty '' ids
    for se_id in to_delete:
        if se_id:
            ids.append(se_id)


    try:
        translations = Translation.objects.filter(source_entity__pk__in=ids,
                                   language=language)

        translations.delete()
#        request.user.message_set.create(
#            message=_("Translations deleted successfully!"))
    except:
#        request.user.message_set.create(
#            message=_("Failed to delete translations due to some error!"))
        raise Http404

    invalidate_stats_cache(resource, language, user=request.user)

    return HttpResponse(status=200)

def spellcheck(request, project_slug, lang_code, resource_slug=None):
    """
    Shows mispelled words along with suggestions
    """
    data = simplejson.loads(request.raw_post_data)
    lang_code = lang_code.encode('utf-8')
    string = escape(data["text"])
    string_ = string
    string = string.encode('utf-8')
    lang_codes = SPELLCHECK_SUPPORTED_LANGS
    if lang_code in lang_codes:
        xmlData = '''<?xml version="1.0" encoding="UTF-8" ?>
                        <spellrequest textalreadyclipped="0" ignoredups="1" ignoredigits="1" ignoreallcaps="1" suggestedlang="%s">
                            <text>%s</text>
                        </spellrequest>'''%(lang_code, string)
        headers = {"Content-type": "text/xml; charset=utf-8",
                   "Request-number":"1",
                   "Document-type":"Request",
                   "Connection":"close"}
        con = httplib.HTTPSConnection('www.google.com')
        con.request('POST', '/tbproxy/spell?lang=%s'%(lang_code), xmlData, headers)
        response = con.getresponse().read().decode('utf-8')
        pattern = re.compile(r'<c o="(?P<o>\d*)" l="(?P<l>\d*)" s="\d*">(?P<suggestions>[^<]*)<\/c>', re.UNICODE)
        matches = pattern.findall(response)
        d = []
        for i in matches:
            o = int(i[0])
            l = int(i[1])
            suggestions = i[2].split('\t')
            word = string_[o:o+l].strip()
            d.append([word, suggestions])
    else:
        d = []
    json_dict = simplejson.dumps(d, 'utf-8')
    return HttpResponse(json_dict, mimetype='application/json')



@login_required
def add_edit_developer_comment_extra(request, project_slug, *args, **kwargs):
    """
    View for handling AJAX calls from Lotte in order to add/edit the
    developer comment for a source entity.

    Only maintainers can edit it.
    """

    # Permissions handling
    project = get_object_or_404(Project, slug=project_slug)
    check = ProjectPermission(request.user)
    if not check.maintain(project):
        content = {'error': True, 'message': _('Permission error.')}
    elif not request.POST:
        content = {'error': True, 'message': _('Bad request.')}
    else:
        previous_comment = None
        try:
            se = SourceEntity.objects.get(
                id=request.POST.get('source_entity_id', None),
                resource__project=project)
            previous_comment_extra = se.developer_comment_extra
            se.developer_comment_extra = request.POST.get('comment_extra', '')
            se.save()
            content = {
                'error': False,
                'comment': se.developer_comment,
                'comment_extra': se.developer_comment_extra,
                }
        except SourceEntity.DoesNotExist:
            content = {
                'error': True,
                'message': _('No such Source Entity for the given project.'),
                }
        except Exception, e:
            logger.error('Lotte: Error while editing developer comment: %s' %
                (e.message or str(e)))
            content = {
                'error': True,
                'message': _('Ops! Something weird happened. The admins '
                    'were notified about it.'),
                }

    return HttpResponse(simplejson.dumps(content), mimetype='application/json')
