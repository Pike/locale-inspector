import warnings

from twisted.python import log
from twisted.internet import reactor
from buildbot.process.buildstep import BuildStep, LoggingBuildStep, LoggedRemoteCommand
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, SKIPPED, \
    Results
from buildbot.steps.shell import WithProperties

from pprint import pformat
from collections import defaultdict
import simplejson

from bb2mbdb.utils import timeHelper

class ResultRemoteCommand(LoggedRemoteCommand):
    """
    Helper command class, extracts compare locale results from updates.
    """

    def __init__(self, name, args):
        LoggedRemoteCommand.__init__(self, name, args)
        self.dbrun = None

    def ensureDBRun(self):
        if self.dbrun is not None:
            return
        from l10nstats.models import Run, Build
        from life.models import Tree, Forest, Locale
        loc, isnew = Locale.objects.get_or_create(code=self.args['locale'])
        forest, isnew = Forest.objects.get_or_create(name=self.step.build.getProperty('l10n_branch'))
        if isnew:
            log.msg(("WARNING: Forest %s created in status, not expected " +
                     "outside of tests") % forest.name)
        tree, isnew = Tree.objects.get_or_create(code=self.args['tree'],
                                                 l10n=forest)
        buildername = self.step.build.getProperty('buildername')
        buildnumber = self.step.build.getProperty('buildnumber')
        try:
            build = Build.objects.get(builder__master__name = self.step.master,
                                      builder__name = buildername,
                                      buildnumber = buildnumber)
        except Build.DoesNotExist:
            build = None
        self.dbrun = Run.objects.create(locale = loc,
                                        tree = tree,
                                        build = build)
        self.dbrun.activate()
        from pushes.models import Changeset, Push
        revs = self.step.build.getProperty('revisions')
        srctime = None
        for rev in revs:
            branch = self.step.build.getProperty('%s_branch' % rev)
            if rev == 'l10n':
                # l10n repo, append locale to branch
                branch += '/' + loc.code
            ident = self.step.build.getProperty('%s_revision' % rev)
            cs = None
            try:
                cs = Changeset.objects.get(revision__startswith=ident[:12])
                self.dbrun.revisions.add(cs)
            except Changeset.DoesNotExist:
                log.msg("no changeset found for %s=%s" % (rev, ident))
                pass
            if cs is not None and cs.id is not 1:
                try:
                    _st = Push.objects.filter(repository__name=branch,
                                              changesets=cs).order_by('push_date')[0].push_date
                    if srctime is None:
                        srctime = _st
                    else:
                        srctime = max(srctime, _st)
                except (Push.DoesNotExist, IndexError):
                    log.msg("no srctime found for %s=%s" % (rev, ident))
                    pass
        if srctime is not None:
            self.dbrun.srctime = srctime
            self.dbrun.save()

    def remoteUpdate(self, update):
        log.msg("remoteUpdate called with keys: " + ", ".join(update.keys()))
        result = None
        try:
            self.rc = update.pop('rc')
            log.msg('Comparison of localizations completed')
        except KeyError:
            pass
        try:
            # get the Observer data from the slave
            result = update.pop('result')
        except KeyError:
            pass
        try:
            # get the Observer data from the slave
            stats = update.pop('stats')
            log.msg('untranslated count: %d' %
                    sum(map(lambda d: sum(d.values()), stats.values())))
            self.addStats(stats)
        except KeyError:
            pass
        if len(update):
            # there's more than just us
            LoggedRemoteCommand.remoteUpdate(self, update)
            pass

        if not result:
            return

        rmsg = {}
        summary = result['summary']
        self.completion = summary['completion']
        changed = summary['changed']
        unchanged = summary['unchanged']
        tbmsg = ''
        if 'tree' in self.args:
            tbmsg = self.args['tree'] + ': '
            tbmsg += "%(tree)s %(locale)s" % self.args
        if self.rc == FAILURE:
            missing = sum([summary[k] \
                           for k in ['missing', 'missingInFiles'] \
                           if k in summary])
        self.logs['stdio'].addEntry(5, simplejson.dumps(result, indent=2))
        self.addSummary(summary)

    def addStats(self, stats):
        self.ensureDBRun()
        id = self.dbrun.id
        def to_rows():
            for m, d in stats.iteritems():
                for f, c in d.iteritems():
                    yield (m, f, c, id)
        rows = list(to_rows())
        modulestats = defaultdict(int)
        for m, f, c, id in rows:
            modulestats[m] += c
        from l10nstats.models import UnchangedInFile, ModuleCount
        from django.db import connection
        cur = connection.cursor()
        cur.executemany("INSERT INTO %s (module, file, count, run_id) VALUES (%%s, %%s, %%s,  %%s)"
                         % UnchangedInFile._meta.db_table, rows)
        log.msg("should have inserted %d rows into %s" %
                (len(rows), UnchangedInFile._meta.db_table))
        mcs = []
        for m, c in modulestats.iteritems():
            mc, created = ModuleCount.objects.get_or_create(name=m, count=c)
            mcs.append(mc)
        self.dbrun.unchangedmodules.add(*mcs)
        self.dbrun.save()
        pass

    def addSummary(self, summary):
        self.ensureDBRun()
        for k in ('missing', 'missingInFiles', 'obsolete', 'total',
                  'changed', 'unchanged', 'keys', 'completion', 'errors'):
            setattr(self.dbrun, k, summary.get(k, 0))
        self.dbrun.save()

    def remoteComplete(self, maybeFailure):
        log.msg('end with compare, rc: %s, maybeFailure: %s' %
                (self.rc, maybeFailure))
        LoggedRemoteCommand.remoteComplete(self, maybeFailure)
        return maybeFailure

class InspectLocale(LoggingBuildStep):
    """
    This class hooks up CompareLocales in the build master.
    """

    name = "moz_inspectlocales"
    cmd_name = name
    warnOnFailure = 1

    description = ["comparing"]
    descriptionDone = ["compare", "locales"]

    def __init__(self, master, workdir, basedir, inipath, l10nbase, locale, tree,
                 gather_stats = False, initial_module=None, **kwargs):
        """
        @type  master: string
        @param master: name of the master

        @type  workdir: string
        @param workdir: local directory (relative to the Builder's root)
                        where the mozilla and the l10n trees reside

        @type basedir: string
        @param basdir: path to all local repository clones, relative to the workdir

        @type inipath: string
        @param inipath: path to the l10n.ini file, relative to the workdir

        @type l10nbase: string
        @param l10nbase: path to the localization dirs, relative to the workdir

        @type  locale: string
        @param locale: Language code of the localization to be compared.

        @type  tree: string
        @param tree: The tree identifier for this branch/product combo.

        @type gather_stats: bool
        @param gather_stats: whether or not to gather stats about untranslated strings.
        """

        LoggingBuildStep.__init__(self, **kwargs)

        self.args = {'workdir'    : workdir,
                     'basedir'    : basedir,
                     'inipath'    : inipath,
                     'l10nbase'   : l10nbase,
                     'locale'     : locale,
                     'tree'       : tree,
                     'gather_stats'     : gather_stats,
                     'initial_module'   : initial_module}
        self.master = master

    def describe(self, done=False):
        if done:
            return self.descriptionDone
        return self.description

    def start(self):
        log.msg('starting with compare')
        args = {}
        args.update(self.args)
        for k, v in args.iteritems():
            if isinstance(v, WithProperties):
                args[k] = self.build.getProperties().render(v)
        try:
            args['tree'] = self.build.getProperty('tree')
        except KeyError:
            pass
        self.descriptionDone = [args['locale'], args['tree']]
        cmd = ResultRemoteCommand(self.cmd_name, args)
        self.startCommand(cmd, [])
  
    def evaluateCommand(self, cmd):
        """Decide whether the command was SUCCESS, WARNINGS, or FAILURE.
        Override this to, say, declare WARNINGS if there is any stderr
        activity, or to say that rc!=0 is not actually an error."""

        return cmd.rc

    def getText(self, cmd, results):
        assert cmd.rc == results, "This should really be our own result"
        log.msg("called getText")
        text = ["no completion found for result %s" % results]
        if hasattr(cmd, 'completion'):
            log.msg("rate is %d, results is %s" % (cmd.completion,results))
            text = ['%d%% translated' % cmd.completion]
        if False and cmd.missing > 0:
            text += ['missing: %d' % cmd.missing]
        return LoggingBuildStep.getText(self,cmd,results) + text


class InspectLocaleDirs(InspectLocale):
    """Subclass InspectLocale to only compare two directories.

    This is used for the dashboard for weave.
    """
    name = "moz_inspectlocales_dirs"
    cmd_name = name
    def __init__(self, master, workdir, basedir, refpath, l10npath, locale,
                 tree, gather_stats = False, **kwargs):
        """
        @type  master: string
        @param master: name of the master

        @type  workdir: string
        @param workdir: local directory (relative to the Builder's root)
                        where the mozilla and the l10n trees reside

        @type basedir: string
        @param basdir: path to all local repository clones, relative to the workdir

        @type refpath: string
        @param refpath: path to the reference dir, relative to the workdir

        @type l10npath: string
        @param l10npath: path to the reference dir, relative to the workdir

        @type  locale: string
        @param locale: Language code of the localization to be compared.

        @type  tree: string
        @param tree: The tree identifier for this branch.

        @type gather_stats: bool
        @param gather_stats: whether or not to gather stats about untranslated strings.
        """

        LoggingBuildStep.__init__(self, **kwargs)

        self.args = {'workdir'    : workdir,
                     'basedir'    : basedir,
                     'refpath'    : refpath,
                     'l10npath'   : l10npath,
                     'locale'     : locale,
                     'tree'       : tree,
                     'gather_stats'     : gather_stats,
                     }
        self.master = master


class GetRevisions(BuildStep):
    name = "moz_get_revs"
    warnOnFailure = 1

    description = ["get", "revisions"]
    descriptionDone = ["got", "revisions"]
    hg_branch = 'default'

    def start(self):
        log.msg("setting build props for revisions")
        self.step_status.setText(self.description)
        changes = self.build.allChanges()
        if not changes:
            return SKIPPED
        from life.models import Push
        when = timeHelper(max(map(lambda c: c.when, changes)))
        loog = self.addLog("stdio")
        loog.addStdout("Timestamps for %s:\n\n" % when)
        revs = self.build.getProperty('revisions')[:]
        for rev in revs:
            branch = self.build.getProperty('%s_branch' % rev)
            if rev == 'l10n':
                # l10n repo, append locale to branch
                branch += '/' + self.build.getProperty('locale')
            try:
                q = Push.objects.filter(repository__name=branch,
                                        push_date__lte=when,
                                        changesets__branch__name=self.hg_branch)
                to_set = str(q.order_by('-pk')[0].tip.shortrev)
            except IndexError:
                # no pushes, update to empty repo 000000000000
                to_set = "000000000000"
            self.build.setProperty('%s_revision' % rev, to_set, 'Build')
            loog.addStdout("%s: %s\n" % (branch, to_set))
        reactor.callLater(0, self.finished, SUCCESS)

    def finished(self, results):
        self.step_status.setText(self.descriptionDone)
        BuildStep.finished(self, results)
