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
from Mozilla.CompareLocales import compareApp, compareDirs

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
    self.args = args.copy()
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
    locale, workdir, gather_stats = (self.args[k]
      for k in ('locale', 'workdir', 'gather_stats'))
    log.msg('Starting to compare %s in %s' % (locale, workdir))
    self.sendStatus({'header': 'Comparing %s against en-US for %s\n' \
                     % (locale, workdir)})
    workingdir = os.path.join(self.builder.basedir, workdir)
    try:
      o, summary, stats = self._compare(workingdir, locale, gather_stats, args)
    except Exception, e:
      log.msg('%s comparison failed with %s' % (locale, str(e)))
      log.msg(Failure().getTraceback())
      self.rc = EXCEPTION
      return
    self.rc = SUCCESS
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
        if gather_stats:
            self.sendStatus({'stats': stats})
        self.sendStatus({'stdout': codecs.utf_8_encode(o.serialize())[0],
                         'result': dict(summary=dict(summary),
                                        details=o.details.toJSON())})
    except Exception, e:
      log.msg('%s status sending failed with %s' % (locale, str(e)))
    pass

  def _compare(self, workingdir, locale, gather_stats, args):
    inipath, l10nbase = (self.args[k]
      for k in ('inipath', 'l10nbase'))
    app = EnumerateSourceTreeApp(os.path.join(workingdir, inipath),
                                 workingdir,
                                 os.path.join(workingdir, l10nbase),
                                 [locale])
    obs = None
    stats = None
    if gather_stats:
      obs = Observer()
    o = compareApp(app, otherObserver=obs)
    summary = o.summary[locale]
    if gather_stats:
      stats = obs.dict()
    return o, summary, stats

  def finished(self, *args):
    # sometimes self.rc isn't set here, no idea why
    try:
      rc = self.rc
    except AttributeError:
      rc = FAILURE
    self.sendStatus({'rc': rc})

class InspectDirsCommand(InspectCommand):
  """Subclass InspectCommand to only compare two directories.

  This is used by the InspectLocaleDirs command, as part of the 
  dashboard for weave.

  Requires `refpath` and `l10npath` to be in args, both are relative
  to `workingdir`.
  """
  def _compare(self, workingdir, locale, gather_stats, args):
    """Overload _compare to call compareDirs."""
    ref, l10n = (self.args[k] for k in ('refpath', 'l10npath'))
    obs = stats = None
    if gather_stats:
      obs = Observer()
    log.msg(workingdir, ref, l10n)
    o = compareDirs(os.path.join(workingdir, ref),
                    os.path.join(workingdir, l10n),
                    otherObserver = obs)
    try:
        summary = o.summary.values()[0]
    except:
        log.msg("Couldn't get summary")
        summary = {}
    if gather_stats:
      stats = obs.dict()
    return o, summary, stats


registerSlaveCommand('moz_inspectlocales', InspectCommand, '0.2')
registerSlaveCommand('moz_inspectlocales_dirs', InspectDirsCommand, '0.2')
