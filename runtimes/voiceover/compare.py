"""Generate two local listening samples with/without reference denoising."""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', choices=worker.ENGINES, required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda'], required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--text', default='Hôm nay chúng ta cùng thử giọng đọc mới. Bạn nghe xem chất giọng đã gần với bản gốc chưa nhé.')
    args = parser.parse_args()
    engine = worker.load_engine(args.device, model_id=worker.ENGINES[args.engine][0])
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for denoise in [False, True]:
        engine.add_voice('comparison', str(args.reference), denoise=denoise, save=False)
        audio = np.asarray(worker.infer_with_retry(engine, args.text, {'voice': 'comparison'}, args.device))
        if not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 0.0001:
            raise ValueError('Engine did not produce valid audio')
        path = args.output / f'{args.engine}-{"denoise" if denoise else "original"}.wav'
        sf.write(path, audio, engine.sample_rate, subtype='PCM_16')
        rows.append({'file': path.name, 'sample_rate': engine.sample_rate,
                     'duration_seconds': round(audio.size / engine.sample_rate, 2), 'denoise': denoise})
    (args.output / f'{args.engine}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == '__main__':
    main()
