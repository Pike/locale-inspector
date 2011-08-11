from calendar import timegm
import os

from twisted.python import log, failure
from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall

from buildbot.changes import base, changes

def createChangeSource(settings, pollInterval=3*60):
    os.environ['DJANGO_SETTINGS_MODULE'] = settings
    from pushes.models import Push, Branch
    from django.db import transaction
    class MBDBChangeSource(base.ChangeSource):
        debug = True
        def __init__(self,  pollInterval=30, branch='default'):
            #base.ChangeSource.__init__(self)
            self.pollInterval = pollInterval
            self.latest = None
            self.branch, created = \
                Branch.objects.get_or_create(name=branch)
        
        def startService(self):
            self.loop = LoopingCall(self.poll)
            base.ChangeSource.startService(self)
            reactor.callLater(0, self.loop.start, self.pollInterval)
        
        def stopService(self):
            self.loop.stop()
            return base.ChangeSource.stopService(self)
        
        @transaction.commit_on_success
        def poll(self):
            '''Check for new pushes.

            Hack around transactions on innodb, make this transaction
            aware and transaction.commit() to get a new transaction
            for our queries.
            '''
            transaction.commit()
            if self.latest is None:
                try:
                    self.latest = Push.objects.order_by('-pk')[0].id
                except IndexError:
                    self.latest = 0
                return
            new_pushes = Push.objects.filter(pk__gt=self.latest).order_by('pk')
            if self.debug:
                log.msg('mbdb changesource found %d pushes after %d' % (new_pushes.count(), self.latest))
            push = None
            for push in new_pushes:
                self.submitChangesForPush(push)
            if push is not None:
                self.latest = push.id

        def submitChangesForPush(self, push):
            if self.debug:
                log.msg('submitChangesForPush called')
            repo = push.repository
            if repo.forest is not None:
                branch = repo.forest.name.encode('utf-8')
                locale = repo.name[len(branch) + 1:].encode('utf-8')
            else:
                branch = repo.name.encode('utf-8')
            for cs in push.changesets.filter(branch=self.branch).order_by('pk'):
                when = timegm(push.push_date.utctimetuple()) +\
                    push.push_date.microsecond/1000.0/1000
                c = changes.Change(who=push.user.encode('utf-8'),
                                    files=map(lambda u: u.encode('utf-8'),
                                    cs.files.values_list('path', flat=True)),
                                    revision=cs.revision.encode('utf-8'),
                                    comments=cs.description.encode('utf-8'),
                                    when=when,
                                    branch=branch)
                if repo.forest is not None:
                    # locale change
                    c.locale = locale
                self.parent.addChange(c)

        def replay(self, builder, startPush=None, startTime=None, endTime=None):
            bm = self.parent.parent.botmaster
            qd = {}
            if startTime is not None:
                qd['push_date__gte'] = startTime
            if endTime is not None:
                qd['push_date__lte'] = endTime
            if startPush is not None:
                qd['id__gte'] = startPush
            q = Push.objects.filter(**qd).order_by('push_date')
            i = q.iterator()
            if self.debug:
                log.msg('replay called for %d pushes' % q.count())
            def next(_cb):
                try:
                    p = i.next()
                except StopIteration:
                    log.msg("done iterating")
                    return
                self.submitChangesForPush(p)
                def stumble():
                    bm.waitUntilBuilderIdle(builder).addCallback(_cb, _cb)
                reactor.callLater(.5, stumble)
            def cb(res, _cb):
                reactor.callLater(.5, next, _cb)
            next(cb)

        def describe(self):
            return str(self)

        def __str__(self):
            return "MBDBChangeSource"

    c = MBDBChangeSource(pollInterval)
    return c
