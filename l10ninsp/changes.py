from calendar import timegm
import os

from twisted.python import log, failure
from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall

from buildbot.changes import base, changes

def createChangeSource(settings, pollInterval=30):
    os.environ['DJANGO_SETTINGS_MODULE'] = settings
    from pushes.models import Push, Branch
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
        
        def poll(self):
            if self.latest is None:
                self.latest = Push.objects.order_by('-pk')[0].id
                return
            new_pushes = Push.objects.filter(pk__gt=self.latest).order_by('pk')
            if self.debug:
                log.msg('mbdb changesource found %d pushes' % new_pushes.count())
            push = None
            for push in new_pushes:
                self.submitChangesForPush(push)
            if push is not None:
                self.latest = push.id

        def submitChangesForPush(self, push):
            repo = push.repository
            if repo.forest is not None:
                branch = repo.forest.name.encode('utf-8')
                locale = repo.name[len(branch) + 1:].encode('utf-8')
            else:
                branch = repo.name.encode('utf-8')
            for cs in push.changesets.filter(branch=self.branch).order_by('pk'):
                c = changes.Change(who=push.user.encode('utf-8'),
                                    files=map(lambda u: u.encode('utf-8'),
                                    cs.files.values_list('path', flat=True)),
                                    revision=cs.revision.encode('utf-8'),
                                    comments=cs.description.encode('utf-8'),
                                    when=timegm(push.push_date.utctimetuple()),
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
            def next(_cb):
                try:
                    p = i.next()
                except StopIteration:
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
