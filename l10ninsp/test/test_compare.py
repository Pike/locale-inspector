

# test step.ShellCommand and the slave-side commands.ShellCommand

import sys, time, os
from twisted.trial import unittest
from twisted.internet import reactor, defer
from twisted.python import util, log
from l10ninsp.slave import InspectCommand
from buildbot import interfaces
from buildbot.test.runutils import SlaveCommandTestBase, RunMixin
from shutil import copytree
import pdb

from django.conf import settings, UserSettingsHolder, global_settings

settings._target = UserSettingsHolder(global_settings)
settings.DATABASE_ENGINE = 'sqlite3'
settings.INSTALLED_APPS = (
  'life',
  'mbdb',
  'bb2mbdb',
  'l10nstats',
)
settings.BUILDMASTER_BASE = 'basedir'

from l10nstats.models import Run, Tree, Locale, ModuleCount
from django.test.utils import connection

def createStage(basedir, *files):
    '''Create a staging environment in the given basedir

    Each argument is a tuple of
    - a tuple with path segments
    - the content of the file to create
    '''
    for pathsteps, content in files:
        try:
            os.makedirs(os.path.join(basedir, *pathsteps[:-1]))
        except OSError, e:
            if e.errno != 17:
                raise e
        f = open(os.path.join(basedir, *pathsteps), 'w')
        f.write(content)
        f.close()


class SlaveSide(SlaveCommandTestBase, unittest.TestCase):
    #old_name = settings.DATABASE_NAME
    basedir = "test_compare.testSuccess"
    stageFiles = ((('mozilla', 'app', 'locales', 'l10n.ini'),
                     '''[general]
depth = ../..
all = app/locales/all-locales

[compare]
dirs = app
'''),
    (('mozilla', 'app', 'locales', 'all-locales'),
     '''good
obsolete
missing
'''),
                  (('mozilla','app','locales','en-US','dir','file.dtd'),
                   '<!ENTITY test "value">\n<!ENTITY test2 "value2">\n<!ENTITY test3 "value3">\n'),
                  (('l10n','good','app','dir','file.dtd'),
                   '''
<!ENTITY test "local value">
<!ENTITY test2 "local value2">
<!ENTITY test3 "local value3">
'''),
                  (('l10n','obsolete','app','dir','file.dtd'),
                   '''
<!ENTITY test "local value">
<!ENTITY test2 "local value 2">
<!ENTITY test3 "local value 3">
<!ENTITY test4 "local value 4">
'''),
                  (('l10n','missing','app','dir','file.dtd'),
                   '<!ENTITY test "local value">\n<!ENTITY test3 "value3">\n'))
    def setUp(self):
        self.setUpBuilder(self.basedir)
        createStage(self.basedir, *self.stageFiles)
        #self._db = connection.creation.create_test_db()

    def tearDown(self):
        #connection.creation.destroy_test_db(self.old_name)
        pass

    def args(self, app, locale, gather_stats=False, initial_module=None):
        return {'workdir': '.',
                'basedir': 'mozilla',
                'inipath': 'mozilla/%s/locales/l10n.ini' % app,
                'l10nbase': 'l10n',
                'locale': locale,
                'tree': app,
                'gather_stats': gather_stats,
                'initial_module': initial_module,
                }

    def testGood(self):
        args = self.args('app', 'good')
        d = self.startCommand(InspectCommand, args)
        d.addCallback(self._check,
                      0,
                      dict(),
                      dict(completion=100))
        return d

    def testGoodStats(self):
        args = self.args('app', 'good', gather_stats=True)
        d = self.startCommand(InspectCommand, args)
        d.addCallback(self._check,
                      0,
                      dict(),
                      dict(completion=100))
        return d

    def testObsolete(self):
        args = self.args('app', 'obsolete')
        d = self.startCommand(InspectCommand, args)
        d.addCallback(self._check,
                      1,
                      None,
                      dict(completion=100))
        return d

    def testMissing(self):
        args = self.args('app', 'missing')
        d = self.startCommand(InspectCommand, args)
        d.addCallback(self._check,
                      2,
                      None,
                      dict(completion=33))
        return d

    def _check(self, res, expectedRC, expectedDetails, exSummary, exStats={}):
        self.assertEqual(self.findRC(), expectedRC)
        res = self._getResults()
        details = res['details']
        summary = res['summary']
        stats = res['stats']
        if expectedDetails is not None:
            self.assertEquals(details, dict())
        for k, v in exSummary.iteritems():
            self.assertEquals(summary[k], v)
        self.assertEquals(stats, exStats)
        return

    def _getResults(self):
        rv = {'stats':{}}
        for d in self.builder.updates:
            if 'result' in d:
                rv.update(d['result'])
            if 'stats' in d:
                rv['stats'] = d['stats']
        return rv


config = """
from buildbot.process import factory
from l10ninsp.steps import InspectLocale
from buildbot.buildslave import BuildSlave

f = factory.BuildFactory()
f.addStep(InspectLocale, master='l10n-master', workdir='.', basedir='mozilla',
                         inipath='mozilla/app/locales/l10n.ini',
                         l10nbase='l10n', locale='missing', tree='app',
                         gather_stats=True)
BuildmasterConfig = c = {}
c['properties'] = {
  'revisions': [],
  'l10n_branch': 'test'
  }
c['slaves'] = [BuildSlave('bot1', 'sekrit')]
c['schedulers'] = []
c['builders'] = []
c['builders'].append({'name': 'test_builder', 'slavename': 'bot1',
                      'builddir': '.', 'factory': f})
c['slavePortnum'] = 0

from bb2mbdb.status import setupBridge
setupBridge('test-master', None, c)
"""

class MasterSide(RunMixin, unittest.TestCase):
    old_name = settings.DATABASE_NAME

    def setUp(self):
        self._db = connection.creation.create_test_db()
        return RunMixin.setUp(self)

    def tearDown(self):
        connection.creation.destroy_test_db(self.old_name)
        return RunMixin.tearDown(self)

    def testBuild(self):
        m = self.master
        s = m.getStatus()
        m.loadConfig(config)
        m.readConfig = True
        m.startService()
        d = self.connectSlave(builders=["test_builder"])
        d.addCallback(self._doBuild)
        return d

    def _doBuild(self, res):
        createStage('slavebase-bot1', *SlaveSide.stageFiles)
        c = interfaces.IControl(self.master)
        d = self.requestBuild("test_builder")
        d2 = self.master.botmaster.waitUntilBuilderIdle("test_builder")
        dl = defer.DeferredList([d, d2])
        dl.addCallback(self._doneBuilding)
        return dl

    def _doneBuilding(self, res):
        self.assertEquals(Run.objects.count(), 1, "one run expected")
        r = Run.objects.all()[0]
        self.assertEquals(r.missing, 1)
        self.assertEquals(r.changed, 1)
        self.assertEquals(ModuleCount.objects.count(), 1,
                          "one module expected")
        mc = ModuleCount.objects.all()[0]
        self.assertEquals(mc.name, 'app')
        self.assertEquals(mc.count, 2)
        pass
