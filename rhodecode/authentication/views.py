# -*- coding: utf-8 -*-

# Copyright (C) 2012-2016  RhodeCode GmbH
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License, version 3
# (only), as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# This program is dual-licensed. If you wish to learn more about the
# RhodeCode Enterprise Edition, including its added features, Support services,
# and proprietary license terms, please see https://rhodecode.com/licenses/

import colander
import formencode.htmlfill
import logging

from pyramid.httpexceptions import HTTPFound
from pyramid.i18n import TranslationStringFactory
from pyramid.renderers import render
from pyramid.response import Response

from rhodecode.authentication.base import get_auth_cache_manager
from rhodecode.authentication.interface import IAuthnPluginRegistry
from rhodecode.lib import auth
from rhodecode.lib.auth import LoginRequired, HasPermissionAllDecorator
from rhodecode.model.forms import AuthSettingsForm
from rhodecode.model.meta import Session
from rhodecode.model.settings import SettingsModel

log = logging.getLogger(__name__)

_ = TranslationStringFactory('rhodecode-enterprise')


class AuthnPluginViewBase(object):

    def __init__(self, context, request):
        self.request = request
        self.context = context
        self.plugin = context.plugin

    # TODO: Think about replacing the htmlfill stuff.
    def _render_and_fill(self, template, template_context, request,
                         form_defaults, validation_errors):
        """
        Helper to render a template and fill the HTML form fields with
        defaults. Also displays the form errors.
        """
        # Render template to string.
        html = render(template, template_context, request=request)

        # Fill the HTML form fields with default values and add error messages.
        html = formencode.htmlfill.render(
            html,
            defaults=form_defaults,
            errors=validation_errors,
            prefix_error=False,
            encoding="UTF-8",
            force_defaults=False)

        return html

    def settings_get(self):
        """
        View that displays the plugin settings as a form.
        """
        form_defaults = {}
        validation_errors = None
        schema = self.plugin.get_settings_schema()

        # Get default values for the form.
        for node in schema.children:
            value = self.plugin.get_setting_by_name(node.name) or node.default
            form_defaults[node.name] = value

        template_context = {
            'resource': self.context,
            'plugin': self.context.plugin
        }

        return Response(self._render_and_fill(
            'rhodecode:templates/admin/auth/plugin_settings.html',
            template_context,
            self.request,
            form_defaults,
            validation_errors))

    def settings_post(self):
        """
        View that validates and stores the plugin settings.
        """
        schema = self.plugin.get_settings_schema()
        try:
            valid_data = schema.deserialize(self.request.params)
        except colander.Invalid, e:
            # Display error message and display form again.
            form_defaults = self.request.params
            validation_errors = e.asdict()
            self.request.session.flash(
                _('Errors exist when saving plugin settings. '
                    'Please check the form inputs.'),
                queue='error')

            template_context = {
                'resource': self.context,
                'plugin': self.context.plugin
            }

            return Response(self._render_and_fill(
                'rhodecode:templates/admin/auth/plugin_settings.html',
                template_context,
                self.request,
                form_defaults,
                validation_errors))

        # Store validated data.
        for name, value in valid_data.items():
            self.plugin.create_or_update_setting(name, value)
        Session.commit()

        # Display success message and redirect.
        self.request.session.flash(
            _('Auth settings updated successfully.'),
            queue='success')
        redirect_to = self.request.resource_path(
            self.context, route_name='auth_home')
        return HTTPFound(redirect_to)


# TODO: Ongoing migration in these views.
# - Maybe we should also use a colander schema for these views.
class AuthSettingsView(object):
    def __init__(self, context, request):
        self.context = context
        self.request = request

        # TODO: Move this into a utility function. It is needed in all view
        # classes during migration. Maybe a mixin?

        # Some of the decorators rely on this attribute to be present on the
        # class of the decorated method.
        self._rhodecode_user = request.user

    @LoginRequired()
    @HasPermissionAllDecorator('hg.admin')
    def index(self, defaults={}, errors=None, prefix_error=False):
        authn_registry = self.request.registry.getUtility(IAuthnPluginRegistry)
        default_plugins = ['egg:rhodecode-enterprise-ce#rhodecode']
        enabled_plugins = SettingsModel().get_auth_plugins() or default_plugins

        # Create template context and render it.
        template_context = {
            'resource': self.context,
            'available_plugins': authn_registry.get_plugins(),
            'enabled_plugins': enabled_plugins,
        }
        html = render('rhodecode:templates/admin/auth/auth_settings.html',
                      template_context,
                      request=self.request)

        # Create form default values and fill the form.
        form_defaults = {
            'auth_plugins': ','.join(enabled_plugins)
        }
        form_defaults.update(defaults)
        html = formencode.htmlfill.render(
            html,
            defaults=form_defaults,
            errors=errors,
            prefix_error=prefix_error,
            encoding="UTF-8",
            force_defaults=False)

        return Response(html)

    @LoginRequired()
    @HasPermissionAllDecorator('hg.admin')
    @auth.CSRFRequired()
    def auth_settings(self):
        try:
            form = AuthSettingsForm()()
            form_result = form.to_python(self.request.params)
            plugins = ','.join(form_result['auth_plugins'])
            setting = SettingsModel().create_or_update_setting(
                'auth_plugins', plugins)
            Session().add(setting)
            Session().commit()

            cache_manager = get_auth_cache_manager()
            cache_manager.clear()
            self.request.session.flash(
                _('Auth settings updated successfully.'),
                queue='success')
        except formencode.Invalid as errors:
            e = errors.error_dict or {}
            self.request.session.flash(
                _('Errors exist when saving plugin setting. '
                  'Please check the form inputs.'),
                queue='error')
            return self.index(
                defaults=errors.value,
                errors=e,
                prefix_error=False)
        except Exception:
            log.exception('Exception in auth_settings')
            self.request.session.flash(
                _('Error occurred during update of auth settings.'),
                queue='error')

        redirect_to = self.request.resource_path(
            self.context, route_name='auth_home')
        return HTTPFound(redirect_to)