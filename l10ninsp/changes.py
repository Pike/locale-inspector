from calendar import timegm
import os

from twisted.python import log, failure
from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall

from buildbot.changes import base, changes

def createChangeSource(settings, pollInterval=30):
    os.environ['DJANGO_SETTINGS_MODULE'] = settings
    from pushes.models import Push
    class MBDBChangeSource(base.ChangeSource):
        debug = True
        def __init__(self,  pollInterval=30):
            #base.ChangeSource.__init__(self)
            self.pollInterval = pollInterval
            self.latest = None
        
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
                repo = push.repository
                if repo.forest is not None:
                    branch = repo.forest.name.encode('utf-8')
                    locale = repo.name[len(branch)+1:].encode('utf-8')
                else:
                    branch = repo.name.encode('utf-8')
                for cs in push.changesets.order_by('pk'):
                    c = changes.Change(who = push.user.encode('utf-8'),
                                       files = map(lambda u: u.encode('utf-8'),
                                                   cs.files.values_list('path', flat=True)),
                                       revision = cs.revision.encode('utf-8'),
                                       comments = cs.description.encode('utf-8'),
                                       when = timegm(push.push_date.utctimetuple()),
                                       branch = branch)
                    if repo.forest is not None:
                        # locale change
                        c.locale = locale
                    self.parent.addChange(c)
            if push is not None:
                self.latest = push.id
                    
        def describe(self):
            return "Getting changes from: %s" % self._make_url()

        def __str__(self):
            return "<HgPoller for %s%s>" % (self.hgURL, self.branch)

    c = MBDBChangeSource(pollInterval)
    return c
