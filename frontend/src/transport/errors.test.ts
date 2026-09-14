import { describe, expect, it } from 'vitest';
import { responseErrorMessage } from './errors';

describe('validation errors', () => {
  it('identifies the actual invalid render option without exposing request input', () => {
    expect(responseErrorMessage([{ loc: ['body', 'options', 'spacing'], type: 'int_from_float',
      msg: 'Input should be a valid integer', input: 'private value' }], 422))
      .toBe('Dữ liệu chưa hợp lệ: Giãn chữ: cần là số nguyên');
  });
  it('identifies the cue and preserves existing API messages', () => {
    expect(responseErrorMessage([{loc:['body','document','segments',2,'text'],msg:'Field required'}],422))
      .toContain('Phụ đề 3 · Nội dung phụ đề');
    expect(responseErrorMessage('Video không tồn tại',404)).toBe('Video không tồn tại');
    expect(responseErrorMessage({message:'Hết lượt thử'},429)).toBe('Hết lượt thử');
    expect(responseErrorMessage([null, {}],422)).toBe('Request failed (422)');
  });
});
