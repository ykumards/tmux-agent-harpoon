"""Integration tests use an isolated tmux socket and fake agents; no API calls."""
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HarpoonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harpoon-tests-')
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "project's $cash (demo)"
        self.project.mkdir()
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        for agent in ('codex', 'pi', 'claude'):
            script = self.bin / agent
            script.write_text(f'#!/bin/bash\nprintf "FAKE_{agent}_READY\\n"\nexec -a {agent} /bin/sleep 1000\n')
            script.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), XDG_CACHE_HOME=str(self.home / 'cache'),
                        PATH=str(self.bin) + ':' + os.environ['PATH'], TERM='xterm-256color')
        for key in ('TMUX', 'TMUX_PANE', 'AH_CLIENT', 'AH_SESSION', 'AH_ORIGIN'):
            self.env.pop(key, None)
        (self.home / '.bash_profile').write_text('export PATH="' + str(self.bin) + ':$PATH"\n')
        self.socket = str(self.home / 'tmux.sock')
        self.tmux('new-session', '-d', '-x', '140', '-y', '40', '-s', 'origin', '-c', str(self.project), '/bin/bash', '--noprofile', '--norc')
        self.tmux('set-option', '-g', 'default-shell', '/bin/bash')
        self.tmux('set-option', '-g', 'prefix', 'C-a')
        self.origin = self.tmux('display-message', '-p', '#{pane_id}').strip()
        self.session = self.tmux('display-message', '-p', '#{session_id}').strip()
        self.env.update(TMUX=f'{self.socket},0,0', AH_ORIGIN=self.origin, AH_SESSION=self.session)
        self.screen = b''
        self.client = None
        self.master = None

    def tearDown(self):
        self.tmux('kill-server', check=False)
        if self.client:
            try:
                self.client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.client.kill()
                self.client.wait()
        if self.master is not None:
            os.close(self.master)
        time.sleep(.1)
        self.temp.cleanup()

    def tmux(self, *args, check=True):
        p = subprocess.run(['tmux', '-f', '/dev/null', '-S', self.socket, *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and p.returncode:
            raise AssertionError(f'tmux {args}: {p.stderr}')
        return p.stdout

    def run_script(self, name, *args, check=True):
        p = subprocess.run([str(ROOT / 'scripts' / name), *args], env=self.env,
                           capture_output=True, text=True, timeout=15)
        if check and p.returncode:
            raise AssertionError(p.stderr + p.stdout)
        return p

    def eventually(self, fn, timeout=6):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            last = fn()
            if last:
                return last
            time.sleep(.05)
        self.fail(f'Condition not met; last result: {last}; screen: {self.screen[-4500:]!r}')

    def windows(self):
        return self.tmux('list-windows', '-a', '-F', '#{window_id}').splitlines()

    def create(self, agent='codex'):
        return self.run_script('agent-workspace', '--create', agent).stdout.strip().split('\t')

    def attach(self):
        self.master, slave = pty.openpty()
        import fcntl, struct, termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
        self.client = subprocess.Popen(['tmux', '-S', self.socket, 'attach-session', '-t', 'origin'],
                                       env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.eventually(lambda: self.tmux('list-clients', '-F', '#{client_name}').strip())
        self.env['AH_CLIENT'] = self.tmux('list-clients', '-F', '#{client_name}').strip()
        self.drain()

    def drain(self, duration=.15):
        output = b''
        until = time.monotonic() + duration
        while time.monotonic() < until:
            ready, _, _ = select.select([self.master], [], [], .02)
            if ready:
                try:
                    output += os.read(self.master, 65536)
                except OSError:
                    break
        self.screen += output
        return output

    def keys(self, data):
        os.write(self.master, data)
        return self.drain(.25)

    def test_create_layout_directory_focus_and_exit_shell(self):
        pane, session = self.create()
        self.assertEqual(session, self.session)
        self.assertEqual(len(self.windows()), 2)
        info = self.tmux('list-panes', '-t', pane, '-F', '#{pane_id}\t#{pane_width}\t#{pane_current_path}\t#{pane_active}').splitlines()
        self.assertEqual(len(info), 2)
        left, right = [line.split('\t') for line in info]
        self.assertEqual(left[0], pane)
        self.assertEqual(left[2], str(self.project))
        self.assertEqual(right[2], str(self.project))
        self.assertEqual(left[3], '1')
        self.assertAlmostEqual(int(right[1]) / (int(left[1]) + int(right[1])), .3, delta=.015)
        self.assertEqual(self.tmux('display-message', '-p', '-t', pane, '#{window_zoomed_flag}').strip(), '0')
        self.eventually(lambda: 'FAKE_codex_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        self.tmux('send-keys', '-t', pane, 'C-c')
        self.eventually(lambda: 'shell ready' in self.tmux('capture-pane', '-p', '-t', pane))
        self.assertEqual(len(self.tmux('list-panes', '-t', pane).splitlines()), 2)

    def test_missing_agent_and_bad_width_leave_no_windows(self):
        self.tmux('set-option', '-g', '@agent-picker-launchers', 'definitely-not-installed')
        p = self.run_script('agent-workspace', '--create', 'definitely-not-installed', check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('not on PATH', p.stderr)
        self.assertEqual(len(self.windows()), 1)
        self.tmux('set-option', '-g', '@agent-picker-launchers', 'codex|pi')
        self.tmux('set-option', '-g', '@agent-picker-terminal-width', '100')
        self.assertNotEqual(self.run_script('agent-workspace', '--create', 'codex', check=False).returncode, 0)
        self.assertEqual(len(self.windows()), 1)

    def test_scanner_manual_agents_stable_ids_and_unknown_status(self):
        self.tmux('send-keys', '-t', self.origin, str(self.bin / 'pi'), 'Enter')
        rows = self.eventually(lambda: self.run_script('agent-picker', '--rows').stdout)
        fields = rows.split('\t')
        self.assertEqual(fields[0], '-1')
        self.assertEqual(fields[1], self.origin)
        self.assertTrue(fields[2].startswith(self.session + ':@'))
        self.assertIn('pi', fields[3])
        self.tmux('rename-session', '-t', self.session, 'renamed')
        self.assertIn(self.session + ':@', self.run_script('agent-picker', '--rows').stdout)

    def test_empty_popup_cancel_and_ctrl_n_launch(self):
        self.attach()
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1.0)
        # A real popup, even with no agents. Ctrl+n must work with no selection.
        self.keys(b'\x0e')
        self.drain(.5)
        self.keys(b'\x1b')
        self.assertEqual(len(self.windows()), 1)
        self.keys(b'\x0e')
        self.drain(.5)
        self.keys(b'pi')
        self.keys(b'\r')
        self.eventually(lambda: len(self.windows()) == 2)
        self.eventually(lambda: self.tmux('display-message', '-p', '#{window_name}').strip() == 'pi')
        self.assertEqual(self.tmux('display-message', '-p', '#{window_panes}').strip(), '2')
        self.assertEqual(self.tmux('display-message', '-p', '#{pane_active}').strip(), '1')

    def test_search_jump_cross_session_and_zoom(self):
        self.attach()
        self.tmux('new-session', '-d', '-s', 'other', '-c', str(self.project), '/bin/bash', '--noprofile', '--norc')
        otherpane = self.tmux('display-message', '-p', '-t', 'other', '#{pane_id}').strip()
        othersession = self.tmux('display-message', '-p', '-t', 'other', '#{session_id}').strip()
        self.env.update(AH_ORIGIN=otherpane, AH_SESSION=othersession)
        pane, _ = self.create('pi')
        self.eventually(lambda: 'pi' in self.run_script('agent-picker', '--rows').stdout)
        self.tmux('set-option', '-g', '@agent-picker-zoom', 'on')
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1.0)
        self.keys(b'pi')
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('display-message', '-p', '-c', self.env['AH_CLIENT'], '#{session_id}').strip() == othersession)
        self.assertEqual(self.tmux('display-message', '-p', '-t', pane, '#{window_zoomed_flag}').strip(), '1')


    def test_failed_start_leaves_both_panes_usable(self):
        self.tmux('set-option', '-g', '@agent-picker-command-codex', 'exit 7')
        pane, _ = self.create()
        self.eventually(lambda: 'exited: 7' in self.tmux('capture-pane', '-p', '-t', pane))
        self.tmux('send-keys', '-t', pane, 'printf SHELL_WORKS', 'Enter')
        self.eventually(lambda: 'SHELL_WORKS' in self.tmux('capture-pane', '-p', '-t', pane))
        self.assertEqual(len(self.tmux('list-panes', '-t', pane).splitlines()), 2)

    def test_refresh_discovers_new_agent_without_reopening(self):
        self.attach()
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1)
        pane, _ = self.create('pi')
        self.eventually(lambda: 'FAKE_pi_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        self.keys(b'\x12')
        self.drain(.5)
        self.keys(b'pi')
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('display-message', '-p', '#{pane_id}').strip() == pane)

    def test_pi_node_wrapper_and_prompt_argument_boundaries(self):
        import json
        harness = self.home / 'match.c'
        harness.write_text('#define main scanner_main\n#include ' +
                           json.dumps(str(ROOT / 'src/agent-scan.c')) + r'''
#undef main
#include <assert.h>
int main(void) {
    init_agents();
    assert(strcmp(match_agent("node\t--import\t/path with spaces/loader.mjs\t/work/pi/packages/coding-agent/src/cli.ts"), "pi") == 0);
    assert(strcmp(match_agent("node\t/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"), "pi") == 0);
    assert(match_agent("node\t/ordinary/app.js\t/work/pi/packages/coding-agent/src/cli.ts") == NULL);
    assert(match_agent("bash\t-c\techo @anthropic") == NULL);
    return 0;
}
''')
        binary = self.home / 'match'
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-o', str(binary), str(harness)], check=True)
        subprocess.run([str(binary)], check=True)

    def search_index(self):
        index = self.home / 'index.tsv'
        index.write_text(self.run_script('agent-picker', '--rows').stdout)
        self.env['AH_SCAN'] = str(self.home / 'cache/agent-harpoon/agent-scan')
        return str(index)

    def test_hidden_metadata_search_and_label_priority(self):
        pane, _ = self.create('codex')
        self.eventually(lambda: 'FAKE_codex_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        self.tmux('rename-window', '-t', pane, 'billing-service')
        self.tmux('select-pane', '-t', pane, '-T', 'Fix login redirect')
        index = self.search_index()
        row = Path(index).read_text().split('\t')
        self.assertTrue(row[3].startswith('Fix login redirect'))
        self.assertNotIn('billing-service', row[3])
        self.assertNotIn(str(self.home), row[3])
        for query in ('billing-service', str(self.home), 'login codex'):
            match = self.run_script('agent-search', 'filter', index, query).stdout
            self.assertIn('\t' + pane + '\t', match)
        self.tmux('set-option', '-p', '-t', pane, '@agent-harpoon-label', 'My investigation')
        self.tmux('select-pane', '-t', pane, '-T', 'Agent changed this title')
        self.assertTrue(self.run_script('agent-picker', '--rows').stdout.split('\t')[3].startswith('My investigation'))

    def test_relevance_ranking_and_empty_query_order(self):
        index = self.home / 'rank.tsv'
        rows = '-1\t%1\t$0:@0\tfirst\tawful undo testing handoff\tfirst\n' + \
               '-1\t%2\t$0:@1\tsecond\tauth\tsecond\n'
        index.write_text(rows)
        self.assertEqual(self.run_script('agent-search', 'filter', str(index), '').stdout, rows)
        ranked = self.run_script('agent-search', 'filter', str(index), 'auth').stdout
        self.assertEqual(ranked.splitlines()[0].split('\t')[1], '%2')
        sentinel = self.home / 'must-not-exist'
        self.run_script('agent-search', 'filter', str(index), '$(touch ' + str(sentinel) + ')')
        self.assertFalse(sentinel.exists())

    def test_rename_shortcut_and_clear_label(self):
        pane, _ = self.create('pi')
        self.eventually(lambda: 'FAKE_pi_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        self.attach()
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1)
        self.keys(b'\x14')
        self.drain(.4)
        self.keys(b'\x15Investigate auth')
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('show-option', '-pqv', '-t', pane, '@agent-harpoon-label').strip() == 'Investigate auth')
        self.drain(.4)
        self.keys(b'\x14')
        self.drain(.4)
        self.keys(b'\x15')
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('show-option', '-pqv', '-t', pane, '@agent-harpoon-label').strip() == '')
        self.keys(b'\x1b')

    def test_grep_snapshot_regex_preview_and_refresh(self):
        pane, _ = self.create('pi')
        self.eventually(lambda: 'FAKE_pi_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        index = self.search_index()
        corpus = str(self.home / 'grep: snapshots')
        self.run_script('agent-search', 'snapshot', index, corpus)
        match = self.run_script('agent-search', 'grep', corpus, 'FAKE_pi_R.*').stdout.splitlines()[0].split('\t')
        self.assertEqual(match[0], pane)
        context = self.run_script('agent-search', 'preview-grep', match[4], match[2]).stdout
        self.assertIn('FAKE_pi_READY', context)
        self.assertIn('›', context)
        self.assertEqual(self.run_script('agent-search', 'grep', corpus, '[').stdout, '')
        self.tmux('send-keys', '-t', pane, '-l', 'fresh_unique_sentence')
        self.eventually(lambda: 'fresh_unique_sentence' in self.tmux('capture-pane', '-p', '-t', pane))
        self.assertEqual(self.run_script('agent-search', 'grep', corpus, 'fresh_unique_sentence').stdout, '')
        refreshed = self.run_script('agent-search', 'refresh-grep', index, corpus, 'fresh_unique_sentence').stdout
        self.assertIn('fresh_unique_sentence', refreshed)

    def test_live_grep_shortcut_jumps_to_matching_agent(self):
        pane, _ = self.create('pi')
        self.eventually(lambda: 'FAKE_pi_READY' in self.tmux('capture-pane', '-p', '-t', pane))
        self.attach()
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1)
        self.keys(b'\x07')
        self.drain(.5)
        self.keys(b'FAKE_pi_READY')
        self.drain(.4)
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('display-message', '-p', '#{pane_id}').strip() == pane)

    def test_typing_selects_best_ranked_match(self):
        first, _ = self.create('codex')
        second, _ = self.create('pi')
        self.eventually(lambda: 'FAKE_pi_READY' in self.tmux('capture-pane', '-p', '-t', second))
        self.tmux('set-option', '-p', '-t', first, '@agent-harpoon-label', 'A useful toolkit here')
        self.tmux('set-option', '-p', '-t', second, '@agent-harpoon-label', 'auth')
        self.attach()
        subprocess.run([str(ROOT / 'agent-picker.tmux')], env=self.env, check=True)
        self.keys(b'\x01a')
        self.drain(1)
        self.keys(b'auth')
        self.drain(.4)
        self.keys(b'\r')
        self.eventually(lambda: self.tmux('display-message', '-p', '#{pane_id}').strip() == second)


if __name__ == '__main__':
    unittest.main(verbosity=2)
