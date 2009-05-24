from buildbot.process import factory
from buildbot.steps.shell import ShellCommand, SetProperty
from buildbot.process.properties import WithProperties

from twisted.python import log, failure

import l10ninsp.steps
reload(l10ninsp.steps)
from steps import InspectLocale, GetRevisions


class Factory(factory.BuildFactory):
    useProgress = False
    
    def __init__(self, basedir, mastername, steps=None):
        factory.BuildFactory.__init__(self, steps)
        self.base = basedir
        self.mastername = mastername
        # hack, should be factored
        from ConfigParser import ConfigParser
        cp = ConfigParser()
        cp.read('l10nbuilds.ini')
        self.inipaths = {}
        for s in cp.sections():
            self.inipaths[s] = cp.get(s, 'l10n.ini')

    def newBuild(self, requests):
        steps = self.createSteps(requests[-1])
        b = self.buildClass(requests)
        b.useProgress = self.useProgress
        b.setStepFactories(steps)
        return b
    
    def createSteps(self, request):
        revs = request.properties.getProperty('revisions')
        if revs is None:
            revs = ['en', 'l10n']
            log.msg('no revisions given in ' + str(request.properties))
        else:
            revs = revs[:]
        revs.remove('l10n')
        tree = request.properties.getProperty('tree')
        preSteps = ((GetRevisions, {}),)
        sourceSteps = tuple(
            (ShellCommand, {'command': 
                            ['hg', 'update', '-r', 
                             WithProperties('%%(%s_revision)s' % mod)],
                            'workdir': WithProperties(self.base + 
                                                      '/%%(%s_branch)s' % mod),
                            'haltOnFailure': True})
            for mod in revs)
        idSteps = tuple(
            (SetProperty, {'command': 
                           ['hg', 'ident', '-i'], 
                            'workdir': WithProperties(self.base + 
                                                      '/%%(%s_branch)s' % mod),
                           'haltOnFailure': True,
                           'property': '%s_revision' % mod})
            for mod in revs)
        l10nSteps = (
            (ShellCommand, {'command': 
                            ['hg', 'update', '-r', 
                             WithProperties('%(l10n_revision)s')],
                            'workdir': WithProperties(self.base + 
                                                      '/%(l10n_branch)s/%(locale)s'),
                            'haltOnFailure': True}),
            (SetProperty, {'command': 
                           ['hg', 'ident', '-i'], 
                            'workdir': WithProperties(self.base + 
                                                      '/%(l10n_branch)s/%(locale)s'),
                           'haltOnFailure': True,
                           'property': 'l10n_revision'}),
            )
        inspectSteps = (
            (InspectLocale, {
                    'master': self.mastername,
                    'workdir': self.base,
                    'basedir': WithProperties('%(en_branch)s'),
                    'inipath': WithProperties('%(en_branch)s/' +
                                              self.inipaths[tree]),
                    'l10nbase': WithProperties('%(l10n_branch)s'),
                    'locale': WithProperties('%(locale)s'),
                    'tree': tree,
                    'gather_stats': True,
                    }),)
        return preSteps + sourceSteps + idSteps + l10nSteps + inspectSteps
