"""Verify real CUDA execution before loading the speech model."""
import json
from pathlib import Path
import torch


def main():
    report = {'torch': torch.__version__, 'cuda_build': torch.version.cuda,
              'cuda_available': torch.cuda.is_available()}
    if report['cuda_available']:
        device = torch.cuda.get_device_properties(0)
        report.update(device=device.name, vram_bytes=device.total_memory)
        matrix = torch.eye(32, device='cuda')
        result = matrix @ matrix
        torch.cuda.synchronize()
        report['matrix_check'] = bool(torch.equal(result, matrix))
    output = Path(__file__).resolve().parents[2] / 'artifacts/voiceover/gpu-check.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    if not report.get('matrix_check'):
        raise RuntimeError('CUDA execution is unavailable; speech GPU preparation cannot proceed.')


if __name__ == '__main__':
    main()
