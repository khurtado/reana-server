# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REST API client generator."""
from functools import partial

from reana_commons.api_client import get_current_api_client
from werkzeug.local import LocalProxy

current_rwc_api_client = LocalProxy(
    partial(get_current_api_client, component='reana-workflow-controller'))
