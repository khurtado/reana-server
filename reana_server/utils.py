# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA-Server utils."""

import base64
import csv
import io
import json
import secrets
from uuid import UUID

import os
import fs
import requests
import yaml
from flask import current_app as app
from flask import url_for
from reana_commons.k8s.secrets import REANAUserSecretsStore
from reana_db.database import Session
from reana_db.models import User
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from werkzeug.wsgi import LimitedStream

from reana_server.config import ADMIN_USER_ID, REANA_GITLAB_URL


def is_uuid_v4(uuid_or_name):
    """Check if given string is a valid UUIDv4."""
    # Based on https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = UUID(uuid_or_name, version=4)
    except Exception:
        return False

    return uuid.hex == uuid_or_name.replace('-', '')


def create_user_workspace(user_workspace_path):
    """Create user workspace directory."""
    reana_fs = fs.open_fs(app.config['SHARED_VOLUME_PATH'])
    if not reana_fs.exists(user_workspace_path):
        reana_fs.makedirs(user_workspace_path)
    if os.environ.get("VC3USERID", None):
        vc3_uid = int(os.environ.get("VC3USERID"))
        owner_gid = os.stat(reana_fs.getsyspath(user_workspace_path)).st_gid
        os.chown(reana_fs.getsyspath(user_workspace_path), vc3_uid, owner_gid)


def get_user_from_token(access_token):
    """Validate that the token provided is valid."""
    user = Session.query(User).filter_by(access_token=access_token).\
        one_or_none()
    if not user:
        raise ValueError('Token not valid.')
    return user


def _get_users(_id, email, user_access_token, admin_access_token):
    """Return all users matching search criteria."""
    admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    search_criteria = dict()
    if _id:
        search_criteria['id_'] = _id
    if email:
        search_criteria['email'] = email
    if user_access_token:
        search_criteria['access_token'] = user_access_token
    users = Session.query(User).filter_by(**search_criteria).all()
    return users


def _create_user(email, user_access_token, admin_access_token):
    """Create user with provided credentials."""
    try:
        admin = Session.query(User).filter_by(id_=ADMIN_USER_ID).one_or_none()
        if admin_access_token != admin.access_token:
            raise ValueError('Admin access token invalid.')
        if not user_access_token:
            user_access_token = secrets.token_urlsafe(16)
        user_parameters = dict(access_token=user_access_token)
        user_parameters['email'] = email
        user = User(**user_parameters)
        Session.add(user)
        Session.commit()
    except (InvalidRequestError, IntegrityError) as e:
        Session.rollback()
        raise ValueError('Could not create user, '
                         'possible constraint violation')
    return user


def _export_users(admin_access_token):
    """Export all users in database as csv.

    :param admin_access_token: Admin access token.
    :type admin_access_token: str
    """
    admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    csv_file_obj = io.StringIO()
    csv_writer = csv.writer(csv_file_obj, dialect='unix')
    for user in User.query.all():
        csv_writer.writerow([user.id_, user.email, user.access_token])
    return csv_file_obj


def _import_users(admin_access_token, users_csv_file):
    """Import list of users to database.

    :param admin_access_token: Admin access token.
    :type admin_access_token: str
    :param users_csv_file: CSV file object containing a list of users.
    :type users_csv_file: _io.TextIOWrapper
    """
    admin = User.query.filter_by(id_=ADMIN_USER_ID).one_or_none()
    if admin_access_token != admin.access_token:
        raise ValueError('Admin access token invalid.')
    csv_reader = csv.reader(users_csv_file)
    for row in csv_reader:
        user = User(id_=row[0], email=row[1], access_token=row[2])
        Session.add(user)
    Session.commit()
    Session.remove()


def _create_and_associate_reana_user(sender, token=None,
                                     response=None, account_info=None):
    try:
        user_email = account_info['user']['email']
        user_fullname = account_info['user']['profile']['full_name']
        username = account_info['user']['profile']['username']
        search_criteria = dict()
        search_criteria['email'] = user_email
        users = Session.query(User).filter_by(**search_criteria).all()
        if users:
            user = users[0]
        else:
            user_access_token = secrets.token_urlsafe(16)
            user_parameters = dict(access_token=user_access_token)
            user_parameters['email'] = user_email
            user_parameters['full_name'] = user_fullname
            user_parameters['username'] = username
            user = User(**user_parameters)
            Session.add(user)
            Session.commit()
    except (InvalidRequestError, IntegrityError):
        Session.rollback()
        raise ValueError('Could not create user, '
                         'possible constraint violation')
    except Exception:
        raise ValueError('Could not create user')
    return user


def _get_user_from_invenio_user(id):
    user = Session.query(User).filter_by(email=id).one_or_none()
    if not user:
        raise ValueError('No users registered with this id')
    return user


def _get_reana_yaml_from_gitlab(webhook_data, user_id):
    gitlab_api = REANA_GITLAB_URL + "/api/v4/projects/{0}" + \
                 "/repository/files/{1}/raw?ref={2}&access_token={3}"
    reana_yaml = 'reana.yaml'
    if webhook_data['object_kind'] == 'push':
        branch = webhook_data['project']['default_branch']
        commit_sha = webhook_data['checkout_sha']
    elif webhook_data['object_kind'] == 'merge_request':
        branch = webhook_data['object_attributes']['source_branch']
        commit_sha = webhook_data['object_attributes']['last_commit']['id']
    secrets_store = REANAUserSecretsStore(str(user_id))
    gitlab_token = secrets_store.get_secret_value('gitlab_access_token')
    project_id = webhook_data['project']['id']
    yaml_file = requests.get(gitlab_api.format(project_id, reana_yaml,
                                               branch, gitlab_token))
    return yaml.load(yaml_file.content), \
        webhook_data['project']['path_with_namespace'], \
        webhook_data['project']['name'], branch, \
        commit_sha


def _format_gitlab_secrets(gitlab_response):
    access_token = json.loads(gitlab_response)['access_token']
    user = json.loads(
                requests.get(REANA_GITLAB_URL + '/api/v4/user?access_token={0}'
                             .format(access_token)).content)
    return {
        "gitlab_access_token": {
            "value": base64.b64encode(
                        access_token.encode('utf-8')).decode('utf-8'),
            "type": "env"
        },
        "gitlab_user": {
            "value": base64.b64encode(
                        user['username'].encode('utf-8')).decode('utf-8'),
            "type": "env"
        }
    }


def _get_gitlab_hook_id(response, project_id, gitlab_token):
    """Return REANA hook id from a GitLab project if it is connected.

    By checking its webhooks and comparing them to REANA ones.

    :param response: Flask response.
    :param project_id: Project id on GitLab.
    :param gitlab_token: GitLab token.
    """
    reana_hook_id = None
    gitlab_hooks_url = (
        REANA_GITLAB_URL +
        "/api/v4/projects/{0}/hooks?access_token={1}"
        .format(project_id, gitlab_token)
    )
    response_json = requests.get(gitlab_hooks_url).json()
    create_workflow_url = url_for('workflows.create_workflow',
                                  _external=True)
    if response.status_code == 200 and response_json:
        reana_hook_id = next(hook['id'] for hook in response_json
                             if hook['url'] and
                             hook['url'] == create_workflow_url)
    return reana_hook_id


class RequestStreamWithLen(object):
    """Wrap ``request.stream`` object to have ``__len__`` attribute.

    Users can uplaod files to REANA through REANA-Server (RS). RS passes then
    the content of the file uploads to the next REANA component,
    REANA-Workflow-Controller (RWC).

    In order for this operation to be efficient we read the user stream upload
    using ``werkzeug`` streams through ``request.stream``. Then, to pass this
    stream to RWC without creating memory leaks we stream upload the
    ``request.stream`` content using the Requests library. However, the
    Request library is not aware of how the size of the stream is represented
    in ``werkzeug`` (``limit`` attribute), Requests only understands
    ``len(stream)`` or ``stream.len``, see more here
    https://github.com/psf/requests/blob/3e7d0a873f838e0001f7ac69b1987147128a7b5f/requests/utils.py#L108-L166

    This class provides the necessary attributes for compatibility with
    Requests stream upload.
    """

    def __init__(self, limitedstream):
        """Wrap the stream to have ``len``."""
        if not hasattr(limitedstream, 'limit'):
            raise TypeError('The provided stream doesn\'t have the limit'
                            'attribute needed to calculate it\'s length.')
        self.limitedstream = limitedstream

    def read(self, *args, **kwargs):
        """Expose ``request.stream``s read method."""
        return self.limitedstream.read(*args, **kwargs)

    def __len__(self):
        """Expose the length of the ``request.stream``."""
        return self.limitedstream.limit
