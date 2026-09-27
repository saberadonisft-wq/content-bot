from app.services import subtitle_asr as asr


def test_runtime_status_hides_cuda_without_an_accessible_gpu(tmp_path, monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, 'get_cuda_device_count', lambda: 0)
    monkeypatch.setattr(ctranslate2, 'get_supported_compute_types', lambda device: {'int8', 'float32'})
    status = asr.asr_runtime_status(tmp_path, allow_download=False)
    assert [device['id'] for device in status['devices']] == ['cpu']


def test_runtime_status_exposes_only_actual_cuda_compute_types(tmp_path, monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, 'get_cuda_device_count', lambda: 1)
    monkeypatch.setattr(ctranslate2, 'get_supported_compute_types',
                        lambda device: {'float16', 'int8'} if device == 'cuda' else {'int8'})
    monkeypatch.setattr(asr, '_cuda_runtime_diagnostic', lambda: {'ready': True, 'message': None})
    status = asr.asr_runtime_status(tmp_path, allow_download=True)
    assert status['devices'] == [
        {'id': 'cpu', 'compute_types': ['int8']},
        {'id': 'cuda', 'compute_types': ['float16', 'int8']},
    ]


def test_runtime_status_hides_cuda_when_provider_dll_is_missing(tmp_path, monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, 'get_cuda_device_count', lambda: 1)
    monkeypatch.setattr(ctranslate2, 'get_supported_compute_types', lambda device: {'float16', 'int8'})
    monkeypatch.setattr(asr, '_cuda_runtime_diagnostic', lambda: {
        'ready': False,
        'message': 'Thiếu runtime cuBLAS cho ASR GPU.',
    })
    status = asr.asr_runtime_status(tmp_path, allow_download=False)
    assert [device['id'] for device in status['devices']] == ['cpu']
    assert status['runtimes']['cuda']['ready'] is False
