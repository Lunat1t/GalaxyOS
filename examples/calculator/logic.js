'use strict';
function calculate(a, op, b) {
  if (!Number.isFinite(a) || !Number.isFinite(b)) throw new TypeError('Введите числа');
  if (op === '+') return a + b;
  if (op === '-') return a - b;
  if (op === '*') return a * b;
  if (op === '/') { if (b === 0) throw new RangeError('Деление на ноль'); return a / b; }
  if (op === '%') { if (b === 0) throw new RangeError('Деление на ноль'); return a % b; }
  throw new Error('Неизвестная операция');
}
if (typeof module !== 'undefined') module.exports = { calculate };
