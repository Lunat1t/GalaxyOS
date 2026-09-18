'use strict';
const display = document.getElementById('display');
let input = '0', previous = null, operator = null, fresh = false;
function press(key) {
  if (key === 'Escape' || key.toLowerCase() === 'c') { input = '0'; previous = operator = null; fresh = false; }
  else if (/^[0-9]$/.test(key)) { input = fresh || input === '0' ? key : input + key; fresh = false; }
  else if (key === '.' && (fresh || !input.includes('.'))) { input = fresh ? '0.' : input + '.'; fresh = false; }
  else if (key === 'Backspace') input = input.length > 1 ? input.slice(0,-1) : '0';
  else if ('+-*/%'.includes(key) && key.length === 1) { previous = Number(input); operator = key; fresh = true; }
  else if ((key === 'Enter' || key === '=') && operator !== null) {
    try { input = String(calculate(previous, operator, Number(input))); } catch (error) { input = error.message; }
    operator = previous = null; fresh = true;
  }
  display.textContent = input;
}
document.getElementById('keys').addEventListener('click', event => { const key = event.target.dataset.key; if (key) press(key); });
document.addEventListener('keydown', event => { if (/^[0-9.+*/%=-]$/.test(event.key) || ['Enter','Escape','Backspace'].includes(event.key)) { event.preventDefault(); press(event.key); } });
