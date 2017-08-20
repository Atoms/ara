#  Copyright (c) 2017 Red Hat, Inc.
#
#  This file is part of ARA: Ansible Run Analysis.
#
#  ARA is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  ARA is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with ARA.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import (absolute_import, division, print_function)

import decorator
import flask
import logging
import os
import six

from ansible import __version__ as ansible_version
from ansible.plugins.callback import CallbackBase
from ara.db import models
from ara.db.models import db
from ara.webapp import create_app
from datetime import datetime
from distutils.version import LooseVersion
from oslo_serialization import jsonutils

# To retrieve Ansible CLI options
try:
    from __main__ import cli
except ImportError:
    cli = None

LOG = logging.getLogger('ara.callback')
app = create_app()


class CommitAfter(type):
    def __new__(kls, name, bases, attrs):
        def commit_after(func):
            def _commit_after(func, *args, **kwargs):
                rval = func(*args, **kwargs)
                db.session.commit()
                return rval

            return decorator.decorate(func, _commit_after)

        for k, v in attrs.items():
            if callable(v) and not k.startswith('_'):
                attrs[k] = commit_after(v)
        return super(CommitAfter, kls).__new__(kls, name, bases, attrs)


class IncludeResult(object):
    """
    This is used by the v2_playbook_on_include callback to synthesize a task
    result for calling log_task.
    """
    def __init__(self, host, path):
        self._host = host
        self._result = {'included_file': path}


@six.add_metaclass(CommitAfter)
class CallbackModule(CallbackBase):
    """
    Saves data from an Ansible run into a database
    """
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'ara'

    def __init__(self):
        super(CallbackModule, self).__init__()

        if not flask.current_app:
            ctx = app.app_context()
            ctx.push()

        self.result = None
        self.task = None
        self.play = None
        self.playbook = None
        self.stats = None
        self.loop_items = []

        if cli:
            self._options = cli.options
        else:
            self._options = None

    def get_or_create_host(self, hostname):
        try:
            host = (models.Host.query
                    .filter_by(name=hostname)
                    .filter_by(playbook_id=self.playbook.id)
                    .one())
        except models.NoResultFound:
            host = models.Host(name=hostname, playbook=self.playbook)
            db.session.add(host)

        return host

    def get_or_create_file(self, path):
        try:
            if self.playbook.id:
                file_ = (models.File.query
                         .filter_by(path=path)
                         .filter_by(playbook_id=self.playbook.id)
                         .one())
                return file_
        except models.NoResultFound:
            pass

        file_ = models.File(path=path, playbook=self.playbook)
        db.session.add(file_)

        try:
            with open(path, 'r') as fd:
                data = fd.read()
            sha1 = models.content_sha1(data)
            try:
                content = models.FileContent.query.filter_by(sha1=sha1).one()
            except models.NoResultFound:
                content = models.FileContent(content=data)

            file_.content = content
        except IOError:
            LOG.warn('failed to open %s for reading', path)

        return file_

    def log_task(self, result, status, **kwargs):
        """
        'log_task' is called when an individual task instance on a single
        host completes. It is responsible for logging a single
        'Result' record to the database.
        """
        LOG.debug('logging task result for task %s (%s), host %s',
                  self.task.name, self.task.id, result._host.get_name())

        result.task_start = self.task.started
        result.task_end = datetime.utcnow()
        host = self.get_or_create_host(result._host.get_name())

        # Use Ansible's CallbackBase._dump_results in order to strip internal
        # keys, respect no_log directive, etc.
        if self.loop_items:
            # NOTE (dmsimard): There is a known issue in which Ansible can send
            # callback hooks out of order and "exit" the task before all items
            # have returned, this can cause one of the items to be missing
            # from the task result in ARA.
            # https://github.com/ansible/ansible/issues/24207
            results = [self._dump_results(result._result)]
            for item in self.loop_items:
                results.append(self._dump_results(item._result))
        else:
            results = self._dump_results(result._result)

        self.result = models.Result(
            playbook=self.playbook,
            play=self.play,
            task=self.task,
            host=host,
            started=result.task_start,
            ended=result.task_end,
            result=jsonutils.loads(results),
            status=status,
            changed=result._result.get('changed', False),
            failed=result._result.get('failed', False),
            skipped=result._result.get('skipped', False),
            unreachable=result._result.get('unreachable', False),
            ignore_errors=kwargs.get('ignore_errors', False),
        )

        db.session.add(self.result)

        if self.task.action == 'setup' and 'ansible_facts' in result._result:
            host.facts = result._result['ansible_facts']

    def log_stats(self, stats):
        """
        Logs playbook statistics to the database.
        """
        LOG.debug('logging stats')
        hosts = sorted(stats.processed.keys())
        for hostname in hosts:
            host = self.get_or_create_host(hostname)
            host_stats = stats.summarize(hostname)
            host.changed = host_stats['changed']
            host.unreachable = host_stats['unreachable']
            host.failed = host_stats['failures']
            host.ok = host_stats['ok']
            host.skipped = host_stats['skipped']

    def close_task(self):
        """
        Marks the completion time of the currently active task.
        """
        if self.task is not None:
            LOG.debug('closing task %s (%s)',
                      self.task.name,
                      self.task.id)
            self.task.stop()
            db.session.add(self.task)

            self.task = None
            self.loop_items = []

    def close_play(self):
        """
        Marks the completion time of the currently active play.
        """
        if self.play is not None:
            LOG.debug('closing play %s (%s)', self.play.name, self.play.id)
            self.play.stop()
            db.session.add(self.play)

            self.play = None

    def close_playbook(self):
        """
        Marks the completion time of the currently active playbook.
        """
        if self.playbook is not None:
            LOG.debug('closing playbook %s', self.playbook.path)
            self.playbook.stop()
            self.playbook.completed = True
            db.session.add(self.playbook)

    def v2_runner_item_on_ok(self, result):
        self.loop_items.append(result)

    def v2_runner_item_on_failed(self, result):
        self.loop_items.append(result)

    def v2_runner_item_on_skipped(self, result):
        self.loop_items.append(result)

    def v2_runner_retry(self, result):
        self.loop_items.append(result)

    def v2_runner_on_ok(self, result, **kwargs):
        self.log_task(result, 'ok', **kwargs)

    def v2_runner_on_unreachable(self, result, **kwargs):
        self.log_task(result, 'unreachable', **kwargs)

    def v2_runner_on_failed(self, result, **kwargs):
        self.log_task(result, 'failed', **kwargs)

    def v2_runner_on_skipped(self, result, **kwargs):
        self.log_task(result, 'skipped', **kwargs)

    def v2_playbook_on_task_start(self, task, is_conditional,
                                  handler=False):
        self.close_task()

        LOG.debug('starting task %s (action %s)',
                  task.name, task.action)
        pathspec = task.get_path()
        if pathspec:
            path, lineno = pathspec.split(':', 1)
            lineno = int(lineno)
            file_ = self.get_or_create_file(path)
        else:
            path = self.playbook.path
            lineno = 1
            file_ = self.get_or_create_file(self.playbook.path)

        self.task = models.Task(
            name=task.get_name(),
            action=task.action,
            play=self.play,
            playbook=self.playbook,
            tags=task._attributes['tags'],
            file=file_,
            lineno=lineno,
            handler=handler)
        self.task.start()
        db.session.add(self.task)

    def v2_playbook_on_handler_task_start(self, task):
        self.v2_playbook_on_task_start(task, False, handler=True)

    def v2_playbook_on_start(self, playbook):
        path = os.path.abspath(playbook._file_name)
        if self._options is not None:
            parameters = self._options.__dict__.copy()
        else:
            parameters = {}

        # Potentially sanitize some user-specified keys
        for parameter in app.config['ARA_IGNORE_PARAMETERS']:
            if parameter in parameters:
                msg = "Parameter not saved by ARA due to configuration"
                parameters[parameter] = msg

        LOG.debug('starting playbook %s', path)
        self.playbook = models.Playbook(
            ansible_version=ansible_version,
            path=path,
            parameters=parameters
        )

        self.playbook.start()
        db.session.add(self.playbook)

        file_ = self.get_or_create_file(path)
        file_.is_playbook = True

        # We need to persist the playbook id so it can be used by the modules
        data = {
            'playbook': {
                'id': self.playbook.id
            }
        }
        tmpfile = os.path.join(app.config['ARA_TMP_DIR'], 'ara.json')
        with open(tmpfile, 'w') as file:
            file.write(jsonutils.dumps(data))

    def v2_playbook_on_play_start(self, play):
        self.close_task()
        self.close_play()

        LOG.debug('starting play %s', play.name)
        if self.play is not None:
            self.play.stop()

        self.play = models.Play(
            name=play.name,
            playbook=self.playbook
        )

        self.play.start()
        db.session.add(self.play)

    def v2_playbook_on_stats(self, stats):
        self.log_stats(stats)

        self.close_task()
        self.close_play()
        self.close_playbook()

        LOG.debug('closing database')
        db.session.close()

    def v2_playbook_on_include(self, included_file):
        # Before Ansible 2.2.0.0, "include" tasks were not sent to the
        # callbacks as "native" tasks.
        if LooseVersion(ansible_version) < LooseVersion('2.2.0'):
            for host in included_file._hosts:
                LOG.debug('log include file for host %s', host)
                self.log_task(IncludeResult(host, included_file._filename),
                              'ok')
