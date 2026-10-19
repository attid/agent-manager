import json
from pathlib import Path
import tempfile
import unittest

from pi_runner.core import Config, RunnerError, sandbox_command, snapshot, changed_files


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'

    def config(self, **overrides):
        data = dict(agent_id='developer', project=str(self.repo), state_dir=str(self.state),
                    pi_command=['/usr/bin/true'], runtime_paths=[], model='test', provider='test')
        data.update(overrides)
        p = self.root / 'config.json'
        p.write_text(json.dumps(data))
        return Config.load(p)

    def test_state_cannot_be_exposed_through_project(self):
        with self.assertRaises(RunnerError):
            self.config(state_dir=str(self.repo / 'state'))

    def test_only_explicit_mounts_and_clean_environment(self):
        c = self.config()
        home = self.state / 'home'
        session = self.state / 'sessions' / 'one'
        argv = sandbox_command(c, home, session)
        self.assertIn('--clearenv', argv)
        self.assertIn('--unshare-pid', argv)
        self.assertIn('--die-with-parent', argv)
        self.assertNotIn('/', [argv[i+1] for i,x in enumerate(argv[:-1]) if x == '--ro-bind'])

    def test_git_metadata_is_read_only(self):
        (self.repo / '.git').mkdir()
        argv = sandbox_command(self.config(), self.state / 'home', self.state / 'session')
        i = argv.index(str(self.repo / '.git'))
        self.assertEqual(argv[i-1], '--ro-bind')
        self.assertEqual(argv[i+1], '/workspace/.git')

    def test_read_only_project_mode(self):
        c = self.config(project_read_only=True)
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        self.assertEqual(argv[argv.index(str(self.repo))-1], '--ro-bind')

    def test_snapshot_detects_edits_deletions_and_ignored_files(self):
        (self.repo / 'deleted').write_text('before')
        (self.repo / 'edited').write_text('before')
        before = snapshot(self.repo)
        (self.repo / 'deleted').unlink()
        (self.repo / 'edited').write_text('after')
        (self.repo / 'new').write_text('new')
        self.assertEqual(changed_files(before, snapshot(self.repo)), ['deleted', 'edited', 'new'])

    def test_readonly_dependency_cannot_expose_home(self):
        with self.assertRaises(RunnerError):
            self.config(readonly_paths=[str(Path.home())])

    def test_workdir_cannot_escape_via_symlink(self):
        (self.repo / 'outside').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RunnerError):
            self.config(workdir='outside')

    def test_linked_worktree_refuses_implicit_external_mount(self):
        (self.repo / '.git').write_text('gitdir: /private/project/.git/worktrees/work\n')
        with self.assertRaises(RunnerError):
            sandbox_command(self.config(), self.state / 'home', self.state / 'session')

    def test_allow_git_commit_mounts_git_readwrite(self):
        (self.repo / '.git').mkdir()
        c = self.config(allow_git_commit=True, skills=['codespaces'])
        self.assertEqual(c.skills, ['codespaces'])
        self.assertTrue(c.allow_git_commit)
        argv = sandbox_command(c, self.state / 'home', self.state / 'session')
        i = argv.index(str(self.repo / '.git'))
        self.assertEqual(argv[i-1], '--bind')
        self.assertEqual(argv[i+1], '/workspace/.git')


if __name__ == '__main__':
    unittest.main()
