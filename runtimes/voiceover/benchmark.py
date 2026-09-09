"""Create review samples and record actual runtime; run after setup."""
import argparse
import json
import time
from pathlib import Path

from worker import load_engine, write

TEXT = (
    "Cô gái tưởng rằng chuyến tàu này sẽ đưa mình trở về nhà. Nhưng khi nhìn ra cửa sổ, "
    "cô phát hiện thành phố quen thuộc đã hoàn toàn biến mất. Người đàn ông ngồi đối diện "
    "bỗng đặt lên bàn một chiếc đồng hồ đã ngừng chạy. Ông nói rằng cô chỉ còn mười lăm phút "
    "để tìm ra sự thật. Ban đầu, cô nghĩ đây chỉ là một trò đùa. Thế nhưng, tấm ảnh trong "
    "chiếc ví của ông lại khiến cô sững người: đó chính là căn nhà của mình hai mươi năm trước. "
    "Không còn thời gian do dự, cô đứng dậy và đi về phía toa cuối. Cánh cửa từ từ mở ra, "
    "để lộ một người mà cô chưa từng nghĩ sẽ gặp lại. Liệu cô có kịp thay đổi kết cục, "
    "hay mọi chuyện đã được định sẵn ngay từ lúc chuyến tàu rời ga?"
)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--output', default='artifacts/voiceover/samples')
    args = parser.parse_args()
    import soundfile as sf
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    engine = load_engine(args.device)
    report = {'device': args.device, 'load_seconds': time.monotonic() - start, 'text': TEXT, 'samples': []}
    if args.device == 'cuda':
        import torch
        torch.cuda.reset_peak_memory_stats()
    for voice, filename in [('Ngọc Huyền', 'ngoc-huyen'), ('Ngọc Linh', 'ngoc-linh'), ('Quỳnh Anh', 'quynh-anh')]:
        start = time.monotonic()
        audio = engine.infer(TEXT, voice=voice, temperature=0.8, batch_size=1)
        seconds = len(audio) / engine.sample_rate
        elapsed = time.monotonic() - start
        sf.write(str(out / f'{filename}-{args.device}.wav'), audio, engine.sample_rate, subtype='PCM_16')
        report['samples'].append({'voice': voice, 'audio_seconds': seconds, 'generation_seconds': elapsed,
                                  'rtf': elapsed / seconds})
        if args.device == 'cuda':
            report['samples'][-1].update(peak_vram_allocated_bytes=torch.cuda.max_memory_allocated(),
                                         peak_vram_reserved_bytes=torch.cuda.max_memory_reserved())
        write(out / f'benchmark-{args.device}.json', report)
        print(json.dumps(report['samples'][-1], ensure_ascii=False), flush=True)
