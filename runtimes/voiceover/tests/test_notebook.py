import json
from pathlib import Path
import zipfile
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_notebook_installs_and_checks_only_isolated_runtime(tmp_path, device):
    notebook = Path(__file__).resolve().parents[3] / 'notebooks/voiceover_kaggle.ipynb'
    cells = json.loads(notebook.read_text(encoding='utf-8'))['cells']
    calls = []
    def run(command, **kwargs):
        assert kwargs.get('check') is True
        calls.append(command)
    namespace = {'ROOT': tmp_path, 'Path': Path, 'sys': sys,
                 'subprocess': SimpleNamespace(run=run), 'manifest': {'device': device}}
    exec(compile(''.join(cells[2]['source']), str(notebook), 'exec'), namespace)
    python = str(tmp_path / 'runtime/bin/python')
    assert calls[0] == [sys.executable, '-m', 'venv', str(tmp_path / 'runtime')]
    assert all(command[0] == python for command in calls[1:])
    assert [python, '-m', 'pip', 'check'] in calls
    assert [python, '-m', 'pip', 'freeze'] in calls
    gpu_checks = [command for command in calls if '-c' in command]
    assert len(gpu_checks) == (1 if device == 'cuda' else 0)
    if gpu_checks:
        assert 'torch.isfinite' in gpu_checks[0][-1]


def test_notebook_refuses_environment_changes_while_worker_runs(tmp_path):
    notebook = Path(__file__).resolve().parents[3] / 'notebooks/voiceover_kaggle.ipynb'
    source = ''.join(json.loads(notebook.read_text(encoding='utf-8'))['cells'][2]['source'])
    with pytest.raises(RuntimeError, match='worker'):
        exec(compile(source, str(notebook), 'exec'), {
            'process': SimpleNamespace(poll=lambda: None), 'ROOT': tmp_path,
        })


def unpacker(root):
    notebook = Path(__file__).resolve().parents[3] / 'notebooks/voiceover_kaggle.ipynb'
    cells = json.loads(notebook.read_text(encoding='utf-8'))['cells']
    source = ''.join(cells[1]['source'])
    function = source[source.index('def unpack('):source.index('unpack(INPUT,')]
    namespace = {'ROOT': root.resolve(), 'zipfile': zipfile}
    exec(compile(function, str(notebook), 'exec'), namespace)
    return namespace['unpack']


def test_notebook_unpack_rejects_traversal_before_writing(tmp_path):
    archive = tmp_path / 'input.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', '{}')
        bundle.writestr('../escape.json', '{}')
    destination = tmp_path / 'output'
    destination.mkdir()
    with pytest.raises(ValueError):
        unpacker(destination)(archive, {'manifest.json'})
    assert not list(destination.iterdir())
    assert not (tmp_path / 'escape.json').exists()


def test_notebook_unpack_accepts_manifest_and_audio_checkpoint(tmp_path):
    archive = tmp_path / 'input.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', '{}')
        bundle.writestr('assets/' + 'a' * 64 + '.json', '{}')
    destination = tmp_path / 'output'
    destination.mkdir()
    unpacker(destination)(archive, {'manifest.json'})
    assert (destination / 'manifest.json').read_text() == '{}'
    assert len(list((destination / 'assets').iterdir())) == 1


def test_notebook_zip_keeps_previous_output_when_packaging_fails(tmp_path):
    notebook = Path(__file__).resolve().parents[3] / 'notebooks/voiceover_kaggle.ipynb'
    cells = json.loads(notebook.read_text(encoding='utf-8'))['cells']
    source = ''.join(cells[6]['source'])
    packaging_cell = next(c for c in cells if c.get('cell_type') == 'code' and any('voiceover-results.zip' in line for line in c.get('source', [])))
    source = ''.join(packaging_cell['source'])
    source = source.replace('Path("/kaggle/working/voiceover-results.zip")', 'OUTPUT_PATH')
    output = tmp_path / 'results.zip'
    output.write_bytes(b'previous-complete-package')
    # Missing manifest simulates a failed package build. The last complete
    # download must survive; only the temporary archive may be incomplete.
    with pytest.raises(FileNotFoundError):
        exec(compile(source, str(notebook), 'exec'), {
            'Path': Path, 'ROOT': tmp_path, 'OUTPUT_PATH': output,
            'json': json, 'zipfile': zipfile,
        })
    assert output.read_bytes() == b'previous-complete-package'


def test_notebook_has_preview_step_before_batch():
    notebook = Path(__file__).resolve().parents[3] / 'notebooks/voiceover_kaggle.ipynb'
    cells = json.loads(notebook.read_text(encoding='utf-8'))['cells']
    preview_cells = [c for c in cells if any('preview_sample.wav' in line for line in c.get('source', []))]
    assert preview_cells, 'Notebook must contain a preview step before full run'
    worker_run_index = next(i for i, c in enumerate(cells) if any('worker.py' in line and 'Popen' in line for line in c.get('source', [])))
    preview_cell_index = next(i for i, c in enumerate(cells) if any('preview_sample.wav' in line for line in c.get('source', [])))
    assert preview_cell_index < worker_run_index, 'Preview cell must precede worker run cell'
