import asyncio
import json
import lzma
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '13f_dashboard'))

import app
import desktop
from tools.unpack_csv import unpack_csv


WORKER = '''\
import json
import sys
from pathlib import Path

root = Path(json.load(sys.stdin)['destination'])
name = '공시 결과.csv'
folder = root / 'institutions'
folder.mkdir()
(folder / name).write_text('기관,보유액\\n한글,100\\n', encoding='utf-8-sig')
manifest = {'files': [{'name': name, 'path': f'institutions/{name}'}]}
print('@@DRAKOS@@' + json.dumps({'event': 'result', **manifest}, ensure_ascii=False))
'''


class ArchiveTest(unittest.TestCase):
    def test_unpack_preserves_utf8_csv_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '기존 자료.csv.xz'
            data = '기관,보유액\r\n이름,100\r\n'.encode('utf-8-sig')
            path.write_bytes(lzma.compress(data))
            result = unpack_csv(path)
            self.assertEqual(result.read_bytes(), data)
            with self.assertRaises(FileExistsError):
                unpack_csv(path)
            self.assertEqual(result.read_bytes(), data)


class StartupTest(unittest.TestCase):
    def test_launch_command_starts_working_folder_api(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
            reserved.close()
            with subprocess.Popen(
                [sys.executable, str(ROOT / '13f_dashboard' / 'run.py'), '--port', str(port)],
                cwd=ROOT, env={**os.environ, 'DRAKOS_STATE_DIR': folder},
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            ) as server:
                try:
                    for attempt in range(100):
                        try:
                            with urlopen(f'http://127.0.0.1:{port}/api/folders', timeout=1) as response:
                                data = json.load(response)
                            break
                        except OSError:
                            if server.poll() is not None:
                                self.fail(server.stderr.read().decode(errors='replace'))
                            if attempt == 99:
                                self.fail('the dashboard did not open its loopback port')
                            time.sleep(0.05)
                    self.assertEqual(Path(data['path']), ROOT)
                    if sys.platform == 'win32':
                        self.assertIn(Path(Path.home().anchor), [Path(drive) for drive in data['drives']])
                    else:
                        self.assertEqual(data['drives'], [])
                finally:
                    if server.poll() is None:
                        server.terminate()
                    server.wait(timeout=10)


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_launchers_keep_path_as_one_argument(self):
        path = Path('/tmp') / '공시 결과.csv'
        with patch('desktop.sys.platform', 'win32'), patch.object(desktop.os, 'startfile', create=True) as launch:
            await desktop.open_path(path)
            launch.assert_called_once_with(os.fspath(path))

        for platform, expected in [('darwin', 'open'), ('linux', 'xdg-open')]:
            with self.subTest(platform=platform), patch('desktop.sys.platform', platform), \
                    patch('desktop.asyncio.create_subprocess_exec', new_callable=AsyncMock) as launch:
                launch.return_value.wait = AsyncMock(return_value=0)
                await desktop.open_path(path)
                self.assertEqual(launch.await_args.args, (expected, os.fspath(path)))

    async def test_native_instance_lock_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'server.lock'
            with desktop.instance_lock(path):
                with self.assertRaisesRegex(RuntimeError, '동일한 상태 폴더'):
                    with desktop.instance_lock(path):
                        self.fail('a second instance acquired the lock')
            with desktop.instance_lock(path):
                self.assertTrue(path.is_file())

    async def test_worker_can_save_to_a_new_unicode_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pipeline.py').write_text(WORKER, encoding='utf-8')
            with patch.object(app, 'STATE', root), patch.object(app, 'APP_DIR', root):
                jobs = app.Jobs()
                try:
                    base = root / '새 저장 폴더' / '분기 결과'
                    options = app.RunOptions(target='2026Q1', compare='2025Q4', sections=['institutions'],
                                             output_dir=str(base))
                    started = jobs.start(options, app.Quarter.parse(options.target), app.Quarter.parse(options.compare))
                    await asyncio.wait_for(jobs.task, 20)
                    finished = jobs.read(started['id'])
                    self.assertEqual(finished['status'], 'complete', finished['error'])
                    path = app.output_file(finished, '공시 결과.csv')
                    self.assertEqual(path.read_text(encoding='utf-8-sig'), '기관,보유액\n한글,100\n')
                    self.assertEqual(path.parents[1], app.output_folder(finished))
                    unregistered = path.with_name('unregistered.csv')
                    unregistered.touch()
                    with self.assertRaises(app.HTTPException):
                        app.output_file(finished, unregistered.name)
                finally:
                    jobs.db.close()

    async def test_cancelling_worker_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pipeline.py').write_text(
                "import time\nprint('@@DRAKOS@@{\"event\":\"progress\",\"stage\":\"source\","
                "\"fraction\":0.1,\"message\":\"collector started\"}', flush=True)\ntime.sleep(60)\n",
                encoding='utf-8',
            )
            with patch.object(app, 'STATE', root), patch.object(app, 'APP_DIR', root):
                jobs = app.Jobs()
                try:
                    options = app.RunOptions(target='2026Q1', compare='2025Q4', sections=['institutions'],
                                             output_dir=str(root / 'results'))
                    run = jobs.start(options, app.Quarter.parse(options.target), app.Quarter.parse(options.compare))

                    async def wait_for_worker():
                        while jobs.read(run['id'])['message'] != 'collector started':
                            await asyncio.sleep(0.02)

                    await asyncio.wait_for(wait_for_worker(), 15)
                    cancelled = await asyncio.wait_for(jobs.cancel(run['id']), 10)
                    self.assertEqual(cancelled['status'], 'cancelled')
                    self.assertFalse(app.staged_output(run).exists())
                    self.assertFalse(Path(run['destination']).exists())
                finally:
                    if jobs.active_id:
                        await jobs.cancel(jobs.active_id)
                    jobs.db.close()


if __name__ == '__main__':
    unittest.main()
