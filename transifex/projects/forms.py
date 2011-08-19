from django import forms
from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.db.models import permalink
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.forms import widgets
from django.utils.encoding import force_unicode
from django.utils.safestring import mark_safe

from transifex.resources.models import Resource
from ajax_select.fields import AutoCompleteSelectMultipleField

from transifex.projects.models import Project
from transifex.projects.signals import (project_access_control_form_start,
                                        project_form_init, project_form_save)
from transifex.releases.models import (Release, RELEASE_ALL_DATA,
                                       RESERVED_RELEASE_SLUGS)
from transifex.txcommon.widgets import SplitSelectDateTimeWidget

class ProjectForm(forms.ModelForm):
    maintainers = AutoCompleteSelectMultipleField('users', required=True,
        help_text=_('Search for a username'))

    class Meta:
        model = Project
        exclude = ('anyone_submit', 'outsource', 'owner')

    def __init__(self, *args, **kwargs):
        super(ProjectForm, self).__init__(*args, **kwargs)
        project_form_init.send(sender=ProjectForm, form=self)

    def save(self, *args, **kwargs):
        retval = super(ProjectForm, self).save(*args, **kwargs)
        project_form_save.send(sender=ProjectForm, form=self, instance=retval)
        return retval

class RadioFieldRenderer(widgets.RadioFieldRenderer):
    """
    An object used by RadioSelect to enable customization of radio widgets.
    """
    def get_class(self, v, v2):
        """
        Return the string 'selected' if both values are equal.

        This is used to set a class attr on the selected radio button.
        """
        if v==v2:
            return 'selected'
        else:
            return ''

    def render(self):
        """Outputs a <ul> for this set of radio fields."""
        help_text = self.attrs['help_text']
        self.attrs.pop('help_text')
        return mark_safe(u'<ul>\n%s\n</ul>' % u'\n'.join(
            [u'<li class="%s"><span>%s</span><p class="helptext">%s</p></li>'
                % (self.get_class(w.value, w.choice_value),
                   force_unicode(w),
                   help_text[w.choice_value]) for w in self]))


class ProjectAccessControlForm(forms.ModelForm):
    """Form to handle the Access Control options of a project."""
    access_control_options=[
        {'free_for_all': {
            'label': _('Free for all'),
            'help_text': _(
                "Allow any logged-in user to submit files to my project. "
                "<a href=\"http://www.youtube.com/watch?v=-b7qaSxuZUg\" "
                "target=\"_blank\">Imagine</a> all the people, sharing all the "
                "world. Recommended for quick translations, and when a "
                "pre-commit review process is in place, e.g. when "
                "contributions are submitted by email or to a separate branch.")
            }
        },
        {'limited_access': {
            'label': _('Limited access'),
            'help_text': _(
                "Give access to specific people. Translations teams will have "
                "access to their language's files only, and global writers "
                "will have access to all translation files. Recommended for "
                "most projects."),
            }
        },
        {'outsourced_access': {
            'label': _('Outsourced access'),
            'help_text': _(
                "Re-use another project's teams and writers by delegating "
                "access control to that project. If a person can contribute to "
                "that project, it can contribute to this one as well. "
                "Recommended for non-upstream projects such as distribution "
                "packages and desktop environment modules."),
            }
        },
    ]

    # Setting up some vars based on the 'access_control_options' var
    access_control_types = []
    access_control_help = {}
    for o in access_control_options:
        for k, v in o.items():
            access_control_types.append((k, v['label']))
            access_control_help.update({k: v['help_text']})

    # Add field
    access_control = forms.ChoiceField(choices=access_control_types,
        required=True, widget=forms.RadioSelect(
            renderer=RadioFieldRenderer,
            attrs={'help_text': access_control_help }))

    class Meta:
        model = Project
        fields = ('access_control', 'outsource')

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super(ProjectAccessControlForm, self).__init__(*args, **kwargs)
        # Changing some field settings based on the project attr and the
        # request.DATA
        project = kwargs.get('instance', None)
        outsource_required = False
        if args:
            access_control_initial = args[0]['access_control']
            if 'outsourced_access' == access_control_initial:
                outsource_required = True
        elif project:
            if project.anyone_submit:
                access_control_initial = 'free_for_all'
            elif project.outsource:
                access_control_initial = 'outsourced_access'
                outsource_required = True
            else:
                access_control_initial = 'limited_access'

        self.fields['access_control'].initial = access_control_initial
        self.fields['outsource'].required = outsource_required

        # Filtering project list

        if user:
            self.fields["outsource"].queryset = Project.objects.for_user(
                user).exclude(slug=project.slug)
        else:
            projects = self.fields["outsource"].queryset.\
                    exclude(slug=project.slug).exclude(private=True)
            self.fields["outsource"].queryset = projects
        project_access_control_form_start.send(sender=ProjectAccessControlForm,
                                               instance=self, project=project)


class ReleaseForm(forms.ModelForm):

    resources = AutoCompleteSelectMultipleField('resources', required=True,
        help_text=_('Search for a resource'))

    # FIXME: Weird, the following message should be displayed by default, but
    # it was necessary to make it explicit here be able to display the correct 
    # 'invalid' message for datetime fields, which has a suggestion of the 
    # format to be used.
    error_messages = {'invalid': _('Enter a valid date/time in '
        'YYYY-MM-DD HH:MM[:ss[.uuuuuu]] format.')}
    release_date = forms.DateTimeField(required=False,
        error_messages=error_messages)
    stringfreeze_date = forms.DateTimeField(required=False,
        error_messages=error_messages)
    develfreeze_date = forms.DateTimeField(required=False,
        error_messages=error_messages)

    class Meta:
        model = Release

    def __init__(self, project, user, *args, **kwargs):
        super(ReleaseForm, self).__init__(*args, **kwargs)
        projects = self.fields["project"].queryset.filter(slug=project.slug)
        self.fields["project"].queryset = projects
        self.fields["project"].empty_label = None
        self.user = user

    def clean_resources(self):
        resources_pk_list = self.cleaned_data['resources']
        for resource_pk in resources_pk_list:
            try:
                resource = Resource.objects.select_related().get(pk=resource_pk)
            except Resource.DoesNotExist, e:
                raise ValidationError(_("Invalid resource used."))
            if resource.project.private:
                if self.user not in resource.project.maintainers.all():
                    raise ValidationError(
                     _("%s is an unaccessible private resource."
                       "Remove it!" % resource.name)
                    )
        return resources_pk_list

    def clean_slug(self):
        """Ensure that reserved slugs are not used."""
        slug = self.cleaned_data['slug']
        if slug in RESERVED_RELEASE_SLUGS:
            raise ValidationError(_("This value is reserved and cannot be used."))
        return slug

    def clean(self):
        """Check whether the dates of the release are valid."""
        cleaned_data = self.cleaned_data
        stringfreeze_date = cleaned_data.get('stringfreeze_date')
        develfreeze_date = cleaned_data.get('develfreeze_date')
        release_date = cleaned_data.get('release_date')

        if develfreeze_date and stringfreeze_date and \
            develfreeze_date <= stringfreeze_date:
            msg = _("Devel freeze date must be after the String freeze date.")
            self._errors["develfreeze_date"] = self.error_class([msg])
            del cleaned_data["develfreeze_date"]

        if release_date and stringfreeze_date and \
            release_date <= stringfreeze_date:
            msg = _("Release date must be after the String freeze date.")
            self._errors["release_date"] = self.error_class([msg])
            del cleaned_data["release_date"]

        elif release_date and develfreeze_date and \
            release_date <= develfreeze_date:
            msg = _("Release date must be after the Devel freeze date.")
            self._errors["release_date"] = self.error_class([msg])
            del cleaned_data["release_date"]

        return cleaned_data
