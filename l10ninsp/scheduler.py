from twisted.python import log
from buildbot.scheduler import BaseUpstreamScheduler
from buildbot.sourcestamp import SourceStamp
from buildbot import buildset
from buildbot.process import properties
from twisted.web.client import getPage

class DirScheduler(BaseUpstreamScheduler):
    """
    Scheduler used for l10n weave builds.
    """
  
    compare_attrs = ('name', 'builderNames', 'branch', 'tree'
                     'properties')
  
    def __init__(self, name, tree, branch, builderNames, repourl):
        BaseUpstreamScheduler.__init__(self, name)
        self.tree = tree
        self.branch = branch
        self.builderNames = builderNames
        self.repourl = repourl


    # Internal helper
    def queueBuild(self, locale, change):
        props = properties.Properties()
        props.update({'locale': locale,
                      'tree': self.tree,
                      'branch': self.branch,
                      'repourl': self.repourl,
                      'refpath': self.branch + '/en-US',
                      'en_revision': 'default',
                      'en_branch': self.branch + '/en-US',
                      'l10npath': self.branch + '/' + locale,
                      'l10n_revision': 'default',
                      'l10n_branch': self.branch,
                      },
                     'Scheduler')
        ss = SourceStamp(changes=[change])
        bs = buildset.BuildSet(self.builderNames, ss,
                               reason = "%s %s" % (self.tree, locale),
                               properties = props)
        self.submitBuildSet(bs)


    def onRepoIndex(self, indexText, change):
        """Callback used when loading the index of the repository list
        to get the list of locales to trigger.
        """
        locales = map(lambda s: s.rsplit('/',2)[1], indexText.strip().split())
        for loc in locales:
            if loc == "en-US":
                continue
            self.queueBuild(loc, change)


    # Implement IScheduler
    def addChange(self, change):
        log.msg("scheduler",
                  "addChange: Change %d, %s" % (change.number, change.asText()))
        if self.branch != change.branch:
            log.msg("not our branch, ignore, %s != %s" %
                    (self.branch, change.branch))
            return
        # take the 'loc' property as locale
        if hasattr(change, properties) and 'loc' in change.properties:
            change.locale = change.properties['loc']
        if not change.locale:
            return
        if change.locale == 'en-US':
            # trigger all builds, load repo index
            d = getPage(self.repourl + self.branch + '?style=raw')
            d.addCallback(self.onRepoIndex, change)
            #d.addErrback(self.failedRepo)
            return
        self.queueBuild(change.locale, change)


    def listBuilderNames(self):
        return self.builderNames


    def getPendingBuildTimes(self):
        return []
