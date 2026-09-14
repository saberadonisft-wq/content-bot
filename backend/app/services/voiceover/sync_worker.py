"""Run source inference in a disposable CPU process with a hard lifetime bound."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..subtitle_jobs import SubtitleJobCanceled
from .models import VoiceDocument
from .store import VoiceStore, read_json, write_json
from .sync_audit import audit_path, run_sync_audit, snapshot_binding


def _stop_tree(pid: int):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_sync_audit_worker(store: VoiceStore, owner: str, identifier: str, video: Path, media: dict,
                          voice: VoiceDocument, subtitles: dict, clip_ids: list[str], *,
                          align_source: bool, model_dir: Path, whisper_model: str = 'small',
                          dubbed_whisper_model: str = 'small',
                          cancel: threading.Event | None = None, progress=None,
                          timeout_seconds: float = 600, repair: dict | None = None) -> dict:
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise SubtitleJobCanceled('Đã hủy kiểm tra đồng bộ')
    if not 0 < timeout_seconds <= 600:
        raise ValueError('Ngân sách kiểm tra phải trong khoảng 0–600 giây.')
    path = audit_path(store, owner, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    project_path = store.path(owner, 'projects', voice.project_id)
    expected_binding = snapshot_binding(voice, subtitles)

    def stamp():
        stat = project_path.stat()
        return stat.st_mtime_ns, stat.st_size

    def ensure_current():
        if snapshot_binding(store.get_document(owner, voice.project_id), subtitles) != expected_binding:
            raise SubtitleJobCanceled('Đầu vào dự án đã thay đổi; đã dừng lượt kiểm tra cũ.')

    observed_stamp = stamp()
    ensure_current()
    with tempfile.TemporaryDirectory(prefix=f'{identifier}-', dir=path.parent) as temporary:
        folder = Path(temporary)
        write_json(folder / 'input.json', {
            'parent_pid': os.getpid(),
            'store_root': str(store.root.resolve()), 'owner': owner, 'identifier': identifier,
            'video': str(video.resolve()), 'media': media, 'voice': voice.model_dump(mode='json'),
            'subtitles': subtitles, 'clip_ids': clip_ids, 'align_source': align_source,
            'model_dir': str(model_dir.resolve()), 'whisper_model': whisper_model,
            'dubbed_whisper_model': dubbed_whisper_model, 'repair': repair})
        env = {**os.environ, 'OMP_NUM_THREADS': '3', 'MKL_NUM_THREADS': '3',
               'OPENBLAS_NUM_THREADS': '3', 'CUDA_VISIBLE_DEVICES': '', 'HF_HUB_OFFLINE': '1'}
        process = subprocess.Popen([sys.executable, '-m', 'app.services.voiceover.sync_worker', str(folder)],
            cwd=Path(__file__).resolve().parents[3], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
        last_progress = None
        try:
            while True:
                if cancel.is_set():
                    raise SubtitleJobCanceled('Đã hủy kiểm tra đồng bộ')
                latest_stamp = stamp()
                if latest_stamp != observed_stamp:
                    ensure_current()
                    observed_stamp = latest_stamp
                if time.monotonic() - started >= timeout_seconds:
                    raise TimeoutError('Kiểm tra đồng bộ hết ngân sách thời gian; đã dừng xử lý audio.')
                if process.poll() is not None:
                    break
                if progress:
                    try:
                        update = read_json(folder / 'progress.json')
                    except (OSError, ValueError):
                        update = None
                    if update and update != last_progress:
                        progress(*update)
                        last_progress = update
                cancel.wait(.2)
            result = read_json(folder / 'result.json')
            if process.returncode or 'error' in result:
                raise RuntimeError(result.get('error', 'Tiến trình kiểm tra đồng bộ bị gián đoạn.'))
            return result['result']
        except BaseException as exc:
            if process.poll() is None:
                _stop_tree(process.pid)
            process.wait(timeout=10)
            # Write only after the child has stopped, preserving its partial results.
            record = read_json(path) if path.exists() else {
                'id': identifier, 'project_id': voice.project_id, 'rows': [], 'warnings': []}
            record.update(state='canceled' if isinstance(exc, SubtitleJobCanceled) else 'failed',
                          error=str(exc)[:500], elapsed_seconds=round(time.monotonic() - started, 3))
            write_json(path, record)
            raise
        finally:
            if process.poll() is None:
                _stop_tree(process.pid)
                process.wait(timeout=10)
            if process.stdin:
                process.stdin.close()


def _watch_parent(parent_pid: int):
    if os.name == 'nt':
        import ctypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE only
        if handle:
            try:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            finally:
                kernel.CloseHandle(handle)
    else:
        while os.getppid() == parent_pid:
            time.sleep(1)
    _stop_tree(os.getpid())


def main():
    folder = Path(sys.argv[1])
    args = read_json(folder / 'input.json')
    parent_pid = args.pop('parent_pid')
    # A native process handle avoids stdin locks that can block NumPy DLL initialization.
    threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True, name='sync-parent-watch').start()
    store = VoiceStore(Path(args.pop('store_root')))
    args['video'] = Path(args['video'])
    args['model_dir'] = Path(args['model_dir'])
    args['voice'] = VoiceDocument.model_validate(args['voice'])
    try:
        repair = args.pop('repair', None)
        runner = run_sync_audit
        if repair is not None:
            from .repair_audio import run_repair_audio
            runner = run_repair_audio
            if repair.get('kind') == 'group':
                from .group_audio import run_group_audio
                runner = run_group_audio
            args['repair'] = repair
        result = runner(store, **args,
            progress=lambda *update: write_json(folder / 'progress.json', update))
        write_json(folder / 'result.json', {'result': result})
    except Exception as exc:
        write_json(folder / 'result.json', {'error': str(exc)[:500]})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
