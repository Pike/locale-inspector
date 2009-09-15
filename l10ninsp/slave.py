from twisted.internet import reactor, defer
from twisted.python import log
from twisted.python.failure import Failure

from buildbot.slave.registry import registerSlaveCommand
from buildbot.slave.commands import Command
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, EXCEPTION

import codecs
from collections import defaultdict
import os
from Mozilla.Paths import EnumerateSourceTreeApp
from Mozilla.CompareLocales import compareApp

class intdict(defaultdict):
    def __init__(self):
        defaultdict.__init__(self, int)

class Observer(object):
    cats = ['missing', 'missingInFiles', 'unchanged']
    def __init__(self):
        self.uc = defaultdict(intdict)
        pass
    def notify(self, category, _file, data):
        if category not in self.cats:
            return True
        self.uc[_file.module][_file.file] += data
        return True
    def dict(self):
        return dict((k, dict(v)) for k, v in self.uc.iteritems())

class InspectCommand(Command):
  """
  Do CompareLocales on the slave.

  To be able to run this, you have to

    import Mozilla.slave

  when starting python on the slave via PYHTONSTARTUP
  """
  
  debug = True
  
  def setup(self, args):
    self.locale = args['locale']
    self.inipath = args['inipath']
    self.l10nbase = args['l10nbase']
    self.workdir = args['workdir']
    self.basedir = args['basedir']
    self.gather_stats = args['gather_stats']
    ## more

  def start(self):
    if self.debug:
      log.msg('Compare started')

    d = defer.Deferred()
    d.addCallback(self.doCompare)
    reactor.callLater(0, d.callback, None)
    d.addBoth(self.finished)
    return d

  def doCompare(self, *args):
    log.msg('Starting to compare %s in %s' % (self.locale, self.workdir))
    self.sendStatus({'header': 'Comparing %s against en-US for %s\n' \
                     % (self.locale, self.workdir)})
    workingdir = os.path.join(self.builder.basedir, self.workdir)
    if self.debug:
      log.msg('trying to import Mozilla from %s'%os.getcwd())
    try:
      app = EnumerateSourceTreeApp(os.path.join(workingdir, self.inipath),
                                   workingdir,
                                   os.path.join(workingdir, self.l10nbase),
                                   [self.locale])
      obs = None
      if self.gather_stats:
          obs = Observer()
      o = compareApp(app, otherObserver=obs)
    except Exception, e:
      log.msg('%s comparison failed with %s' % (self.locale, str(e)))
      log.msg(Failure().getTraceback())
      self.rc = EXCEPTION
      return
    self.rc = SUCCESS
    summary = o.summary[self.locale]
    if 'obsolete' in summary and summary['obsolete'] > 0:
      self.rc = WARNINGS
    if 'missing' in summary and summary['missing'] > 0:
      self.rc = FAILURE
    if 'missingInFiles' in summary and summary['missingInFiles'] > 0:
      self.rc = FAILURE
    if 'errors' in summary and summary['errors'] > 0:
      self.rc = FAILURE
    total = sum(summary[k] for k in ['changed','unchanged','missing',
                                     'missingInFiles'])
    summary['completion'] = int((summary['changed'] * 100) / total)
    summary['total'] = total

    try:
        if self.gather_stats:
            self.sendStatus({'stats': obs.dict()})
        self.sendStatus({'stdout': codecs.utf_8_encode(o.serialize())[0],
                         'result': dict(summary=dict(summary),
                                        details=o.details.toJSON())})
    except Exception, e:
      log.msg('%s status sending failed with %s' % (self.locale, str(e)))
    pass

  def finished(self, *args):
    # sometimes self.rc isn't set here, no idea why
    try:
      rc = self.rc
    except AttributeError:
      rc = FAILURE
    self.sendStatus({'rc': rc})

registerSlaveCommand('moz_inspectlocale', InspectCommand, '0.2')
