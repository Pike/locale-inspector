from twisted.python import log
from buildbot.scheduler import BaseUpstreamScheduler
from buildbot.sourcestamp import SourceStamp
from buildbot import buildset
from buildbot.process import properties
from buildbot.util import ComparableMixin
from twisted.internet import defer, reactor
from twisted.web.client import getPage

from collections import defaultdict
from datetime import datetime
import os.path
from ConfigParser import ConfigParser, NoSectionError, NoOptionError

import logger

#from bb2mbdb.utils import timeHelper
def timeHelper(t):
    if t is None:
        return t
    return datetime.utcfromtimestamp(t)


class Tree(ComparableMixin):
    """Carry data per tree."""

    compare_attrs = ['name', 'repo', 'branches', 'l10ninis', 'all_locales',
                     'locales', 'branch2dirs', ]

    def __init__(self, name, repo, branch, l10nbranch, l10nini):
        self.name = name
        self.repo = repo
        self.branches = {'en': branch, 'l10n': l10nbranch}
        self.l10ninis = {branch: [l10nini]}
        self.all_locales = None
        self.locales = []
        self.branch2dirs = {}
        self.tld = None

    def addData(self, branch, l10nini, dirs, tld=None):
        log.msg(l10nini + ", " + str(tld))
        try:
            self.branch2dirs[branch] += dirs
        except KeyError:
            self.branch2dirs[branch] = dirs[:]
        if tld is not None:
            self.tld = tld

        if l10nini:
            if branch in self.l10ninis:
                if l10nini not in self.l10ninis[branch]:
                    self.l10ninis[branch].append(l10nini)
            else:
                self.l10ninis[branch] = [l10nini]


class AppScheduler(BaseUpstreamScheduler):
    """Scheduler used for app compare-locales builds.
    """

    compare_attrs = ('name', 'builderNames', 'treebuilder', 'inipath', 'trees')

    class BranchData:
        '''Helper class that caches the data of all trees per hg branch.
        '''
        def __init__(self):
            self.inis = defaultdict(list)
            self.dirs = defaultdict(list)
            self.topleveltrees = set()
            self.all_locales = defaultdict(set)
        def addDirs(self, tree, dirs):
            for d in dirs:
                self.dirs[d].append(tree)

    class L10nDirs(defaultdict):
        def __init__(self):
            defaultdict.__init__(self, set)
        def addDirs(self, tree, dirs):
            for d in dirs:
                self[d].add(tree) 

    def __init__(self, name, builderNames, inipath, treebuildername):
        """
        @param name: the name of this Scheduler
        @param builderNames: a list of Builder names. When this Scheduler
                             decides to start a set of builds, they will be
                             run on the Builders named by this list.
        @param inipath: path to l10nbuilds.ini, describing the apps
        @param treebuildername: the name of the builder that collects
                                tree info from remote l10n.ini files
        """

        BaseUpstreamScheduler.__init__(self, name)
        self.builderNames = builderNames
        # Path to the l10nbuilds.ini file that is read synchronously
        # Can be None for testing
        if inipath is not None:
            assert os.path.exists(inipath)
        self.inipath = inipath
        self.treebuilder = treebuildername
        self.trees = {}
        # just volatile data below
        # cache tree data per hg repo branch
        self.branches = defaultdict(self.BranchData)
        self.l10nbranches = defaultdict(self.L10nDirs)
        # map tree/locale tuples to list of changes
        self.pendings = defaultdict(list)
        self.dSubmitBuildsets = None
        # deferred that's non-None if a tree builds are currently running
        self.waitOnTree = None
        self.pendingChanges = []
        self.treesToDo = set() # trees that changed on a tree build

    def listBuilderNames(self):
        return self.builderNames + [self.treebuilder]

    def getPendingBuildTimes(self):
        return []

    def addTree(self, tree, changes=None):
        '''Callback that is passed to the TreeLoader step'''
        if tree.name in self.trees:
            if self.trees[tree.name] == tree:
                # we allready got that tree, all good
                logger.debug('scheduler.l10n',
                             'Tree info for %s loaded, unchanged' % tree.name)
                return
            # updated tree. Add this to treesToDo, which will be picked up
            # by checkEnUS, called after the buildset is done
            self.treesToDo.add(tree.name)
        self.trees[tree.name] = tree
        logger.debug("scheduler.l10n", "updated tree " + tree.name)
        try:
            # update caches of tree data
            self.branches.clear()
            self.l10nbranches.clear()
            for _n, _t in self.trees.iteritems():
                for _b, dirs in _t.branch2dirs.iteritems():
                    self.branches[_b].addDirs(_n, dirs)
                    self.l10nbranches[_t.branches['l10n']].addDirs(_n, dirs)
                for _b, inis in _t.l10ninis.iteritems():
                    for ini in inis:
                        self.branches[_b].inis[ini].append(_n)
                if _t.tld is not None:
                    self.l10nbranches[_t.branches['l10n']].addDirs(_n, [_t.tld])
                    self.branches[_t.branches['en']].topleveltrees.add(_n)
                if _t.all_locales is not None:
                    self.branches[_t.branches['en']].all_locales[_t.all_locales].add(_n)
        except Exception, e:
            log.msg(str(e))
        logger.debug("scheduler.l10n", "branch data cache updated")

    def startService(self):
        BaseUpstreamScheduler.startService(self)
        log.msg("starting l10n scheduler")
        if self.inipath is None:
            # testing, don't trigger tree builds
            return
        # trigger tree builds for our trees, clear() first
        cp = ConfigParser()
        cp.read(self.inipath)
        self.trees.clear()
        _ds = []
        for tree in cp.sections():
            # create a BuildSet, submit it to the BuildMaster
            props = properties.Properties()
            props.update({
                    'tree': tree,
                    'l10nbuilds': self.inipath,
                    },
                         "Scheduler")
            bs = buildset.BuildSet([self.treebuilder],
                                   SourceStamp(),
                                   properties=props)
            self.submitBuildSet(bs)
            _ds.append(bs.waitUntilFinished())
        d = defer.DeferredList(_ds)
        d.addCallback(self.onTreesBuilt)
        self.waitOnTree = d

    def onTreesBuilt(self, res, branchdata=None, change=None):
        '''Callback used when all tree-builder buildsets are done.
        If change is None, this is called from startService, otherwise
        it's called as a follow up from a change-based build. If so,
        call into checkEnUS.
        After that, process all pending changes, as long as we're not
        doing more tree builds again.
        '''
        # res is either None or list of tuple build sets
        logger.debug('scheduler.l10n',
                     'pending trees got built' + (change is not None and ", change given" or ""))
        # trees for the last change are built, wait no longer
        self.waitOnTree = None
        if change is not None and branchdata is not None:
            self.checkEnUS(res, branchdata, change)
        while self.waitOnTree is None and self.pendingChanges:
            c = self.pendingChanges.pop(0)
            self.addChange(c)

    def addChange(self, change):
        '''Main entry point for the scheduler, this is called by the 
        buildmaster.
        '''
        if self.waitOnTree is not None:
            # a tree build is currently running, wait with this
            # until we're done with it
            self.pendingChanges.append(change)
            return
        # fixup change.locale if property is given
        if not hasattr(change, 'locale') or not change.locale:
            if 'locale' in change.properties:
                change.locale = change.properties['locale']
        if not hasattr(change, 'locale') or not change.locale:
            # check branch, l10n.inis
            # if l10n.inis are found, callback to all-locales, locales/en-US
            # otherwise just check those straight away
            if change.branch not in self.branches:
                log.msg('not our branches')
                return
            tree_triggers = set()
            branchdata = self.branches[change.branch]
            for f in change.files:
                if f in branchdata.inis:
                    tree_triggers.update(branchdata.inis[f])
            if tree_triggers:
                # trigger tree builds, wait for them to finish
                # and check the change for en-US builds
                _ds = []
                for _n in tree_triggers:
                    _t = self.trees[_n]
                    props = properties.Properties()
                    props.update({
                            'tree': _n,
                            'l10nbuilds': self.inipath,
                            },
                                 "Scheduler")
                    bs = buildset.BuildSet([self.treebuilder],
                                           SourceStamp(branch=change.branch,
                                                       changes=[change]),
                                           properties=props)
                    self.submitBuildSet(bs)
                    _ds.append(bs.waitUntilFinished())
                d = defer.DeferredList(_ds)
                d.addCallback(self.onTreesBuilt,
                              branchdata = branchdata, change = change)
                self.waitOnTree = d
                return
            self.checkEnUS(None, branchdata, change)
            return
        # check l10n changesets
        if change.branch not in self.l10nbranches:
            return
        l10ndirs = self.l10nbranches[change.branch]
        trees = set()
        for f in change.files:
            for mod, _trees in l10ndirs.iteritems():
                if f.startswith(mod):
                    trees |= _trees
        for _n in trees:
            if change.locale in self.trees[_n].locales:
                self.compareBuild(_n, change.locale, [change])
        return

    def checkEnUS(self, result, branchdata, change):
        """Factored part of change handling that's either called
        from onChange, or from onTreesBuilt.
        """
        # ignore result, either None or list of build sets
        logger.debug('scheduler.l10n',
                     'checking en-US for change %d' % change.number)
        all_locales = set()
        # pick up trees from onTreesBuilt
        en_US = set(self.treesToDo)
        self.treesToDo.clear()
        for f in change.files:
            if f in branchdata.all_locales:
                all_locales.update(branchdata.all_locales[f])
            if 'locales/en-US' in f:
                mod = f.split('locales/en-US', 1)[0]
                if mod:
                    mod = mod.rstrip('/') # common case for non-single
                if not mod:
                    # single-module-hg, aka mobile
                    for _n in branchdata.topleveltrees:
                        for l in self.trees[_n].locales:
                            self.compareBuild(_n, l, [change])
                else:
                    if mod in branchdata.dirs:
                        en_US.update(branchdata.dirs[mod])
        # load all-locales files
        rev = 'default'
        for _n in all_locales:
            if change.revision is not None:
                rev = change.revision
            _t = self.trees[_n]
            url = _t.repo + '/' + _t.branches['en'] + '/raw-file/' + rev
            url += '/' + _t.all_locales
            d = getPage(url)
            d.addCallback(self.onAllLocales, _n, change)
        # trigger all locales for all trees
        for _n in en_US:
            _t = self.trees[_n]
            for l in _t.locales:
                self.compareBuild(_n, l, [change])

    def onAllLocales(self, page, tree, change = None):
        newlocs = filter(None, page.splitlines())
        added = set(newlocs) - set(self.trees[tree].locales)
        logger.debug('scheduler.l10n.all-locales',
                     "had %s; got %s; new are %s" % 
                     (', '.join(self.trees[tree].locales),
                      ', '.join(list(newlocs)),
                      ', '.join(list(added))))
        self.trees[tree].locales = newlocs
        for loc in added:
            self.compareBuild(tree, loc, [change])

    def compareBuild(self, tree, locale, changes):
        cs = self.pendings[(tree, locale)]
        if changes is not None:
            cs += changes
        if self.dSubmitBuildsets is None:
            self.dSubmitBuildsets = reactor.callLater(0, self.submitBuildsets)

    def submitBuildsets(self):
        for tpl, changes in self.pendings.iteritems():
            tree, locale = tpl
            _t = self.trees[tree]
            props = properties.Properties()
            # figure out the latest change
            try:
                when = timeHelper(max(filter(None, (c.when for c in changes))))
                from life.models import Push
            except (ValueError, ImportError):
                when = None
            for k, v in _t.branches.iteritems():
                props.setProperty(k+"_branch", v, "Scheduler")
                _r = "default"
                if when is not None:
                    if k == 'l10n':
                        repo = '%s/%s' % (v, locale)
                    else:
                        repo = v
                    q = Push.objects.filter(repository__name=repo,
                                            push_date__lte=when,
                                            changesets__branch__name='default')
                    try:
                        # get the latest changeset on the 'default' branch
                        #  not strictly .tip, for pushes with heads on
                        #  multiple branches (bug 602182)
                        _c = q.order_by('-pk')[0].changesets.order_by('-pk')
                        _r = str(_c.filter(branch__name='default')[0].shortrev)
                    except IndexError:
                        # no pushes, update to empty repo 000000000000
                        _r = "default"
                props.setProperty(k+"_revision", _r, "Scheduler")
            props.update({"tree": tree,
                          "locale": locale,
                          "l10n.ini": _t.l10ninis[_t.branches['en']][0],
                          "revisions": sorted(_t.branches.keys()),
                          },
                         "Scheduler")
            bs = buildset.BuildSet(self.builderNames,
                                   SourceStamp(changes=changes),
                                   properties=props)
            self.submitBuildSet(bs)
        self.dSubmitBuildsets = None
        self.pendings.clear()
        

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
        if not hasattr(change, 'locale') or not change.locale:
            if 'locale' in change.properties:
                change.locale = change.properties['locale']
            else:
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
